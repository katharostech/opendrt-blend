#!/usr/bin/env python3
"""Deterministic golden-data generator for the compiled-DCTL oracle shim.

Generates the three deterministic input sets (linear CIE XYZ D65 -> the shim pins
in_oetf=Linear and in_gamut=XYZ-identity, exactly like the planned node tree),
runs tools/dctl_shim/build_dctl_shim once per (display_encoding 0..8, set),
writes tests/golden/enc<N>/<set>.bin, and runs every case a second time to
assert byte-identical output (determinism gate). Also asserts all outputs are
finite and records per-encoding shim config (from --json-info) plus sha256 of
every golden file into tests/golden/index.json.

Inputs never feed NaN/Inf; the "negative / superwhite" edge cases in the patch
grid check that the shim stays finite rather than probing undefined domain.

Usage: pixi run python tools/verify_shim.py [--shim PATH] [--out DIR]
"""

import argparse
import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHIM = ROOT / "tools/dctl_shim/build_dctl_shim"
DEFAULT_OUT = ROOT / "tests/golden"

# display_encoding_preset option order, as in reference/OpenDRT_v1.1.0.dctl line 128.
ENC_NAMES = ["rec1886", "srgb", "displayp3", "p3d60", "p3dci", "xyz",
             "rec2100pq", "rec2100hlg", "dolbypq"]

BUILD_FLAGS = "g++ -std=c++17 -O2 -ffp-contract=off -fno-fast-math"

# The D65 whitepoint used throughout: (0.95046, 1.0, 1.08906).
D65_WHITE = (0.95046, 1.0, 1.08906)


def d65_scaled(y):
    """D65-chromaticity neutral with the given Y (luma) level."""
    return (D65_WHITE[0] * y, y, D65_WHITE[2] * y)


# ---------------------------------------------------------------------------
# Patch grid: hard-coded table of (name, X, Y, Z) in linear CIE XYZ D65.
# 40 rows: D65 luma ladder, neutrals, primaries, memorable colors, vivid
# chromaticities, alternate whites, negative-value edge cases, superwhites.
# ---------------------------------------------------------------------------
PATCH_GRID = [
    # D65-axis luminance ladder (shadows -> superwhite), X/Z = Y * D65 offsets
    ("white-d65-y0.004",   0.003802,  0.004,  0.004356),
    ("white-d65-y0.02",    0.019009,  0.02,   0.021781),
    ("white-d65-y0.09",    0.085541,  0.09,   0.098015),
    ("gray-0.18",          0.171083,  0.18,   0.196031),
    ("white-d65-y0.41",    0.389689,  0.41,   0.446515),
    ("white-d65-y1.0",     0.95046,   1.0,    1.08906),
    ("white-d65-y2.0",     1.90092,   2.0,    2.17812),
    ("white-d65-y8.0",     7.60368,   8.0,    8.71248),
    ("white-d65-y64.0",    60.82944,  64.0,   69.69984),
    ("white-d65-y10000.0", 9504.6,    10000.0, 10890.6),
    # neutrals and edge cases
    ("equal-power-gray-y0.18",      0.18,     0.18,    0.18),
    ("black",                       0.0,      0.0,     0.0),
    ("negative-x",                  -0.1,     0.18,    0.15),
    ("negative-z",                  0.171083, 0.18,   -0.05),
    ("mixed-negative-1",            0.3,     -0.05,    0.25),
    ("mixed-negative-2",           -0.02,    -0.04,    0.1),
    # Rec.709 primaries in XYZ (red Y=0.18, green Y=0.18, blue luma-weighted)
    ("rec709-red-y0.18",     0.349091, 0.18,  0.016364),
    ("rec709-green-y0.18",   0.09,     0.18,  0.03),
    ("rec709-blue-y0.013",   0.0325,   0.013, 0.171167),
    # P3 primaries in XYZ (blue coincides with Rec.709 blue; skipped)
    ("p3d65-red-y0.18",      0.3825,   0.18,  0.0),
    ("p3d65-green-y0.18",    0.06913,  0.18,  0.011739),
    # memorable colors
    ("skin-light-y0.18",     0.205714, 0.18,  0.128571),
    ("skin-dark-y0.07",      0.079459, 0.07,  0.03973),
    ("foliage-y0.07",        0.051667, 0.07,  0.045),
    ("sky-y0.25",            0.24,     0.25,  0.51),
    ("ocean-y0.08",          0.072727, 0.08,  0.210909),
    ("brick-y0.09",          0.126,    0.09,  0.041143),
    ("rose-y0.11",           0.18,     0.11,  0.21),
    # vivid near-gamut chromaticities
    ("magenta-vivid-y0.08",  0.186667, 0.08,  0.266667),
    ("cyan-vivid-y0.1",      0.05,     0.1,   0.207143),
    ("yellow-vivid-y0.16",   0.137941, 0.16,  0.015686),
    ("orange-vivid-y0.1",    0.1375,   0.1,   0.0125),
    ("purple-deep-y0.004",   0.009714, 0.004, 0.043429),
    ("green-pure-y0.05",     0.014583, 0.05,  0.004861),
    ("red-extreme-y0.02",    0.054074, 0.02,  0.0),
    ("teal-y0.09",           0.033,    0.09,  0.177),
    # alternate whitepoints at Y=1.0 (DCI / D60 / D93) and a shifted d60 luma step
    ("dci-white-y1.0",       0.894587, 1.0,   0.954416),
    ("d60-white-y1.0",       0.952293, 1.0,   1.008586),
    ("d93-white-y1.0",       0.952862, 1.0,   1.414141),
    ("d60-white-y0.41",      0.39044,  0.41,  0.41352),
]

SET_NEUTRAL_POINTS = 4096
SET_HDR_POINTS = 2048
HDR_LO_EXP, HDR_HI_EXP = -4.0, 4.0  # 1e-4 .. 1e4


def gen_neutral_ramp():
    """4096 points, r=g=b, linearly spaced 0..1 inclusive."""
    n = SET_NEUTRAL_POINTS
    return [(i / (n - 1), i / (n - 1), i / (n - 1)) for i in range(n)]


def gen_hdr_log_sweep():
    """2048 points, r=g=b, log-spaced 1e-4 .. 1e4 inclusive."""
    n = SET_HDR_POINTS
    lo, hi = HDR_LO_EXP, HDR_HI_EXP
    return [(10.0 ** (lo + (hi - lo) * i / (n - 1)),) * 3 for i in range(n)]


def gen_patch_grid():
    """40 hard-coded XYZ D65 chromaticity patches."""
    return [(x, y, z) for _, x, y, z in PATCH_GRID]


def pack_triplets(triplets):
    """Round python doubles to float32 and pack interleaved R,G,B little-endian."""
    return b"".join(struct.pack("<3f", *(struct.unpack("<3f", struct.pack("<3f", *t))))
                    for t in triplets)


def run_shim(shim, enc, data):
    proc = subprocess.run([str(shim), "--enc", str(enc)], input=data,
                          capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"shim --enc {enc} failed rc={proc.returncode}: "
                           f"{proc.stderr.decode(errors='replace')}")
    return proc.stdout


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def check_finite(raw):
    """Return (values, count_of_non_finite) for a float32 buffer."""
    n = len(raw) // 4
    vals = struct.unpack("<%df" % n, raw)
    bad = [v for v in vals if v != v or v in (float("inf"), float("-inf"))]
    return vals, len(bad)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shim", default=str(DEFAULT_SHIM))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    shim = Path(args.shim)
    outdir = Path(args.out)
    if not shim.exists():
        print(f"ERROR: shim binary not found at {shim}\n"
              f"build it first: pixi run build-shim", file=sys.stderr)
        return 1
    outdir.mkdir(parents=True, exist_ok=True)

    sets = {
        "neutral-ramp": gen_neutral_ramp(),
        "hdr-log-sweep": gen_hdr_log_sweep(),
        "patch-grid": gen_patch_grid(),
    }
    packed = {name: pack_triplets(rows) for name, rows in sets.items()}

    index = {
        "format": "opendrt-dctl-shim-goldens/1",
        "generated_by": "tools/verify_shim.py",
        "input_space": "linear CIE XYZ D65 (shim pins in_oetf=0 Linear, in_gamut=0 XYZ identity; "
                       "crv overlay disabled) - identical omissions to the planned node tree",
        "shim": {
            "binary_sha256": sha256(shim.read_bytes()),
            "build_flags": BUILD_FLAGS,
            "reference": "OpenDRT_v1.1.0.dctl",
            "reference_sha256": sha256((ROOT / "reference/OpenDRT_v1.1.0.dctl").read_bytes()),
            "pins": {"in_oetf": 0, "in_gamut": 0, "look_preset": 0, "tonescale_preset": 0,
                     "cwp_selector": 3, "crv_enable": 0},
        },
        "sets": {
            name: {
                "triplets": len(rows),
                "input_sha256": sha256(packed[name]),
                **({"rows": [r[0] for r in PATCH_GRID]} if name == "patch-grid" else {}),
            }
            for name, rows in sets.items()
        },
        "encodings": {},
        "checks": {"determinism_reruns": 2, "all_finite": None},
    }

    failures = []
    finite_bad_total = 0

    for enc in range(9):
        enc_info = subprocess.run([str(shim), "--enc", str(enc), "--json-info"],
                                  capture_output=True)
        if enc_info.returncode != 0:
            raise RuntimeError(f"shim --enc {enc} --json-info failed: "
                               f"{enc_info.stderr.decode(errors='replace')}")
        cfg = json.loads(enc_info.stdout)

        enc_entry = {
            "name": ENC_NAMES[enc],
            "config": cfg,
            "files": {},
        }
        for name, data in packed.items():
            run1 = run_shim(shim, enc, data)
            run2 = run_shim(shim, enc, data)   # determinism gate: byte-identical rerun
            if run1 != run2:
                failures.append(f"enc {enc} set {name}: rerun output differs")
                continue
            if len(run1) != len(data):
                failures.append(f"enc {enc} set {name}: output length {len(run1)} != "
                                f"input {len(data)}")
                continue
            vals, bad = check_finite(run1)
            finite_bad_total += bad
            dest = outdir / f"enc{enc}" / f"{name}.bin"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(run1)
            enc_entry["files"][name] = {
                "path": f"enc{enc}/{name}.bin",
                "triplets": len(data) // 12,
                "bytes": len(run1),
                "sha256": sha256(run1),
                "deterministic": True,
                "finite": bad == 0,
            }
        index["encodings"][str(enc)] = enc_entry
        print(f"enc {enc} ({ENC_NAMES[enc]}): {len(enc_entry['files'])}/3 sets ok, "
              + ", ".join(v["sha256"][:12] for v in enc_entry["files"].values()))

    if failures:
        print("\nFAILURES:\n" + "\n".join(failures), file=sys.stderr)
        return 1

    # ------------------------------------------------------------------
    # Self-evidence: encoded values for the mid-gray patch (gray-0.18) under
    # enc 0 (rec1886) and enc 1 (srgb), plus all-encodings table for reference.
    # ------------------------------------------------------------------
    mid_row = next(i for i, r in enumerate(PATCH_GRID) if r[0] == "gray-0.18")
    evidence = {}
    for enc in range(9):
        raw = (outdir / f"enc{enc}" / "patch-grid.bin").read_bytes()
        r, g, b = struct.unpack_from("<3f", raw, mid_row * 12)
        evidence[enc] = (r, g, b)
    print("\nMid-gray patch gray-0.18 (0.171083, 0.18, 0.196031) encoded values:")
    for enc in range(9):
        r, g, b = evidence[enc]
        print(f"  enc {enc} {ENC_NAMES[enc]:10s} -> ({r:.7f}, {g:.7f}, {b:.7f})")

    index["checks"]["all_finite"] = finite_bad_total == 0
    index["self_evidence"] = {
        "gray_0.18_row": mid_row,
        "display_encoding_preset_names": ENC_NAMES,
        "gray_0.18_encoded": {str(enc): list(v) for enc, v in evidence.items()},
    }
    index_path = outdir / "index.json"
    index_path.write_text(json.dumps(index, indent=2, sort_keys=False) + "\n")

    total = sum(len(s) for s in packed.values()) * 9
    print(f"\nOK: {total} golden triplets across 9 encodings x 3 sets; "
          f"all finite: {index['checks']['all_finite']}; "
          f"index.json -> {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())