#!/usr/bin/env python3
"""OpenDRT v1.1.0 -> Blender compositor node group (Build wave).

Contract: docs/BUILDER_DESIGN.md (+ STATUS.md final decisions). Ground truth
for math semantics: docs/PROBE_REPORT.md (measured Blender facts win over
design-doc fallbacks) and spec/opendrt-v1.1.0.json (the IR).

Emission (user decision 2026-10-26: nested node groups; NO NodeFrames and
NO node layout - the group interior is compiler output, not a reading aid):
  - main tree "OpenDRT 1.1 Base" (defaults = the official "Base" look
    preset; every other official look also gets a thin wrapper node
    "OpenDRT 1.1 [look]" = one Base instance, inputs forwarded 1:1):
    interface = 1 input "Image" (NodeSocketColor
    = linear CIE XYZ D65, values used as-is, no color-management conversion
    in-tree) + 69 parameter sockets + 9 color outputs (one per display
    encoding; selection = which output is linked; per-output lazy
    evaluation, PROBE_REPORT §2); interior = SeparateColor at the Image
    entry + tonescale cluster (src 943-960 emitted once; ts_p (948) and
    ts_dsc (953) live inside the branch groups) + 9 CompositorNodeGroup
    instances, one per encoding, each wired with the live vars it reads.
  - per-encoding branch groups "OpenDRT b{enc}": src 964..1210 (crv overlay
    pruned exactly per design §D.4) with branch-baked constants (tn_su,
    display_gamut, eotf) + CombineColor -> one color output socket.
  - fn groups "OpenDRT fn {name}" (bodies emitted ONCE; call sites are
    CompositorNodeGroup instances with per-instance arg defaults): fns with
    >= 2 static call sites reachable from the emitted regions, except
    GROUP_EXEMPT (see below); vdot gets a dedicated 12-socket group;
    display_gamut_whitepoint gets one group per baked gamut. Group arg
    sockets are flat floats; float3 args/returns are .x/.y/.z sockets so
    call sites keep the channel-tracked scalar chains with no boundary
    Separate/Combine nodes.
  - matrices as flat 9x MUL + 6 ADD (per-site instance defaults in the
    vdot group); literals cast to float32 at emission (single-rounded IEEE
    f32; arithmetic via numpy float32).
  - naming: call-site nodes and branch-group interiors b{B} r{src}#{k}
    (B = branch 0..8, "bS" = main tree; k = 1-based per-statement index);
    fn-group interiors "g r{src}#{k}". No NodeFrames anywhere.
  - asset hygiene: main group asset_mark() + use_fake_user=True + provenance
    description (spec sha256 + DCTL sha256; NO timestamps); every emitted
    tree written via bpy.data.libraries.write(compress=True,
    path_remap="NONE"); never-instantiated groups removed with a note;
    fake-user bit written by libraries.write itself (fake_user=True). The
    main group is the sole asset; the "OpenDRT" catalog sidecar
    (blender_assets.cats.txt) is written beside the file. Blend-byte
    determinism is explicitly not a goal (see STATUS.md).

Measured-semantics reconciliations (design doc + measurements; also printed
by the build log):
  D1  R-01 sdivf = one DIVIDE node: measured Blender DIVIDE x/0.0 -> 0.0 on
      CPU (== DCTL `return 0.0f`); no NaN/inf reliance.
  D2  §B eq = Math COMPARE with epsilon 0.0: measured exact equals fire;
      |diff| 1e-7 still fires, 5e-7 does not (near-equality band ~1e-7;
      reachable only via off-step slider values - documented).
  D3  Mix arms: measured ShaderNodeMix(FLOAT) propagates NaN from ANY arm
      (even at factor 0) and saturates inf in an arm (even when outweighed).
      eotf_hlg's arm LOGARITHM input is wrapped in MAXIMUM(..., 1e-6): the
      arm is only used when 12*ch - 0.28466892 > 0.715 (never altered); it
      exists only to keep the always-computed arm finite (the DCTL skips
      untaken branches; node arms always compute).
  D4  R-03 spowf keeps its gate (1-GT(a,0) -> Mix; a<=0 -> a) wherever the
      DCTL emits one; in compress_toe_quadratic's inv=0 arm the gate is
      elided because x >= 0 is structural at every live site (design §B's
      recipe POWER(x, 2.0) assumes it). compress_toe_cubic's inv=1 arm IS
      live (call site 1031) and is emitted as prescribed.
  D5  §B contrast_high's inner gate: the IR is authoritative (true arm =
      the k1 linear extension past x1); the design row lists that Mix's A/B
      inverted relative to its own B-R-4 rule (A = false side).
  D6  §D.7 spowf3(rgb, 1.0/eotf_p) folds to the single-rounded f32 division:
      0x3ED55555 @2.4, 0x3EE8BA2E @2.2 as printed; @2.6 the correctly-rounded
      f32 result is 0x3EC4EC4E (the design's 0x3EC4EC4F is the f64 quotient
      rounded twice). The emitted value follows the IEEE f32 division that
      both the DCTL runtime and Blender DIVIDE compute; 1 ULP, R-02.
  D7  §C folded constants attach as unconnected-input default_value (0 extra
      nodes); the "#c{k}" naming applies only to materialized constant
      nodes, which v1 emission does not need.
  D8  §E/§F socket inventory = §E table (68 rows) + "HDR Purity" (pt_hdr,
      §F panel 5 note; DCTL 19) = 69 parameter sockets total.
  D9  §B.6 cwp ladders: acc starts at candidate 0; fires are the DCTL arms'
      own conditions (eq cwp k / ge cwp 2), mutually exclusive for reachable
      integer cwp; a fractional cwp resolves to the candidate-0 arm
      (unreachable from the DCTL's step-1 combo; documented).

Usage:
  pixi run python tools/build_nodes.py --spec spec/opendrt-v1.1.0.json \
    --out dist/OpenDRT_v1.1.0.blend
Exit codes: 0 ok; 1 any unmappable IR element or failed invariant (no silent
drops, fails loudly); 2 usage error.
"""

import argparse
import hashlib
import json
import struct
import sys
import uuid
from pathlib import Path

import numpy as np
import bpy

GROUP_NAME = "OpenDRT 1.1 Base"
PRESET_PREFIX = "OpenDRT 1.1 "

CLUSTER_SRC = set(range(943, 961))         # shared tonescale cluster
CLUSTER_PER_BRANCH = {948, 953}            # ts_p / ts_dsc move per branch
BRANCH_LO, BRANCH_HI = 964, 1210           # per-branch duplicated region
TAIL_LO = 1158                             # "branch tail" frame start
CRV_SKIP = {925, 930, 976, 1035, 1043, 1051, 1052, 1162, 1170, 1171, 1191,
            1199}                          # design §D.4 prune, by item src
SHARED_TAG = "S"

# Fns that do NOT become fn groups:
#  - sdivf: one DIVIDE node is the whole body (a group would be bigger).
#  - display_gamut_whitepoint: emitted as per-gamut variant groups instead
#    (its ladder tables + back conversion are baked per display_gamut).
#  - everything else here carries sqrt/ln inside gated arms: with live group
#    args the gate would emit BOTH arms; a Blender untaken-arm NaN would
#    contaminate the Mix (D3). hypotf*'s sqrt arg is a sum of squares
#    (structurally safe), but the boring rule keeps the sites inline.
GROUP_EXEMPT = {"sdivf", "display_gamut_whitepoint", "hypotf2", "hypotf3",
                "compress_toe_quadratic", "compress_toe_cubic", "eotf_hlg"}

E_F32 = np.float32(2.718281828459045)      # LOGARITHM natural base; see D2
IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

UNASSIGNED = object()


class BuildError(Exception):
    """Any unmappable IR element or failed invariant (fail loudly, exit 1)."""


def f32(v):
    return struct.unpack("f", struct.pack("f", v))[0]


def f32_from_hex(h):
    return struct.unpack("f", struct.pack("I", int(h, 16)))[0]


def hex_of_f32(v):
    return "0x%08X" % struct.unpack("I", struct.pack("f", np.float32(v)))[0]


def f32_from_literal(text):
    """Parse a C float literal ('0.4865f', '4', '0.004') to float32."""
    return np.float32(np.float64(str(text).strip().rstrip("fF")))


def f32_lit(op, a, b):
    """f32 arithmetic in IR order via numpy float32 (IEEE single rounding)."""
    a, b = np.float32(a), np.float32(b)
    if op == "add":
        r = a + b
    elif op == "sub":
        r = a - b
    elif op == "mul":
        r = a * b
    elif op == "div":
        r = a / b
    elif op == "pow":
        r = np.power(a, b)
    elif op == "min":
        r = np.minimum(a, b)
    elif op == "max":
        r = np.maximum(a, b)
    elif op == "neg":
        r = -a
    elif op == "sqrt":
        r = np.sqrt(a)
    elif op == "exp":
        r = np.exp(a)
    elif op == "ln":
        r = np.log(a)
    elif op == "log2":
        r = np.log2(a)
    elif op == "eq":
        r = np.float32(1.0) if a == b else np.float32(0.0)
    elif op == "ne":
        r = np.float32(1.0) if a != b else np.float32(0.0)
    elif op == "lt":
        r = np.float32(1.0) if a < b else np.float32(0.0)
    elif op == "gt":
        r = np.float32(1.0) if a > b else np.float32(0.0)
    elif op == "le":
        r = np.float32(1.0) if a <= b else np.float32(0.0)
    elif op == "ge":
        r = np.float32(1.0) if a >= b else np.float32(0.0)
    elif op == "and":
        r = a * b
    elif op == "or":
        r = np.maximum(a, b)
    else:
        raise BuildError(f"no scalar fold for op '{op}'")
    return np.float32(r)


def src_first(s):
    return int(str(s).split("-")[0])


def is_crv_stmt(st):
    return src_first(st.get("src", "0")) in CRV_SKIP


# --------------------------------------------------------------------------
# Socket inventory - transcribed from docs/BUILDER_DESIGN.md §E (68 rows) plus
# HDR Purity (§F panel 5 note; DCTL 19) = 69 parameter sockets. Cross-checked
# against spec.parameters (label/min/max/step) and default sources at build
# time; ANY mismatch fails the build loudly.
#   (socket_name, ui_param_id, kind, f32 default hex-or-num, min, max, step, src)
# --------------------------------------------------------------------------

SOCKET_TABLE = [
    ("Display Peak Luminance", "tn_Lp", "float", "0x42C80000", 100.0, 1000.0, 1.0, "17"),
    ("Display Grey Luminance", "tn_Lg", "float", "0x41200000", 3.0, 25.0, 0.1, "20"),
    ("HDR Grey Boost", "tn_gb", "float", "0x3E051EB8", 0.0, 1.0, 0.001, "18"),
    ("Contrast", "_tn_con", "float", "0x3FD47AE1", 1.0, 2.0, 0.01, "43"),
    ("Shoulder Clip", "_tn_sh", "float", "0x3F000000", 0.0, 1.0, 0.01, "44"),
    ("Toe", "_tn_toe", "float", "0x3B449BA6", 0.0, 0.1, 0.001, "45"),
    ("Offset", "_tn_off", "float", "0x3BA3D70A", 0.0, 0.02, 0.0002, "46"),
    ("Enable Contrast High", "_tn_hcon_enable", "bool", "0x00000000", 0, 1, 1, "47"),
    ("Contrast High", "_tn_hcon", "float", "0x00000000", -1.0, 1.0, 0.02, "48"),
    ("Contrast High Pivot", "_tn_hcon_pv", "float", "0x3F800000", 0.0, 4.0, 0.04, "49"),
    ("Contrast High Strength", "_tn_hcon_st", "float", "0x40800000", 0.0, 4.0, 0.04, "50"),
    ("Enable Contrast Low", "_tn_lcon_enable", "bool", "0x00000000", 0, 1, 1, "51"),
    ("Contrast Low", "_tn_lcon", "float", "0x00000000", 0.0, 3.0, 0.03, "52"),
    ("Contrast Low Width", "_tn_lcon_w", "float", "0x3F000000", 0.0, 2.0, 0.02, "53"),
    ("Creative White", "_cwp", "float", 2.0, 0.0, 5.0, 1.0, "54,126"),
    ("Creative White Limit", "_cwp_lm", "float", "0x3E800000", 0.0, 1.0, 0.01, "55,127"),
    ("Render Space Strength", "_rs_sa", "float", "0x3EB33333", 0.0, 0.6, 0.006, "56"),
    ("Render Space Weight R", "_rs_rw", "float", "0x3E800000", 0.0, 0.8, 0.008, "57"),
    ("Render Space Weight B", "_rs_bw", "float", "0x3F0CCCCD", 0.0, 0.8, 0.008, "58"),
    ("Enable Purity Compress High", "_pt_enable", "bool", 1, 0, 1, 1, "59"),
    ("Purity Limit Low", "_pt_lml", "float", "0x3E800000", 0.0, 1.0, 0.01, "60"),
    ("Purity Limit Low R", "_pt_lml_r", "float", "0x3F000000", 0.0, 1.0, 0.01, "61"),
    ("Purity Limit Low G", "_pt_lml_g", "float", "0x00000000", 0.0, 1.0, 0.01, "62"),
    ("Purity Limit Low B", "_pt_lml_b", "float", "0x3DCCCCCD", 0.0, 1.0, 0.01, "63"),
    ("Purity Limit High", "_pt_lmh", "float", "0x3E800000", 0.0, 1.0, 0.01, "64"),
    ("Purity Limit High R", "_pt_lmh_r", "float", "0x3F000000", 0.0, 1.0, 0.01, "65"),
    ("Purity Limit High B", "_pt_lmh_b", "float", "0x00000000", 0.0, 1.0, 0.01, "66"),
    ("Enable Purity Softclip", "_ptl_enable", "bool", 1, 0, 1, 1, "67"),
    ("Purity Softclip C", "_ptl_c", "float", "0x3D75C28F", 0.0, 0.25, 0.0025, "68"),
    ("Purity Softclip M", "_ptl_m", "float", "0x3DA3D70A", 0.0, 0.25, 0.0025, "69"),
    ("Purity Softclip Y", "_ptl_y", "float", "0x3D75C28F", 0.0, 0.25, 0.0025, "70"),
    ("Enable Mid Purity", "_ptm_enable", "bool", 1, 0, 1, 1, "71"),
    ("Mid Purity Low", "_ptm_low", "float", "0x3ECCCCCD", 0.0, 2.0, 0.02, "72"),
    ("Mid Purity Low Range", "_ptm_low_rng", "float", "0x3E800000", 0.0, 1.0, 0.01, "73"),
    ("Mid Purity Low Strength", "_ptm_low_st", "float", "0x3F000000", 0.1, 1.0, 0.01, "74"),
    ("Mid Purity High", "_ptm_high", "float", "0xBF4CCCCD", -0.9, 0.0, 0.01, "75"),
    ("Mid Purity High Range", "_ptm_high_rng", "float", "0x3EB33333", 0.0, 1.0, 0.01, "76"),
    ("Mid Purity High Strength", "_ptm_high_st", "float", "0x3ECCCCCD", 0.1, 1.0, 0.01, "77"),
    ("Enable Brilliance", "_brl_enable", "bool", 1, 0, 1, 1, "78"),
    ("Brilliance", "_brl", "float", "0x00000000", -6.0, 2.0, 0.08, "79"),
    ("Brilliance R", "_brl_r", "float", "0xC0200000", -6.0, 2.0, 0.08, "80"),
    ("Brilliance G", "_brl_g", "float", "0xBFC00000", -6.0, 2.0, 0.08, "81"),
    ("Brilliance B", "_brl_b", "float", "0xBFC00000", -6.0, 2.0, 0.08, "82"),
    ("Brilliance Range", "_brl_rng", "float", "0x3F000000", 0.0, 1.0, 0.01, "83"),
    ("Brilliance Strength", "_brl_st", "float", "0x3EB33333", 0.0, 1.0, 0.01, "84"),
    ("Enable Post Brilliance", "_brlp_enable", "bool", 1, 0, 1, 1, "85"),
    ("Brilliance Post", "_brlp", "float", "0xBF000000", -1.0, 0.0, 0.01, "86"),
    ("Post Brilliance R", "_brlp_r", "float", "0xBFA00000", -3.0, 0.0, 0.03, "87"),
    ("Post Brilliance G", "_brlp_g", "float", "0xBFA00000", -3.0, 0.0, 0.03, "88"),
    ("Post Brilliance B", "_brlp_b", "float", "0xBE800000", -3.0, 0.0, 0.03, "89"),
    ("Enable Hue Contrast", "_hc_enable", "bool", 1, 0, 1, 1, "90"),
    ("Hue Contrast R", "_hc_r", "float", "0x3F800000", 0.0, 2.0, 0.02, "91"),
    ("Hue Contrast R Range", "_hc_r_rng", "float", "0x3E99999A", 0.0, 1.0, 0.01, "92"),
    ("Enable Hueshift RGB", "_hs_rgb_enable", "bool", 1, 0, 1, 1, "93"),
    ("Hueshift R", "_hs_r", "float", "0x3F19999A", 0.0, 1.0, 0.01, "94"),
    ("Hueshift R Range", "_hs_r_rng", "float", "0x3F19999A", 0.0, 2.0, 0.02, "95"),
    ("Hueshift G", "_hs_g", "float", "0x3EB33333", 0.0, 1.0, 0.01, "96"),
    ("Hueshift G Range", "_hs_g_rng", "float", "0x3F800000", 0.0, 2.0, 0.02, "97"),
    ("Hueshift B", "_hs_b", "float", "0x3F28F5C3", 0.0, 1.0, 0.01, "98"),
    ("Hueshift B Range", "_hs_b_rng", "float", "0x3F800000", 0.0, 4.0, 0.02, "99"),
    ("Enable Hueshift CMY", "_hs_cmy_enable", "bool", 1, 0, 1, 1, "100"),
    ("Hueshift C", "_hs_c", "float", "0x3E800000", 0.0, 1.0, 0.01, "101"),
    ("Hueshift C Range", "_hs_c_rng", "float", "0x3F800000", 0.0, 1.0, 0.01, "102"),
    ("Hueshift M", "_hs_m", "float", "0x00000000", 0.0, 1.0, 0.01, "103"),
    ("Hueshift M Range", "_hs_m_rng", "float", "0x3F800000", 0.0, 1.0, 0.01, "104"),
    ("Hueshift Y", "_hs_y", "float", "0x00000000", 0.0, 1.0, 0.01, "105"),
    ("Hueshift Y Range", "_hs_y_rng", "float", "0x3F800000", 0.0, 1.0, 0.01, "106"),
    ("HDR Purity", "pt_hdr", "float", "0x3F000000", 0.0, 1.0, 0.01, "19"),
    ("Clamp", "_clamp", "bool", 1, 0, 1, 1, "108"),
]


def ui_to_internal(ui):
    return ui[1:] if ui.startswith("_") else ui


# Panel layout (design §F): (panel name, [ui ids in order], open?, description)
PANELS = [
    ("Exposure & Tonescale",
     ["tn_Lp", "tn_Lg", "tn_gb", "_tn_con", "_tn_sh", "_tn_toe", "_tn_off"],
     True,
     "Tonescale shape: peak/grey display luminance, HDR grey boost, "
     "contrast, shoulder, toe, offset (tn_*; OpenDRT tonescale constraint)."),
    ("Creative White", ["_cwp", "_cwp_lm"], False,
     "Creative white: 0=D93 ... 5=D50 (DCTL stickshift combo maps the socket "
     "directly 0..5; default 2 = D65). Limit scales how far chroma shifts."),
    ("Look (Tonescale Contrast)",
     ["_tn_hcon_enable", "_tn_hcon", "_tn_hcon_pv", "_tn_hcon_st",
      "_tn_lcon_enable", "_tn_lcon", "_tn_lcon_w"],
     False,
     "High/low extra-contrast modules (DCTL look contrast hcon/lcon; both "
     "disabled by default with the Standard look)."),
    ("Saturation", ["_rs_sa", "_rs_rw", "_rs_bw"], False,
     "Render-space saturation strength and per-channel weights (rs_*)."),
    ("Purity Compress",
     ["pt_hdr", "_pt_enable", "_pt_lml", "_pt_lml_r", "_pt_lml_g",
      "_pt_lml_b", "_pt_lmh", "_pt_lmh_r", "_pt_lmh_b", "_ptl_enable",
      "_ptl_c", "_ptl_m", "_ptl_y", "_ptm_enable", "_ptm_low",
      "_ptm_low_rng", "_ptm_low_st", "_ptm_high", "_ptm_high_rng",
      "_ptm_high_st"],
     False,
     "Chroma compression limits, softclip and mid purity (pt_*). HDR Purity "
     "scales the purity fade-in above 100 nits (_pt_hdr drives pt_cmp_Lf)."),
    ("Brilliance",
     ["_brl_enable", "_brl", "_brl_r", "_brl_g", "_brl_b", "_brl_rng",
      "_brl_st", "_brlp_enable", "_brlp", "_brlp_r", "_brlp_g", "_brlp_b"],
     False,
     "Brilliance (hue-windowed exposure shaping) and post-brilliance boost."),
    ("Hue Shifts",
     ["_hc_enable", "_hc_r", "_hc_r_rng", "_hs_rgb_enable", "_hs_r",
      "_hs_r_rng", "_hs_g", "_hs_g_rng", "_hs_b", "_hs_b_rng",
      "_hs_cmy_enable", "_hs_c", "_hs_c_rng", "_hs_m", "_hs_m_rng",
      "_hs_y", "_hs_y_rng"],
     False,
     "Hue contrast and hue shifts (hc_*, hs_*; hue-windowed per channel)."),
    ("Advanced", ["_clamp"], False,
     "Clamp display values to [0, 1] (DCTL 827-828 hardcodes this on for the "
     "official path)."),
]

OUTPUTS = [
    ("Rec.1886", "Rec.1886 - 2.4 Power / Rec.709"),
    ("sRGB Display", "sRGB Display - 2.2 Power / Rec.709"),
    ("Display P3", "Display P3 - 2.2 Power / P3-D65"),
    ("P3-D60", "DCI - 2.6 Power / P3-D60"),
    ("P3-DCI", "DCI - 2.6 Power / P3-DCI"),
    ("DCI P3 XYZ", "DCI - 2.6 Power / XYZ"),
    ("Rec.2100 PQ", "Rec.2100 - PQ / Rec.2020 (P3 Limited)"),
    ("Rec.2100 HLG", "Rec.2100 - HLG / Rec.2020 (P3 Limited)"),
    ("Dolby PQ", "Dolby - PQ / P3-D65"),
]

IMAGE_SOCKET_DESC = (
    "Linear CIE XYZ D65. Values used as-is (no color-management conversion "
    "in-tree). Upstream recipe: Image -> Convert Color Space node (To: "
    "'Linear CIE-XYZ D65'). Measured: the node equals OpenDRT's input "
    "matrices for Linear Rec.709/Rec.2020 (<= ~1e-6). ACES-class caveat: "
    "ACEScg/ACES2065-1 DIVERGE (max_abs ~4e-3, different internal "
    "AP0/AP1->XYZ transforms) - ACES inputs need a baked OpenDRT matrix "
    "converter group.")


def cwp_norm_ladder(gamut):
    """Ladder fires for cwp_norm (§B.6 step 7): [(op, k), ...]."""
    if gamut == 5:
        return [("eq", 1), ("ge", 2)]
    return [("eq", k) for k in (1, 2, 3, 4, 5)]


def cwp_norm_candidates(gamut):
    if gamut == 0:
        return [0.744192699063, 0.873470832146, 1.0, 0.955936992163,
                0.905671332781, 0.850004385027]
    if gamut in (1, 2):
        return [0.762687057298, 0.884054083328, 1.0, 0.964320186739,
                0.92307651886, 0.876572837784]
    if gamut == 3:
        return [0.704956321013, 0.816715709816, 0.923382193663, 1.0,
                0.956138500287, 0.906801453023]
    if gamut == 4:
        return [0.665336141225, 0.770397131382, 0.870572343302,
                0.891354547503, 0.855327825187, 0.814566436117]
    if gamut == 5:
        return [0.707142784007, 0.815561082617, 0.91655527974]
    raise BuildError(f"cwp_norm ladder undefined for gamut {gamut}")


def cat_ladder_candidates(gamut, spec):
    """Design §B.6 step 4: 6 matrix candidates ascending k=0..5."""

    def m(name):
        if name == "IDENTITY":
            return IDENTITY
        return spec["constants"][name]["rows"]

    if gamut in (0, 1, 2):
        return [m("matrix_cat_d65_to_d93"), m("matrix_cat_d65_to_d75"),
                IDENTITY, m("matrix_cat_d65_to_d60"),
                m("matrix_cat_d65_to_d55"), m("matrix_cat_d65_to_d50")]
    if gamut == 3:
        return [m("matrix_cat_d60_to_d93"), m("matrix_cat_d60_to_d75"),
                m("matrix_cat_d60_to_d65"), IDENTITY,
                m("matrix_cat_d60_to_d55"), m("matrix_cat_d60_to_d50")]
    if gamut in (4, 5):
        return [m("matrix_cat_dci_to_d93"), m("matrix_cat_dci_to_d75"),
                m("matrix_cat_dci_to_d65"), m("matrix_cat_dci_to_d60"),
                m("matrix_cat_dci_to_d55"), m("matrix_cat_dci_to_d50")]
    raise BuildError(f"CAT ladder undefined for gamut {gamut}")


def back_matrix_for(gamut, spec):
    """A638-647 back conversion, baked per gamut (§B.6 step 6)."""
    if gamut == 0:
        return spec["constants"]["matrix_xyz_to_rec709"]["rows"]
    if gamut == 5:
        return spec["constants"]["matrix_cat_d65_to_dci"]["rows"]
    if gamut in (1, 2, 3, 4):
        return spec["constants"]["matrix_xyz_to_p3d65"]["rows"]
    raise BuildError(f"back matrix undefined for gamut {gamut}")


def hna(arg):  # "x"/"y"/"z" -> row index
    return {"x": 0, "y": 1, "z": 2}[arg]


class Scope:
    """One walkable statement list (fn body / if-body / region)."""

    def __init__(self, kind):
        self.kind = kind
        self.pending = []  # deferred @ret arms: (fire, value, anchor_stmt)


class Builder:
    """Compiles spec.opendrt-v1.1.0.json into one CompositorNodeTree."""

    def __init__(self, spec, log):
        self.spec = spec
        self.log = log
        self.functions = spec["functions"]
        self.constants = spec["constants"]
        self.pipeline = spec["pipeline"]
        self.params = spec["parameters"]
        self.selection = spec["selection"]
        self.dead_fns = set(spec["reports"]["dead_functions"])
        self.dev_notes = []
        # group-emission context stack; [0] = the main tree. Each entry:
        # {"tree", "links", "gout", "kcount", "prefix", "reads"}
        self.ctx = []
        self.fn_groups = {}     # name -> CompositorNodeTree (built once)
        self.group_used = {}    # tree name -> # of instances made
        self.all_trees = []
        self.fn_ret_shape = {}
        self.dgpw_variants = {}
        self.branch_stack = []
        self.socket_uses = {}
        self.socket_vars = {ui_to_internal(r[1]): r[0] for r in SOCKET_TABLE}
        # D3: keep eotf_hlg's always-computed arm finite (see D3 note)
        self.functions["eotf_hlg"] = self._wrap_hlg_ln_args(
            self.functions["eotf_hlg"])

    @staticmethod
    def _wrap_hlg_ln_args(fn):
        """Rewrite [ln, X] -> [ln, MAXIMUM(X, 1e-6)] inside eotf_hlg."""

        def tx(x):
            if isinstance(x, list):
                if len(x) == 2 and x[0] == "ln":
                    return ["ln", ["max", tx(x[1]), 1e-06]]
                return [x[0]] + [tx(a) for a in x[1:]]
            if isinstance(x, dict):
                d = dict(x)
                for key in ("inits", "branches", "els", "body", "expr",
                            "ops", "cond"):
                    v = d.get(key)
                    if v is None:
                        continue
                    if isinstance(v, list):
                        d[key] = [tx(i) if isinstance(i, (list, dict)) else i
                                  for i in v]
                    elif isinstance(v, dict):
                        d[key] = {k2: tx(v2) if isinstance(v2, list) else v2
                                  for k2, v2 in v.items()}
                return d
            return x

        return {"returns": fn["returns"], "args": fn["args"], "src": fn["src"],
                "body": [tx(s) for s in fn["body"]]}

    # ------------------------------------------------------------------
    # group-emission context: the builder emits into a stack of trees
    # (main tree + per-fn groups + per-branch groups). Values that cross a
    # group boundary are late-bound ("P", varname) heads: attaching one
    # registers a group-input read; finalize_group_inputs() creates the
    # interface sockets and wires them once the read set is exact.
    # ------------------------------------------------------------------

    def push_ctx(self, tree, prefix, out_sockets=()):
        """Emit into `tree`. Creates GroupInput/Output nodes and the
        return-side interface sockets (known up front)."""
        self.all_trees.append(tree)
        gout = tree.nodes.new("NodeGroupOutput")
        gin = tree.nodes.new("NodeGroupInput")
        for name, stype in out_sockets:
            tree.interface.new_socket(name, in_out="OUTPUT",
                                      socket_type=stype)
        self.ctx.append({"tree": tree, "links": tree.links, "gout": gout,
                         "gin": gin, "kcount": {}, "prefix": prefix,
                         "reads": {}})

    @property
    def tree(self):
        return self.ctx[-1]["tree"]
    @property
    def links(self):
        return self.ctx[-1]["links"]

    @property
    def gout(self):
        return self.ctx[-1]["gout"]

    @property
    def gin(self):
        return self.ctx[-1]["gin"]


    def pop_ctx(self):
        if not self.ctx:
            raise BuildError("internal: pop_ctx on empty context")
        return self.ctx.pop()

    def finalize_group_inputs(self):
        """Late-bind ('P', var) reads: create input sockets + wire them."""
        c = self.ctx[-1]
        read_vars = list(c["reads"])
        for var in read_vars:
            c["tree"].interface.new_socket(var, in_out="INPUT",
                                           socket_type="NodeSocketFloat")
        for var, dsts in c["reads"].items():
            for dst in dsts:
                c["links"].new(c["gin"].outputs[var], dst)
        return read_vars

    # ------------------------------------------------------------------
    # constants + naming
    # ------------------------------------------------------------------

    def mat_rows(self, name):
        try:
            c = self.constants[name]
        except KeyError:
            raise BuildError(f"unresolvable constant '{name}'")
        lits = c.get("literals")
        if lits is None:
            return [[np.float32(v) for v in r] for r in c["rows"]]
        return [[f32_from_literal(t) for t in row] for row in lits]

    def scalar_const(self, name):
        c = self.constants.get(name)
        if c is None or c.get("kind") == "m3x3":
            return None
        lits = c.get("literals")
        return f32_from_literal(str(lits[0] if isinstance(lits, list) else lits))

    @staticmethod
    def is_identity(rows):
        return all(abs(float(rows[i][j]) - (1.0 if i == j else 0.0)) == 0.0
                   for i in range(3) for j in range(3))

    def next_k(self, stmt):
        kc = self.ctx[-1]["kcount"]
        key = (self.branch_stack[-1] if self.branch_stack else SHARED_TAG,
               str(stmt.get("src", "?")), stmt.get("k"))
        k = kc.get(key, 0) + 1
        kc[key] = k
        return k

    def _mk(self, stmt, idname, label=None):
        node = self.tree.nodes.new(idname)
        k = self.next_k(stmt)
        node.name = (f"{self.ctx[-1]['prefix']} "
                     f"r{src_first(stmt.get('src', '0'))}#{k}")
        if label:
            node.label = label
        return node

    def attach(self, dst, head):
        if head[0] == "c":
            v = head[1]
            dst.default_value = (float(v) if not isinstance(v, (list, tuple))
                                 else [float(x) for x in v])
        elif head[0] == "P":
            self.ctx[-1]["reads"].setdefault(head[1], []).append(dst)
        else:
            self.links.new(head[1], dst)

    # ------------------------------------------------------------------
    # const folding - pure, emission-free
    # ------------------------------------------------------------------

    def fold(self, env, e):
        """('c', np.float32) | ('v', [x, y, (z)]) | ('m', rows) | None."""
        if isinstance(e, (int, float)):
            return ("c", np.float32(e))
        if isinstance(e, str):
            v = env.get(e)
            if v is None:
                sc = self.scalar_const(e)
                if sc is not None:
                    return ("c", sc)
                if e.startswith("matrix_"):
                    return ("m", self.mat_rows(e))
                return None
            if v is UNASSIGNED or v is None:
                return None
            if isinstance(v, tuple):
                return ("m", v[1]) if v[0] == "m" else v
            if isinstance(v, dict):
                return self.fold_vecdict(v)
            return None
        if not isinstance(e, list):
            return None
        op, *rest = e
        kids = [self.fold(env, k) for k in rest]
        if any(k is None for k in kids):
            return None
        if op == "attr":
            base, comp = rest
            if kids[0][0] == "m":
                return ("v", [np.float32(v) for v in kids[0][1][hna(comp)]])
            if kids[0][0] == "v" and len(kids[0][1]) > hna(comp):
                return ("c", kids[0][1][hna(comp)])
            return None
        if op == "m3x3":
            try:
                return ("m", [list(k[1]) for k in kids])
            except Exception:
                return None
        if op == "vdot":
            mk, vk = kids[0], kids[1]
            if mk[0] == "m" and vk[0] == "v":
                if self.is_identity(mk[1]):
                    return vk  # channel alias; float-exact (design D.1)
                out = []
                for r in mk[1]:
                    out.append(f32_lit("add", f32_lit("mul", r[0], vk[1][0]),
                                       f32_lit("add",
                                               f32_lit("mul", r[1], vk[1][1]),
                                               f32_lit("mul", r[2], vk[1][2]))))
                return ("v", out)
            return None
        if op == "cast_float":
            return kids[0]
        if op == "cond" and len(kids) == 3 and kids[0][0] == "c":
            return kids[1] if float(kids[0][1]) != 0.0 else kids[2]
        if op in ("f2", "f3") and len(kids) == int(op[1]):
            return ("v", [k[1] for k in kids])
        if op == "sdivf" and len(kids) == 2 and all(k[0] == "c" for k in kids):
            if float(kids[1][1]) == 0.0:
                return ("c", np.float32(0.0))  # DCTL sdivf: return 0.0f
            return ("c", f32_lit("div", kids[0][1], kids[1][1]))
        if op == "atan2" and len(kids) == 2 and all(k[0] == "c" for k in kids):
            return ("c", np.float32(np.arctan2(float(kids[1][1]),
                                               float(kids[0][1]))))
        if all(k[0] == "c" for k in kids):
            return ("c", f32_lit(op, kids[0][1],
                                 kids[1][1] if len(kids) > 1 else 0.0))
        return None

    def fold_vecdict(self, d):
        vals = []
        for c in "xyz" if "z" in d else "xy":
            h = d.get(c)
            if not (isinstance(h, tuple) and h[0] == "c"):
                return None
            vals.append(np.float32(h[1]))
        return ("v", vals)

    def cond_is_true(self, env, cond):
        f = self.fold(env, cond)
        if f is not None and f[0] == "c":
            return bool(float(f[1]) != 0.0)
        return None

    # ------------------------------------------------------------------
    # expression emission
    #   scalar head: ("c", np.float32) | ("s", bpy NodeSocket)
    #   vector: {"x": head, "y": head, "z": head} ({"x", "y"} for float2)
    # ------------------------------------------------------------------

    def emit(self, env, e, stmt):
        if isinstance(e, str) and e in self.socket_vars:
            self.socket_uses[e] = self.socket_uses.get(e, 0) + 1
        f = self.fold(env, e)
        if f is not None:
            if f[0] == "v":
                return {c: ("c", v) for c, v in zip("xyz", f[1])}
            if f[0] == "m":
                return f
            return f  # ("c", scalar)
        if isinstance(e, (int, float)):
            return ("c", np.float32(e))
        if isinstance(e, str):
            v = env.get(e)
            if v is None:
                if e.startswith("matrix_"):
                    return ("m", self.mat_rows(e))
                raise BuildError(
                    f"unmappable IR: unresolved variable '{e}' (src "
                    f"{stmt.get('src')})")
            if v is UNASSIGNED:
                raise BuildError(
                    f"unmappable IR: variable '{e}' used before any "
                    f"assignment (src {stmt.get('src')})")
            return v
        op, *args = e
        if op == "attr":
            return self.emit_attr(env, e, stmt)
        kids = [self.emit(env, a, stmt) for a in args]
        if any(isinstance(k, dict) for k in kids):
            return self.emit_vec_op(env, op, kids, e, stmt)
        return self.emit_scalar_op(env, op, kids, e, stmt)

    def emit_attr(self, env, e, stmt):
        _, base_e, comp = e
        f = self.fold(env, e)
        if f is not None:
            if f[0] == "m":
                return {c: ("c", v) for c, v in zip("xyz", f[1])}  # row vec
            return f
        base = self.emit(env, base_e, stmt)
        if isinstance(base, dict):
            if comp in base:
                return base[comp]
            raise BuildError(
                f"unmappable IR: attr component '{comp}' absent "
                f"(src {stmt.get('src')})")
        if isinstance(base, tuple) and base[0] == "m":
            return {c: ("c", np.float32(v))
                    for c, v in zip("xyz", base[1][hna(comp)])}
        raise BuildError(
            f"unmappable IR: attr '{comp}' of {base!r} (src "
            f"{stmt.get('src')})")

    def math1(self, stmt, op, a, b=None, label=None, cin2=None):
        n = self._mk(stmt, "ShaderNodeMath", label)
        n.operation = op
        self.attach(n.inputs[0], a)
        if b is not None:
            self.attach(n.inputs[1], b)
        if cin2 is not None:
            self.attach(n.inputs[2], cin2)
        return ("s", n.outputs[0])

    def mix2(self, stmt, fac, ha, hb, label=None):
        n = self._mk(stmt, "ShaderNodeMix", label)
        n.data_type = "FLOAT"
        n.clamp_factor = False
        self.attach(n.inputs[0], fac)
        self.attach(n.inputs["A"], ha)
        self.attach(n.inputs["B"], hb)
        return ("s", n.outputs[0])

    MATH_OPS = {"add": "ADD", "sub": "SUBTRACT", "mul": "MULTIPLY",
                "div": "DIVIDE", "min": "MINIMUM", "max": "MAXIMUM"}

    def emit_scalar_op(self, env, op, kids, e, stmt):
        src = stmt.get("src", "?")
        lbl = f"{op} (src {src})"
        a = kids[0]
        b = kids[1] if len(kids) > 1 else None
        if op in self.MATH_OPS and op != "min" and op != "max":
            return self.math1(stmt, self.MATH_OPS[op], a, b, lbl)
        if op == "add":
            return self.math1(stmt, "ADD", a, b, lbl)
        if op == "sub":
            return self.math1(stmt, "SUBTRACT", a, b, lbl)
        if op == "mul":
            return self.math1(stmt, "MULTIPLY", a, b, lbl)
        if op == "div":
            return self.math1(stmt, "DIVIDE", a, b, lbl)
        if op == "neg":
            return self.math1(stmt, "SUBTRACT", ("c", np.float32(0.0)), a,
                              f"neg (src {src})")
        if op in ("min", "max"):
            return self.math1(stmt, self.MATH_OPS[op], a, b, lbl)
        if op == "abs":
            return self.math1(stmt, "ABSOLUTE", a, label=lbl)
        if op == "sqrt":
            return self.math1(stmt, "SQRT", a, label=lbl)
        if op == "exp":
            return self.math1(stmt, "EXPONENT", a, label=lbl)
        if op == "pow":
            return self.math1(stmt, "POWER", a, b, lbl)
        if op == "fmod":
            return self.math1(stmt, "MODULO", a, b, lbl)
        if op == "atan2":
            return self.math1(stmt, "ARCTAN2", a, b, lbl)  # arg0 -> input0
        if op == "ln":
            n = self._mk(stmt, "ShaderNodeMath", f"ln (src {src})")
            n.operation = "LOGARITHM"
            self.attach(n.inputs[0], a)
            n.inputs[1].default_value = float(E_F32)  # base e (measured)
            return ("s", n.outputs[0])
        if op == "log2":
            n = self._mk(stmt, "ShaderNodeMath", f"log2 (src {src})")
            n.operation = "LOGARITHM"
            self.attach(n.inputs[0], a)
            n.inputs[1].default_value = 2.0
            return ("s", n.outputs[0])
        if op == "lt":
            return self.math1(stmt, "LESS_THAN", a, b, lbl)
        if op == "gt":
            return self.math1(stmt, "GREATER_THAN", a, b, lbl)
        if op == "le":
            gt = self.math1(stmt, "GREATER_THAN", a, b, lbl)
            return self.math1(stmt, "SUBTRACT", ("c", np.float32(1.0)), gt,
                              f"le (src {src})")
        if op == "ge":
            lt = self.math1(stmt, "LESS_THAN", a, b, lbl)
            return self.math1(stmt, "SUBTRACT", ("c", np.float32(1.0)), lt,
                              f"ge (src {src})")
        if op == "eq":
            return self.math1(stmt, "COMPARE", a, b, lbl,
                              cin2=("c", np.float32(0.0)))
        if op == "ne":
            cmp_ = self.math1(stmt, "COMPARE", a, b, lbl,
                              cin2=("c", np.float32(0.0)))
            return self.math1(stmt, "SUBTRACT", ("c", np.float32(1.0)), cmp_,
                              f"ne (src {src})")
        if op == "and":
            return self.math1(stmt, "MULTIPLY", a, b, f"and (src {src})")
        if op == "or":
            return self.math1(stmt, "MAXIMUM", a, b, f"or (src {src})")
        if op == "cond":
            # [cond, c, t, f] -> Mix(factor=c, A=f (false side), B=t) (B-R-4)
            return self.mix2(stmt, kids[0], kids[2], kids[1],
                             f"cond (src {src})")
        if op == "cast_float":
            return a
        if op in ("f2", "f3"):
            return {c: k for c, k in zip("xyz"[:int(op[1])], kids)}
        if op == "vdot":
            return self.emit_vdot_inline(kids, stmt)
        return self.call_fn(env, op, kids, e, stmt)

    def emit_vdot_inline(self, kids, stmt):
        """[vdot, m, v]; group body once (m as per-instance defaults)."""
        mk, vk = kids
        if not (isinstance(mk, tuple) and mk[0] == "m"):
            raise BuildError(
                f"unmappable IR: vdot over a runtime matrix (v1 emits "
                f"constant matrices only) (src {stmt.get('src')})")
        if self.is_identity(mk[1]):
            return vk
        vt = self.fn_groups.get("vdot")
        if vt is None:
            return self.vdot_flat(kids, stmt)
        gn = self._mk(stmt, "CompositorNodeGroup")
        gn.node_tree = vt
        self.group_used[vt.name] = self.group_used.get(vt.name, 0) + 1
        for ri in range(3):
            row = mk[1][ri]
            for ci in range(3):
                self.attach(gn.inputs[f"m{ri}{ci}"],
                            ("c", np.float32(row[ci])))
        for c in "xyz":
            self.attach(gn.inputs[f"v.{c}"], self.comp_head(vk, c))
        return {c: ("s", gn.outputs[f"out.{c}"]) for c in "xyz"}

    def build_vdot_group(self):
        """'OpenDRT fn vdot': 9 MUL + 6 ADD with matrix entries as inputs."""
        tree = bpy.data.node_groups.new("OpenDRT fn vdot",
                                        "CompositorNodeTree")
        self.push_ctx(tree, "g",
                      [(f"out.{c}", "NodeSocketFloat") for c in "xyz"])
        rows = [[("P", f"m{i}{j}") for j in range(3)] for i in range(3)]
        env = {c: ("P", f"v.{c}") for c in "xyz"}
        out = self.vdot_flat(((None, rows), env),
                             {"k": "vdot-group", "src": "323"})
        for c in "xyz":
            self.attach(self.gout.inputs[f"out.{c}"], out[c])
        self.finalize_group_inputs()
        self.pop_ctx()
        self.fn_groups["vdot"] = tree
        self.fn_ret_shape["vdot"] = tuple("xyz")

    def vdot_flat(self, kids, stmt):
        """Constant-matrix vdot: flat 9 MUL + 6 ADD (design §B).
        mk[1] = 3 rows of 3 entries; entry = float (const) or head tuple."""
        mk, vk = kids
        lbl = f"vdot (src {stmt.get('src')})"

        def entry(x):
            return x if isinstance(x, tuple) else ("c", np.float32(x))

        out = {}
        for ri, comp in enumerate("xyz"):
            row = mk[1][ri]
            t0 = self.math1(stmt, "MULTIPLY", self.comp_head(vk, "x"),
                            entry(row[0]), lbl)
            t1 = self.math1(stmt, "MULTIPLY", self.comp_head(vk, "y"),
                            entry(row[1]), lbl)
            t2 = self.math1(stmt, "MULTIPLY", self.comp_head(vk, "z"),
                            entry(row[2]), lbl)
            s = self.math1(stmt, "ADD", t0, t1, lbl)
            s = self.math1(stmt, "ADD", s, t2, lbl)
            out[comp] = s
        return out

    @staticmethod
    def comp_head(child, comp):
        if isinstance(child, dict):
            if comp in child:
                return child[comp]
            return ("c", np.float32(0.0))  # absent float2 channel
        return child

    def emit_vec_op(self, env, op, kids, e, stmt):
        if op == "vdot":
            return self.emit_vdot_inline(kids, stmt)
        if op in ("f2", "f3"):
            return {c: k for c, k in zip("xyz"[:int(op[1])], kids)}
        if op in ("add", "sub", "mul", "div", "min", "max"):
            va, vb = kids[0], kids[1]
            comps = "xyz" if ("z" in (va if isinstance(va, dict) else vb)) \
                else "xy"
            return {c: self.math1(stmt, self.MATH_OPS[op],
                                  self.comp_head(va, c),
                                  self.comp_head(vb, c),
                                  f"{op} (src {stmt.get('src')})")
                    for c in comps}
        if op == "neg":
            return {c: self.math1(stmt, "SUBTRACT", ("c", np.float32(0.0)),
                                  self.comp_head(kids[0], c),
                                  f"neg (src {stmt.get('src')})")
                    for c in kids[0]}
        return self.call_fn(env, op, kids, e, stmt)

    # ------------------------------------------------------------------
    # user functions: emitted once per fn into a group tree; call sites
    # instantiate CompositorNodeGroup nodes. Fns in GROUP_EXEMPT (or with
    # <2 call sites) stay inlined, preserving per-site emission exactly.
    # ------------------------------------------------------------------

    def call_fn(self, env, name, kids, e, stmt):
        if name == "sdivf":
            # D1: one DIVIDE; measured div-by-0 -> 0.0 == DCTL `return 0.0f`
            return self.math1(stmt, "DIVIDE", kids[0], kids[1],
                              f"sdivf (src {stmt.get('src')})")
        if name == "display_gamut_whitepoint":
            return self.emit_dgpw_call(env, kids, e, stmt)
        fg = self.fn_groups.get(name)
        if fg is not None:
            return self.inst_fn(name, fg, kids, stmt)
        return self.inline_fn(env, name, kids, e, stmt)

    def inst_fn(self, name, fgtree, kids, stmt):
        """Group-instance call: wire arg sockets, return `out` head(s)."""
        fn = self.functions[name]
        args = fn.get("args", [])
        if len(args) != len(kids):
            raise BuildError(
                f"unmappable IR: arity mismatch calling {name} "
                f"(src {stmt.get('src')})")
        gn = self._mk(stmt, "CompositorNodeGroup")
        gn.node_tree = fgtree
        self.group_used[fgtree.name] = self.group_used.get(fgtree.name, 0) + 1
        for arg, kid in zip(args, kids):
            nm, t = arg["name"], arg["type"]
            if t in ("float2", "float3"):
                if not isinstance(kid, dict):
                    raise BuildError(
                        f"unmappable IR: arg '{nm}' of {name} expects a "
                        f"vector (src {stmt.get('src')})")
                for c in ("xy" if t == "float2" else "xyz"):
                    self.attach(gn.inputs[f"{nm}.{c}"], kid[c])
            else:
                self.attach(gn.inputs[nm], kid)
        shape = self.fn_ret_shape[name]
        if shape == "float":
            return ("s", gn.outputs["out"])
        return {c: ("s", gn.outputs[f"out.{c}"]) for c in shape}

    def inline_fn(self, env, name, kids, e, stmt):
        """Pre-groups inline path (the original per-site emitter)."""
        fn = self.functions.get(name)
        if fn is None:
            raise BuildError(
                f"unmappable IR: unknown function '{name}' expr={e!r} "
                f"(src {stmt.get('src')})")
        if name in self.dead_fns:
            raise BuildError(
                f"unmappable IR: dead function '{name}' reached "
                f"(src {stmt.get('src')})")
        args = fn.get("args", [])
        if len(args) != len(kids):
            raise BuildError(
                f"unmappable IR: arity mismatch calling {name} "
                f"(src {stmt.get('src')})")
        fenv = {arg["name"]: val for arg, val in zip(args, kids)}
        scope = Scope("fn")
        self.emit_stmt_list(fn["body"], fenv, scope)
        ret = fenv.get("@ret", None)
        if ret is None or ret is UNASSIGNED:
            raise BuildError(
                f"unmappable IR: inlined '{name}' has no return value "
                f"(src {stmt.get('src')})")
        return ret

    # ------------------------------------------------------------------
    # fn group pre-builds: plan (static call-site counts from the emitted
    # regions), then emit every planned body ONCE into its own tree.
    # ------------------------------------------------------------------

    def fn_calls_in_body(self, body):
        """Static IR call counts inside one statement list (no emission)."""
        acc = {}

        def walk_expr(e):
            if isinstance(e, list):
                if e and isinstance(e[0], str) and e[0] in self.functions:
                    acc[e[0]] = acc.get(e[0], 0) + 1
                for a in e[1:]:
                    walk_expr(a)
            elif isinstance(e, dict):
                for v in (e.get("inits") or {}).values():
                    walk_expr(v)
                for br in e.get("branches") or []:
                    walk_expr(br["cond"])
                    for st in br["body"]:
                        walk_stmt(st)
                if e.get("els") is not None:
                    for st in e["els"]:
                        walk_stmt(st)
                if e.get("expr") is not None:
                    walk_expr(e["expr"])
                for op in e.get("ops") or []:
                    walk_expr(op["expr"])

        def walk_stmt(st):
            k = st["k"]
            if k == "declare":
                for v in (st.get("inits") or {}).values():
                    walk_expr(v)
            elif k == "assign":
                for op in st["ops"]:
                    walk_expr(op["expr"])
            elif k == "return":
                if st.get("expr") is not None:
                    walk_expr(st["expr"])
            elif k == "if":
                walk_expr(st)

        for st in body:
            walk_stmt(st)
        return acc

    def plan_fn_groups(self):
        """fn name -> static call count (>= 2), reachable from the emitted
        regions, excluding GROUP_EXEMPT. May over-count (a site statically
        pruned at emission); unused groups are removed after the build."""
        region_counts = {}
        for st in self.pipeline:
            src = src_first(st.get("src", "0"))
            if not (src in CLUSTER_SRC or BRANCH_LO <= src <= BRANCH_HI):
                continue
            if is_crv_stmt(st):
                continue
            for name, cnt in self.fn_calls_in_body([st]).items():
                region_counts[name] = region_counts.get(name, 0) + cnt
        counts = dict(region_counts)
        queue = [n for n in region_counts
                 if n in self.functions and n not in self.dead_fns]
        visited = set(queue)
        while queue:
            name = queue.pop()
            for sub, cnt in self.fn_calls_in_body(
                    self.functions[name]["body"]).items():
                counts[sub] = counts.get(sub, 0) + cnt
                if (sub in self.functions and sub not in visited and
                        sub not in self.dead_fns):
                    visited.add(sub)
                    queue.append(sub)
        return {n: c for n, c in counts.items()
                if c >= 2 and n in self.functions and n not in self.dead_fns
                and n not in GROUP_EXEMPT and n != "vdot"}

    def build_fn_group(self, name):
        """Emit one fn body into 'OpenDRT fn {name}'; args late-bound."""
        if name in self.fn_groups:
            raise BuildError(f"internal: fn group '{name}' built twice")
        fn = self.functions[name]
        if name in self.dead_fns:
            raise BuildError(f"unmappable IR: dead function '{name}' grouped")
        rets = fn.get("returns", "float")
        if rets == "float":
            out_sockets = [("out", "NodeSocketFloat")]
            shape = "float"
        elif rets == "float2":
            out_sockets = [(f"out.{c}", "NodeSocketFloat") for c in "xy"]
            shape = tuple("xy")
        elif rets == "float3":
            out_sockets = [(f"out.{c}", "NodeSocketFloat") for c in "xyz"]
            shape = tuple("xyz")
        else:
            raise BuildError(
                f"unmappable IR: fn '{name}' returns '{rets}', not grouped")
        tree = bpy.data.node_groups.new(f"OpenDRT fn {name}",
                                        "CompositorNodeTree")
        self.fn_ret_shape[name] = shape
        self.push_ctx(tree, "g", out_sockets)
        fenv = {}
        for a in fn.get("args", []):
            t, nm = a["type"], a["name"]
            if t == "float3":
                fenv[nm] = {c: ("P", f"{nm}.{c}") for c in "xyz"}
            elif t == "float2":
                fenv[nm] = {c: ("P", f"{nm}.{c}") for c in "xy"}
            else:
                fenv[nm] = ("P", nm)
        scope = Scope("fn")
        self.emit_stmt_list(fn["body"], fenv, scope)
        ret = fenv.get("@ret", None)
        if ret is None or ret is UNASSIGNED:
            self.pop_ctx()
            raise BuildError(f"unmappable IR: fn '{name}' has no return")
        self.attach_fn_ret(fn, ret, name)
        self.finalize_group_inputs()
        self.pop_ctx()
        self.fn_groups[name] = tree

    def attach_fn_ret(self, fn, ret, name):
        """Wire the fn's @ret head(s) to the group's output interface."""
        rets = fn.get("returns", "float")
        try:
            if rets == "float":
                if isinstance(ret, dict) or (isinstance(ret, tuple) and
                                             ret[0] == "m"):
                    raise BuildError(
                        f"unmappable IR: fn '{name}' returns scalar")
                self.attach(self.gout.inputs["out"], ret)
            else:
                if not isinstance(ret, dict):
                    raise BuildError(
                        f"unmappable IR: fn '{name}' must return the vector")
                for c in "xyz" if rets == "float3" else "xy":
                    if c in ret:
                        self.attach(self.gout.inputs[f"out.{c}"], ret[c])
        except BuildError:
            raise


    # ------------------------------------------------------------------
    # statement walking (§C)
    # ------------------------------------------------------------------

    def emit_stmt_list(self, stmts, env, scope):
        import time as _t
        for st in stmts:
            t0 = _t.perf_counter()
            self.emit_stmt(st, env, scope)
            dt = _t.perf_counter() - t0
            if dt > 0.2:
                print(f"      stmt {st.get('src')} took {dt:.3f}s",
                      flush=True)
        self.close_scope(scope, env)

    def emit_stmt(self, st, env, scope):
        kind = st["k"]
        if kind in ("stage", "select", "skipped_preprocessor",
                    "pass_through_ifdef", "pass_through_if", "call"):
            raise BuildError(
                f"unmappable IR element walked: {kind} (src {st.get('src')})")
        if kind == "declare":
            inits = st.get("inits") or {}
            for name in st["names"]:
                if name in inits:
                    env[name] = self.emit(env, inits[name], st)
                else:
                    env[name] = UNASSIGNED
            return
        if kind == "assign":
            for op in st["ops"]:
                tgt = op["tgt"]
                val = self.emit(env, op["expr"], st)
                if "." in tgt:
                    var, comp = tgt.split(".", 1)
                    cur = env.get(var)
                    if not isinstance(cur, dict):
                        raise BuildError(
                            f"unmappable IR: assign to '{tgt}' of a "
                            f"non-vector (src {st.get('src')})")
                    cur[comp] = val
                else:
                    env[tgt] = val
            return
        if kind == "return":
            expr = st.get("expr")
            if expr is None:
                raise BuildError(
                    f"unmappable IR: null return outside sdivf "
                    f"(src {st.get('src')})")
            env["@ret"] = self.emit(env, expr, st)
            return
        if kind == "if":
            return self.emit_if(st, env, scope)
        raise BuildError(f"unmappable IR element: kind '{kind}' "
                         f"(src {st.get('src')})")

    # -- gated merges (the heart of if emission) --

    def child_delta(self, child, env):
        delta = {}
        for var, val in child.items():
            if var == "@ret":
                continue
            if var not in env:
                continue  # gate-local declare dies with the gate body
            if val is env[var]:
                continue
            if val is UNASSIGNED and env[var] is UNASSIGNED:
                continue
            delta[var] = val
        return delta

    def arm_fire(self, env, cond, prior_fires, st):
        c = self.emit(env, cond, st)
        if not prior_fires:
            return c
        acc = prior_fires[0]
        for f in prior_fires[1:]:
            acc = self.math1(st, "MAXIMUM", acc, f,
                             f"or of fires (src {st.get('src')})")
        notp = self.math1(st, "SUBTRACT", ("c", np.float32(1.0)), acc,
                          f"gate not (src {st.get('src')})")
        return self.math1(st, "MULTIPLY", c, notp,
                          f"gate and (src {st.get('src')})")

    def emit_if(self, st, env, scope):
        branches = st["branches"]
        els = st.get("els")
        fires = []
        arms = []  # (fire, delta, child)
        els_delta = None
        els_child = None
        static_taken = False

        for br in branches:
            ct = self.cond_is_true(env, br["cond"])
            if ct is None:  # live
                child = dict(env)
                self.emit_stmt_list(br["body"], child, Scope("body"))
                fire = self.arm_fire(env, br["cond"], fires, st)
                fires.append(fire)
                arms.append((fire, self.child_delta(child, env), child))
            elif ct:  # static true -> direct merge; later branches dead
                child = dict(env)
                self.emit_stmt_list(br["body"], child, Scope("body"))
                ret = child.get("@ret")
                if ret is not None and ret is not UNASSIGNED:
                    env["@ret"] = ret  # static flow-through
                for var, val in self.child_delta(child, env).items():
                    env[var] = val
                static_taken = True
                break
            # static false -> no nodes

        if els is not None and not static_taken:
            els_child = dict(env)
            self.emit_stmt_list(els, els_child, Scope("body"))
            els_delta = self.child_delta(els_child, env)
        if not arms and els_delta is None:
            return  # fully static-false if: zero nodes

        # @ret (C early-return semantics; see B-R-4 + design §C)
        ret_arms = []
        for fire, delta, child in arms:
            ret = child.get("@ret")
            if ret is not None and ret is not UNASSIGNED:
                ret_arms.append((fire, ret))
        if els_child is not None:
            els_ret = els_child.get("@ret")
            if els_ret is not None and els_ret is not UNASSIGNED:
                ret_arms.append((None, els_ret))
        if ret_arms:
            self.merge_ret(st, env, scope, ret_arms)

        # normal vars
        for var, fire, _ in [(v, f, c) for f, d, c in arms for v in d] + \
                [(v, None, None) for v in (els_delta or {})]:
            pass  # order helper below computes deterministically
        seen = []

        def order(vars_):
            for v in vars_:
                if v not in seen:
                    seen.append(v)
        for _, d, _ in arms:
            order(d.keys())
        if els_delta:
            order(els_delta.keys())
        for var in seen:
            self.merge_var(st, env, var, arms, els_delta)

    def merge_var(self, st, env, var, arms, els_delta):
        """One var's gated merge: two-arm form for if/else, else gated chain."""
        vals = [(fire, delta[var]) for fire, delta, _ in arms if var in delta]
        elsv = els_delta.get(var) if els_delta else None
        prev = env.get(var)
        lbl = f"gate (src {st.get('src')})"
        if not vals and elsv is None:
            return
        if len(vals) == 1 and elsv is not None:
            # full-cover if/else (§B rule): A = els (false side), B = arm
            env[var] = self.merge_val(st, vals[0][0], elsv, vals[0][1], lbl)
            return
        if len(vals) == 1:
            if prev is None or prev is UNASSIGNED:
                if var == "@ret":
                    return  # deferral handled in merge_ret
                raise BuildError(
                    f"unmappable IR: '{var}' assigned inside a gate but "
                    f"undefined outside (src {st.get('src')})")
            env[var] = self.merge_val(st, vals[0][0], prev, vals[0][1], lbl)
            return
        # multi-arm progressive (all-static multi-arms resolve earlier)
        if prev is None or prev is UNASSIGNED:
            raise BuildError(
                f"unmappable IR: multi-arm gate for undefined '{var}' "
                f"(src {st.get('src')})")
        acc = prev
        for fire, v in vals:
            acc = self.merge_val(st, fire, acc, v, lbl)
        if elsv is not None:
            running = arms[0][0]
            for f, _, _ in arms[1:]:
                running = self.math1(st, "MAXIMUM", running, f,
                                     f"or of fires (src {st.get('src')})")
            notp = self.math1(st, "SUBTRACT", ("c", np.float32(1.0)), running,
                              f"gate not (src {st.get('src')})")
            acc = self.merge_val(st, notp, acc, elsv, lbl)
        env[var] = acc

    def merge_val(self, st, fire, a, b, lbl):
        """Mix one/each channel: scalar head A=false-side B=true-side."""
        if isinstance(a, dict) or isinstance(b, dict):
            if not isinstance(a, dict) or not isinstance(b, dict):
                raise BuildError(
                    f"unmappable IR: mixed scalar/vector gate merge "
                    f"(src {st.get('src')})")
            return {c: self.mix2(st, fire, a[c], b[c], lbl) for c in b}
        return self.mix2(st, fire, a, b, lbl)

    def merge_ret(self, st, env, scope, ret_arms):
        """@ret merges: two-arm if/else form, then deferrals."""
        armed = [x for x in ret_arms if x[0] is not None]
        els_arms = [x for x in ret_arms if x[0] is None]
        if els_arms and len(armed) == 1:
            # full-cover single branch + els: one two-arm Mix
            fire0, arm0 = armed[0]
            env["@ret"] = self.merge_val(
                st, fire0, els_arms[0][1], arm0,
                f"fn ret gate (src {st.get('src')})")
            return
        if els_arms:
            # static-els only (no live arms): direct base assignment
            if len(els_arms) != 1 or armed:
                raise BuildError(
                    f"unmappable IR: unsupported @ret arm shape "
                    f"(src {st.get('src')})")
            env["@ret"] = els_arms[0][1]
            return
        for fire, val in armed:
            prev = env.get("@ret")
            if prev is None or prev is UNASSIGNED:
                scope.pending.append((fire, val, st))  # defer to scope close
            else:
                env["@ret"] = self.mix2(st, fire, prev, val,
                                        f"fn ret gate (src {st.get('src')})")

    def close_scope(self, scope, env):
        """Apply deferred @ret arms in order over the scope's base value."""
        if not scope.pending:
            return
        base = env.get("@ret")
        base = None if base in (None, UNASSIGNED) else base
        for fire, val, st in reversed(scope.pending):
            if base is None:
                base = val
            else:
                base = self.mix2(st, fire, base, val,
                                 f"fn ret gate (src {st.get('src')})")
        env["@ret"] = base
        scope.pending = []

    # ------------------------------------------------------------------
    # display_gamut_whitepoint inline (§B.6)
    # ------------------------------------------------------------------

    def emit_dgpw_call(self, env, kids, e, stmt):
        """[dgpw, ...]: fold the baked gamut, call its variant group."""
        f_g = self.fold(env, e[4])
        if f_g is None or f_g[0] != "c":
            raise BuildError(
                "unmappable IR: dgpw display_gamut is not a baked constant "
                f"(src {stmt.get('src')})")
        gamut = int(float(f_g[1]))
        vt = self.dgpw_variants.get(gamut)
        if vt is None:
            raise BuildError(
                f"unmappable IR: no dgpw variant group for gamut {gamut}")
        gn = self._mk(stmt, "CompositorNodeGroup")
        gn.node_tree = vt
        self.group_used[vt.name] = self.group_used.get(vt.name, 0) + 1
        for c in "xyz":
            self.attach(gn.inputs[f"rgb.{c}"], self.comp_head(kids[0], c))
        self.attach(gn.inputs["tsn"], kids[1])
        self.attach(gn.inputs["cwp_lm"], kids[2])
        self.attach(gn.inputs["cwp"], kids[4])
        return {c: ("s", gn.outputs[f"out.{c}"]) for c in "xyz"}

    def build_dgpw_variants(self, gamuts):
        for gamut in sorted(gamuts):
            name = f"OpenDRT fn display_gamut_whitepoint g{gamut}"
            tree = bpy.data.node_groups.new(name, "CompositorNodeTree")
            self.push_ctx(tree, "g",
                          [(f"out.{c}", "NodeSocketFloat") for c in "xyz"])
            env = {"rgb": {c: ("P", f"rgb.{c}") for c in "xyz"},
                   "tsn": ("P", "tsn"),
                   "cwp_lm": ("P", "cwp_lm"),
                   "cwp": ("P", "cwp")}
            stmt = {"k": "call", "src": "593"}
            out = self.emit_dgpw_body(env, gamut, stmt)
            for c in "xyz":
                self.attach(self.gout.inputs[f"out.{c}"], out[c])
            self.finalize_group_inputs()
            self.pop_ctx()
            self.dgpw_variants[gamut] = tree

    def emit_dgpw_body(self, env, gamut, stmt):
        """dgpw interior; rgb/tsn/cwp_lm/cwp from env; gamut baked per variant."""
        rgb_h = env["rgb"]
        tsn_h = env["tsn"]
        cwplm_h = env["cwp_lm"]
        cwp_h = env["cwp"]
        src = stmt.get("src", "?")
        label = "display_gamut_whitepoint (src 593-732)"

        def vdot(mrows, v):
            return self.emit_vdot_inline((("m", mrows), v), stmt)

        # 1. A598: rgb = vdot(matrix_p3d65_to_xyz, rgb)
        rgb = vdot(self.mat_rows("matrix_p3d65_to_xyz"), rgb_h)
        # 2. D601: cwp_neutral = rgb (dci family gets its own, step 5)
        neutral = rgb if gamut in (0, 1, 2, 3) else \
            vdot(self.mat_rows("matrix_cat_dci_to_d65"), rgb)
        # 3. D603: cwp_f = pow(tsn_arg, 2.0 * cwp_lm)
        fac2 = self.math1(stmt, "MULTIPLY", ("c", np.float32(2.0)), cwplm_h,
                          f"cwp_f (src {src})")
        cwp_f = self.math1(stmt, "POWER", tsn_h, fac2,
                           f"cwp_f (src {src})")
        # 4. CAT ladder (baked family, live cwp): shared compares + mixes
        fire_specs = [("eq", k) for k in (1, 2, 3, 4, 5)]
        if gamut == 5:
            fire_specs = [("eq", 1), ("ge", 2)]
        cmps = []
        for opf, k in fire_specs:
            if opf == "eq":
                cmps.append(self.math1(
                    stmt, "COMPARE", cwp_h, ("c", np.float32(k)),
                    cin2=("c", np.float32(0.0)), label=f"cwp=={k}"))
            else:
                lt = self.math1(stmt, "LESS_THAN", cwp_h, ("c", np.float32(k)),
                                label=f"cwp>={k}")
                cmps.append(self.math1(stmt, "SUBTRACT",
                                       ("c", np.float32(1.0)), lt,
                                       label=f"cwp>={k}"))
        cands = cat_ladder_candidates(gamut, self.spec)
        # per-element (row, col) ladder: 9 chains x 5 Mix (matrix constants
        # as Mix default inputs, design §B.6)
        mcat = [[None, None, None] for _ in range(3)]
        for i in range(3):
            for j in range(3):
                acc = ("c", np.float32(cands[0][i][j]))
                for bi in range(len(fire_specs)):
                    acc = self.mix2(stmt, cmps[bi], acc,
                                    ("c", np.float32(cands[bi + 1][i][j])),
                                    label=f"CAT ladder (src {src})")
                mcat[i][j] = acc
        # 5. A633: rgb = (mcat . rgb_pre)*cwp_f + neutral*(1 - cwp_f)
        one_cf = self.math1(stmt, "SUBTRACT", ("c", np.float32(1.0)), cwp_f,
                            f"cwp_f (src {src})")
        blended = {}
        for ci, c in enumerate("xyz"):
            t0 = self.math1(stmt, "MULTIPLY", self.comp_head(rgb, "x"),
                            mcat[ci][0], label)
            t1 = self.math1(stmt, "MULTIPLY", self.comp_head(rgb, "y"),
                            mcat[ci][1], label)
            t2 = self.math1(stmt, "MULTIPLY", self.comp_head(rgb, "z"),
                            mcat[ci][2], label)
            s1 = self.math1(stmt, "ADD", t0, t1, label)
            s2 = self.math1(stmt, "ADD", s1, t2, label)
            blended[c] = self.math1(
                stmt, "ADD",
                self.math1(stmt, "MULTIPLY", s2, cwp_f, label),
                self.math1(stmt, "MULTIPLY", neutral[c], one_cf, label),
                label)
        rgb = blended
        # 6. back conversion (baked matrix per gamut)
        rgb = vdot(back_matrix_for(gamut, self.spec), rgb)
        # 7. cwp_norm ladder (shares the compares)
        cnd = cwp_norm_candidates(gamut)
        accn = ("c", np.float32(cnd[0]))
        for bi, (opf, k) in enumerate(fire_specs):
            val = cnd[bi + 1] if bi + 1 < len(cnd) else cnd[-1]
            accn = self.mix2(stmt, cmps[bi], accn, ("c", np.float32(val)),
                             label=f"cwp_norm (src {src})")
        cwp_norm = accn
        # 8. A729: rgb = rgb * ((cwp_norm*cwp_f + 1.0) - cwp_f)
        f1 = self.math1(stmt, "MULTIPLY", cwp_norm, cwp_f,
                        label=f"cwp_norm (src {src})")
        f2 = self.math1(stmt, "ADD", f1, ("c", np.float32(1.0)),
                        label=f"cwp_norm (src {src})")
        f3 = self.math1(stmt, "SUBTRACT", f2, cwp_f,
                        label=f"cwp_norm (src {src})")
        out = {c: self.math1(stmt, "MULTIPLY", rgb[c], f3,
                             label=f"cwp_norm (src {src})")
               for c in "xyz"}
        return out

    # ------------------------------------------------------------------
    # inventory cross-check (doc §E + §F vs spec)
    # ------------------------------------------------------------------

    def cross_check_inventory(self):
        look = self.selection["look"]["entries"][0]
        if look["name"] != "Standard":
            raise BuildError("expected the Standard look as entry 0")
        issues = []
        for name, ui, kind, default, mn, mx, step, srcs in SOCKET_TABLE:
            p = self.params.get(ui)
            if p is None:
                issues.append(f"unknown param id {ui}")
                continue
            if p.get("label") != name:
                issues.append(f"label mismatch {ui}: doc '{name}' vs spec "
                              f"'{p.get('label')}'")
            mode = p["modes"].get("stickshift") or p["modes"].get("active")
            if kind == "float":
                if ui in ("tn_Lp", "tn_Lg", "tn_gb", "pt_hdr"):
                    want = float(p["modes"]["active"]["default"])
                elif ui == "_clamp":
                    want = float(p["modes"]["stickshift"]["default"])
                else:
                    intern = ui_to_internal(ui)
                    if intern not in look["values"]:
                        issues.append(f"{ui} missing from Standard look")
                        want = None
                    else:
                        want = float(look["values"][intern])
                if want is not None:
                    got = f32_from_hex(default) if isinstance(default, str) \
                        else np.float32(default)
                    if got != np.float32(want):
                        issues.append(
                            f"default mismatch {ui}: table {hex_of_f32(got)} "
                            f"vs want {want} ({hex_of_f32(want)})")
                if mode is not None and "min" in mode and "max" in mode and (
                        float(mode["min"]) != float(mn) or
                        float(mode["max"]) != float(mx)):
                    issues.append(f"min/max mismatch {ui}: doc {mn}/{mx} vs "
                                  f"spec {mode['min']}/{mode['max']}")
            elif kind == "bool":
                want = 1 if ui == "_clamp" else None
                if want is not None:
                    dv = default if isinstance(default, int) else \
                        int(float(str(default)))
                    if dv != want:
                        issues.append(f"default mismatch {ui}: {dv} vs {want}")
        if issues:
            raise BuildError("socket inventory cross-check failed:\n" +
                             "\n".join(f"  - {i}" for i in issues))
        labels = [r[0] for r in SOCKET_TABLE]
        if len(labels) != len(set(labels)):
            dupes = {l for l in labels if labels.count(l) > 1}
            raise BuildError(f"duplicate socket names: {dupes}")
        if len(SOCKET_TABLE) != 69:
            raise BuildError(f"socket table has {len(SOCKET_TABLE)} rows; "
                             f"contract = 69")
        panel_ids = [i for _, ids, _, _ in PANELS for i in ids]
        table_ids = [r[1] for r in SOCKET_TABLE]
        if sorted(panel_ids) != sorted(table_ids):
            raise BuildError("panel layout does not cover the socket table")

    # ------------------------------------------------------------------
    # interface
    # ------------------------------------------------------------------

    def build_interface(self, preset_vals=None, tree=None):
        iface = (tree or self.tree).interface
        i_in = iface.new_socket("Image", in_out="INPUT",
                                socket_type="NodeSocketColor",
                                description=IMAGE_SOCKET_DESC)
        outs = []
        for name, desc in OUTPUTS:
            outs.append(iface.new_socket(
                name, in_out="OUTPUT", socket_type="NodeSocketColor",
                description=f"Target display: {desc}"))
        panels = []
        for pname, ids, open_, desc in PANELS:
            p = iface.new_panel(pname, description=desc,
                                default_closed=not open_)
            panels.append(p)
            for ui in ids:
                row = next(r for r in SOCKET_TABLE if r[1] == ui)
                name, _ui, kind, default, mn, mx, step, srcs = row
                tooltip = self.params[ui].get("tooltip") or ""
                enum_note = " 0=D93 1=D75 2=D65 3=D60 4=D55 5=D50" \
                    if ui == "_cwp" else ""
                rng = "" if kind == "bool" else \
                    f" (range {mn}..{mx}, step {step})"
                s = iface.new_socket(
                    name, in_out="INPUT",
                    socket_type="NodeSocketBool" if kind == "bool"
                    else "NodeSocketFloat",
                    parent=p, description=tooltip + enum_note + rng)
                if kind == "bool":
                    if preset_vals is not None and \
                            ui_to_internal(ui) in preset_vals:
                        dv = int(preset_vals[ui_to_internal(ui)])
                    else:
                        dv = int(default, 16) if isinstance(default, str) \
                            else int(default)
                    s.default_value = bool(dv)
                else:
                    # FACTOR subtype: all float params carry fixed spec
                    # min/max; subtype is UI-only (0.001-step slider, value
                    # not rescaled) - verified round-trip in 5.3 Beta with
                    # out-of-unit ranges (100..1000) unclamped.
                    s.subtype = "FACTOR"
                    intern = ui_to_internal(ui)
                    if preset_vals is not None and intern in preset_vals:
                        s.default_value = np.float32(preset_vals[intern])
                    else:
                        s.default_value = f32_from_hex(default) if \
                            isinstance(default, str) else np.float32(default)
                    s.min_value = float(mn)
                    s.max_value = float(mx)
        return i_in, outs, panels

    def build_preset_groups(self, ng):
        """One thin wrapper node per official look preset (user decision
        2026-10-26): the main group is the "Base" look; every other look
        becomes a wrapper "OpenDRT 1.1 [look]" containing exactly one
        instance of OpenDRT Base (node named "base"), with every input/
        output socket forwarded one-to-one and the look's chain values as
        the wrapper's own interface defaults. Math-exact: DCTL look chains
        (758-781) set only exposed params, tonescale combos (787-825) set
        merely contrast knobs, "USE LOOK PRESET" is a no-op, and clamp=1
        is hard-coded in presets mode."""
        # spec look entries are keyed by internal param names (tn_con);
        # table ids are underscored (_tn_con) - bridge via ui_to_internal.
        chain_ids = {ui_to_internal(r[1]) for r in SOCKET_TABLE}
        presets = []
        for entry in self.spec["selection"]["look"]["entries"]:
            if entry["name"] == "Base":
                continue
            extra = set(entry["values"]) - chain_ids
            if extra:
                raise BuildError(f"look preset '{entry['name']}' sets "
                                 f"non-node parameters: {sorted(extra)}")
            pre = bpy.data.node_groups.new(PRESET_PREFIX + entry["name"],
                                           "CompositorNodeTree")
            self.build_interface(tree=pre, preset_vals=entry["values"])
            gin = pre.nodes.new("NodeGroupInput")
            inst = pre.nodes.new("CompositorNodeGroup")
            inst.name = "base"
            inst.node_tree = ng
            gout = pre.nodes.new("NodeGroupOutput")
            for it in pre.interface.items_tree:
                if it.item_type != "SOCKET":
                    continue
                if it.in_out == "INPUT":
                    pre.links.new(gin.outputs[it.name], inst.inputs[it.name])
                elif it.in_out == "OUTPUT":
                    pre.links.new(inst.outputs[it.name],
                                  gout.inputs[it.name])
            presets.append(pre)
        return presets

    # ------------------------------------------------------------------
    # graph build
    # ------------------------------------------------------------------

    def separate_image_and_env(self):
        """Shared interior head: SeparateColor at the Image entry (B-R-5)."""
        base = self.pipeline[0]  # declare rgb f3(p_R,p_G,p_B), src 739
        sep = self._mk(base, "CompositorNodeSeparateColor",
                       "rgb = (p_R, p_G, p_B) via SeparateColor (src 739)")
        gin = self.gin
        self.links.new(gin.outputs["Image"], sep.inputs["Image"])
        env = {
            "rgb": {"x": ("s", sep.outputs["Red"]),
                    "y": ("s", sep.outputs["Green"]),
                    "z": ("s", sep.outputs["Blue"])},
            "in_gamut": ("c", np.float32(0.0)),    # XYZ (baked identity)
            "in_oetf": ("c", np.float32(0.0)),     # Linear (baked)
            "crv_enable": ("c", np.float32(0.0)),  # overlay pruned
            "in_to_xyz": ("m", IDENTITY),          # baked (design §D.1)
        }
        for var, sock in self.socket_vars.items():
            env[var] = ("s", gin.outputs[sock])
        return env

    def build_graph(self):
        enc_entries = sorted(self.selection["display_encoding"]["entries"],
                             key=lambda x: x["index"])
        encs = [(e["index"], e["values"]["tn_su"],
                 e["values"]["display_gamut"], e["values"]["eotf"])
                for e in enc_entries]
        if [e[0] for e in encs] != list(range(9)):
            raise BuildError("display_encoding entries are not 0..8")

        # ---- shared cluster (src 943-960 minus the per-branch 948/953) ----
        cluster = [st for st in self.pipeline
                   if src_first(st.get("src", "0")) in CLUSTER_SRC
                   and src_first(st.get("src", "0")) not in
                   CLUSTER_PER_BRANCH]
        print("PHASE: cluster", flush=True)
        shared = self.separate_image_and_env()
        self.emit_stmt_list(cluster, shared, Scope("cluster"))
        cluster_vars = [v for v in shared
                        if v not in self.socket_vars and v != "rgb" and
                        v not in ("in_gamut", "in_oetf", "crv_enable",
                                  "in_to_xyz")]

        # ---- per-encoding branch groups, instantiated in the main tree ----
        for enc, tn_su, gamut, eotf in encs:
            tree, in_vars = self.build_branch_group(
                enc, tn_su, gamut, eotf, cluster_vars)
            gn = self._mk({"k": "branch", "src": "964"},
                          "CompositorNodeGroup")
            gn.node_tree = tree
            self.group_used[tree.name] = self.group_used.get(tree.name, 0) + 1
            for var in in_vars:
                self.attach(gn.inputs[var], self.main_head(shared, var))
            self.links.new(gn.outputs["out"],
                           self.gout.inputs[OUTPUTS[enc][0]])

    def build_branch_group(self, enc, tn_su, gamut, eotf, cluster_vars):
        """One CompositorNodeTree per encoding; live vars late-bound.
        Returns (tree, interface input var names in registration order)."""
        tree = bpy.data.node_groups.new(f"OpenDRT b{enc}",
                                        "CompositorNodeTree")
        self.all_trees.append(tree)
        self.branch_stack.append(enc)
        self.push_ctx(tree, f"b{enc}", [("out", "NodeSocketColor")])
        env = {
            "rgb": {c: ("P", f"rgb.{c}") for c in "xyz"},
            "in_gamut": ("c", np.float32(0.0)),    # XYZ (baked identity)
            "in_oetf": ("c", np.float32(0.0)),     # Linear (baked)
            "crv_enable": ("c", np.float32(0.0)),  # overlay pruned
            "in_to_xyz": ("m", IDENTITY),          # baked (design §D.1)
            "tn_su": ("c", np.float32(tn_su)),
            "display_gamut": ("c", np.float32(gamut)),
            "eotf": ("c", np.float32(eotf)),
        }
        for var in self.socket_vars:
            env[var] = ("P", var)
        for var in cluster_vars:
            env[var] = ("P", var)
        # per-branch ts_p (948): DIVIDE(tn_con, f32(1.0 + f32(su)*0.05f))
        div = f32_lit("add", 1.0,
                      (np.float32(tn_su) * np.float32(0.05)).item())
        st_p = {"k": "ts_p-anchor", "src": "948"}
        self.socket_uses["tn_con"] = self.socket_uses.get("tn_con", 0) + 1
        env["ts_p"] = self.math1(
            st_p, "DIVIDE", env["tn_con"],
            ("c", np.float32(div)), label="ts_p (src 948)")
        # per-branch ts_dsc (953): 0.01 (PQ), 0.1 (HLG), else 100/tn_Lp
        st_d = {"k": "ts_dsc-anchor", "src": "953"}
        if eotf == 4:
            env["ts_dsc"] = ("c", np.float32(f32_from_hex("0x3C23D70A")))
        elif eotf == 5:
            env["ts_dsc"] = ("c", np.float32(f32_from_hex("0x3DCCCCCD")))
        else:
            self.socket_uses["tn_Lp"] = \
                self.socket_uses.get("tn_Lp", 0) + 1
            env["ts_dsc"] = self.math1(
                st_d, "DIVIDE", ("c", np.float32(100.0)),
                env["tn_Lp"], label="ts_dsc (src 953)")
        branch_stmts = [st for st in self.pipeline
                        if BRANCH_LO <= src_first(st.get("src", "0")) <=
                        BRANCH_HI and not is_crv_stmt(st)]
        main_stmts = [st for st in branch_stmts
                      if src_first(st.get("src", "0")) < TAIL_LO]
        tail_stmts = [st for st in branch_stmts
                      if src_first(st.get("src", "0")) >= TAIL_LO]
        print(f"  b{enc} main {len(main_stmts)} stmts", flush=True)
        self.emit_stmt_list(main_stmts, env, Scope("branch"))
        self.emit_stmt_list(tail_stmts, env, Scope("tail"))
        ret = env.get("@ret")
        if ret is None or ret is UNASSIGNED:
            raise BuildError(f"branch {enc}: no return value")
        if not isinstance(ret, dict):
            raise BuildError(f"branch {enc}: return is not a vector")
        cc = self._mk({"k": "combine", "src": "1210"},
                      "CompositorNodeCombineColor",
                      f"return rgb (src 1210)")
        cc.mode = "RGB"
        self.attach(cc.inputs["Red"], ret["x"])
        self.attach(cc.inputs["Green"], ret["y"])
        self.attach(cc.inputs["Blue"], ret["z"])
        cc.inputs["Alpha"].default_value = 1.0
        self.links.new(cc.outputs[0], self.gout.inputs["out"])
        in_vars = self.finalize_group_inputs()
        self.pop_ctx()
        self.branch_stack.pop()
        return tree, in_vars

    @staticmethod
    def main_head(env, var):
        """Resolve a deferred group-input var against the main-tree env."""
        if "." in var:
            base, comp = var.split(".", 1)
            if isinstance(env.get(base), dict) and comp in env[base]:
                return env[base][comp]
        if var in env:
            return env[var]
        raise BuildError(
            f"unmappable IR: branch input '{var}' unresolvable in the "
            f"main tree")

    # ------------------------------------------------------------------
    # layout + audits + write
    # ------------------------------------------------------------------


    def audit(self):
        issues = []
        for var in self.socket_vars:
            if self.socket_uses.get(var, 0) == 0:
                # spec facts: `_pt_enable` is assigned in the preset chain but
                # never read by the v1.1.0 transform (verified vs the DCTL:
                # only line 59/746/759+ mentions). The socket stays exposed
                # per design §E; its reads are simply absent upstream.
                self.dev_notes.append(
                    f"parameter '{var}' is exposed but unread by the "
                    "transform (defunct in DCTL v1.1.0; socket kept per "
                    "design §E)")
        # R-14 (all trees): no Color (RGBA) socket may feed Math / float-Mix
        for tree in self.all_trees:
            for lk in tree.links:
                fst = lk.from_socket.type
                tnode = lk.to_node.bl_idname
                if fst == "RGBA" and tnode == "ShaderNodeMath":
                    issues.append(f"color feeds math: {tree.name}."
                                  f"{lk.to_node.name}")
                if fst == "RGBA" and tnode == "ShaderNodeMix":
                    issues.append(f"color feeds mix-float: {tree.name}."
                                  f"{lk.to_node.name}")
            names = [n.name for n in tree.nodes if "#" in n.name]
            if len(names) != len(set(names)):
                issues.append(f"duplicate node names in tree '{tree.name}'")
        main_tree = self.ctx[0]["tree"]
        for name, _ in OUTPUTS:
            got = len([l for l in main_tree.links
                       if l.to_node.bl_idname == "NodeGroupOutput" and
                       l.to_socket.name == name])
            if got != 1:
                issues.append(f"output '{name}' fed {got} times")
        return issues


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="spec/opendrt-v1.1.0.json")
    ap.add_argument("--out", default="dist/OpenDRT_v1.1.0.blend")
    args = ap.parse_args(argv)
    print("bpy", bpy.app.version_string, flush=True)
    if tuple(bpy.app.version)[:3] != (5, 2, 3):
        print(f"WARNING: expected bpy 5.2.3 wheel, got {bpy.app.version[:3]}",
              flush=True)
    spec_path = Path(args.spec)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    spec = json.loads(spec_path.read_text())
    spec_sha = hashlib.sha256(spec_path.read_bytes()).hexdigest()

    b = Builder(spec, print)
    print("PHASE: cross-check", flush=True)
    b.cross_check_inventory()
    look_vals = {e["name"]: e["values"] for e in
                 spec["selection"]["look"]["entries"]}
    base_vals = look_vals["Base"]
    ng = bpy.data.node_groups.new(GROUP_NAME, "CompositorNodeTree")
    b.push_ctx(ng, "bS")
    print("PHASE: interface", flush=True)
    # The main group defaults to the official "Base" look (user decision
    # 2026-10-26; replaces the Standard-preset defaults). The Base chain
    # sets only exposed params (DCTL 779-781), so this is a pure interface
    # change; the 5 non-chain params keep their table defaults.
    b.build_interface(preset_vals=base_vals)
    print("PHASE: fn-groups", flush=True)
    plan = b.plan_fn_groups()
    for name in plan:
        b.build_fn_group(name)
        print(f"  fn {name} group: {len(b.fn_groups[name].nodes)} nodes",
              flush=True)
    b.build_vdot_group()
    enc_entries = sorted(spec["selection"]["display_encoding"]["entries"],
                         key=lambda x: x["index"])
    b.build_dgpw_variants(
        {int(e["values"]["display_gamut"]) for e in enc_entries})
    print("PHASE: graph", flush=True)
    b.build_graph()
    issues = b.audit()
    print("PHASE: audited", flush=True)
    if issues:
        print("Error: audit failed: " + "; ".join(issues))
    for note in b.dev_notes:
        print(f"NOTE: {note}", flush=True)
    # Official preset nodes (user decision 2026-10-26): every look preset
    # other than Base becomes a separate node group with identical
    # interface/graph and that look's chain values as input defaults
    # (look chains set only exposed params: DCTL 758-781).
    preset_groups = b.build_preset_groups(ng)
    print("preset groups: " + ", ".join(g.name for g in preset_groups),
          flush=True)
    # Full registry as the explicit write set: b.ctx loses popped branch/fn
    # contexts, and IDs reached only via indirect expansion skip the
    # fake_user write flag (measured 2026-10-26: with a ctx-only set the 9
    # "OpenDRT b*" branch groups read use_fake_user=False in 5.3 Beta).
    all_trees = {c["tree"] for c in b.ctx} | set(b.all_trees) | \
        set(b.fn_groups.values()) | set(b.dgpw_variants.values()) | \
        set(preset_groups)
    for tree in list(b.fn_groups.values()) + list(b.dgpw_variants.values()):
        if b.group_used.get(tree.name, 0) == 0:
            b.dev_notes.append(f"removing unused group '{tree.name}'")
            bpy.data.node_groups.remove(tree)
            all_trees.discard(tree)
    # Asset hygiene: the 8 user-facing groups (main "Base" + the 7 preset
    # wrappers) are the assets; branch/vdot/fn/dgpw groups stay internal
    # plumbing, deliberately NOT assets. All share one deterministic
    # catalog uuid5 ("OpenDRT").
    catalog_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "blender-opendrt"))
    for group, look_name in ([(ng, "Base")] +
                             [(g, g.name.removeprefix(PRESET_PREFIX))
                              for g in preset_groups]):
        group.asset_mark()
        group.use_fake_user = True
        group.asset_data.catalog_id = catalog_id
        preset_note = (f", {look_name} look preset (official OpenDRT "
                       "preset; thin wrapper node: one OpenDRT 1.1 Base "
                       "instance inside with every input forwarded 1:1 and "
                       "every look-chain control defaulted to this "
                       "preset's DCTL chain values)"
                       if look_name != "Base" else
                       ", Base look preset (official OpenDRT 'Base'; all "
                       "look-chain controls defaulted to the Base DCTL "
                       "chain values. Every official look is also available "
                       "as a separate preset node 'OpenDRT 1.1 [look]')")
        group.asset_data.description = (
            "OpenDRT v1.1.0 display transform as a native Blender compositor "
            f"node group{preset_note}; 9 output branches (one per display "
            "encoding; select by linking an output), 1 image input (linear "
            "CIE XYZ D65) + 69 parameter sockets. Provenance: spec sha256 "
            f"{spec_sha}; source DCTL sha256 {spec['provenance']['sha256']}; "
            f"tag {spec['provenance']['tag']} commit "
            f"{spec['provenance']['commit'][:8]}.")
    # fake_user=True: the fake-user bit must be written by libraries.write
    # itself (5.3 Beta reads use_fake_user=False otherwise). The historical
    # open_mainfile/save_as_mainfile re-save workaround also re-embedded
    # factory startup data (workspaces, scene, WinMan) - a pure library
    # .blend must contain only the node groups.
    bpy.data.libraries.write(str(out_path), datablocks=all_trees,
                             compress=True, path_remap="NONE", fake_user=True)
    print(f"size={out_path.stat().st_size} bytes (single libraries.write)",
          flush=True)
    # Catalog definition sidecar at the asset-library root (Blender
    # AssetCatalogService::DEFAULT_CATALOG_FILENAME); header/grammar mirrors
    # AssetCatalogDefinitionFile (VERSION 1, "uuid:path:simple_name").
    out_path.parent.joinpath("blender_assets.cats.txt").write_text(
        "# This is an Asset Catalog Definition file for Blender.\n"
        "#\n"
        "# Empty lines and lines starting with `#` will be ignored.\n"
        "# The first non-ignored line should be the version indicator.\n"
        "# Other lines are of the format \"UUID:catalog/path/for/assets:"
        "simple catalog name\"\n"
        "\n"
        "VERSION 1\n"
        "\n"
        f"{catalog_id}:OpenDRT:OpenDRT\n")
    print(f"catalog {catalog_id} 'OpenDRT' -> blender_assets.cats.txt",
          flush=True)
    print(f"spec sha256 {spec_sha}", flush=True)
    print(f"DCTL sha256 {spec['provenance']['sha256']}", flush=True)
    print(f"groups written: {len(bpy.data.node_groups)}", flush=True)
    return 0




if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        print(f"BUILD_ERROR: {e}", file=sys.stderr)
        sys.exit(1)