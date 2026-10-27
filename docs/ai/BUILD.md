# BUILD.md — pixi tasks + how to build the assets

Everything this repo generates lives under `build/` (gitignored). Tracked
inputs: `reference/OpenDRT_v1.1.0.dctl` (upstream source of truth),
`tools/` (converter, builder, smoke gate, probes, DCTL shim),
`docs/index.html` (landing-page source).

```
reference/OpenDRT_v1.1.0.dctl
  --(pixi run generate-spec)-->   build/spec/opendrt-v1.1.0.json      [IR]
  --(pixi run build-nodes)-->     build/online-asset-pack/            [web root]
      .blend + blender_assets.cats.txt + _asset-library-meta.json
      + _v1/asset-index.json + index.html
  --(pixi run build-local-pack)--> build/local-pack/OpenDRT_v1.1.0/
      .blend + blender_assets.cats.txt
  --(pixi run gen-goldens)-->     build/golden/enc<N>/... + index.json [oracle data]
```

Two artifact packs:

- **online-asset-pack** (`build/online-asset-pack/`) — what GitHub Pages
  deploys (`.github/workflows/deploy-asset-pack.yml`): a REMOTE asset
  library (blend + catalog sidecar + the remote-listing files
  `_asset-library-meta.json` / `_v1/asset-index.json` + the landing page
  copied from `docs/index.html`). Built by default; omit nothing.
- **local-pack** (`build/local-pack/OpenDRT_v1.1.0/`) — what the rolling
  `latest` GitHub release zips (`.github/workflows/release-local-pack.yml`
  -> `build/opendrt-blend.zip`): a LOCAL asset library (blend + catalog
  sidecar only, `--local`).

## Prerequisites

- pixi (`pixi.toml` pins python 3.13, bpy 5.2.3 wheel, g++ 13, jq).
- For the smoke gate only: a Blender binary (5.3.x expected) in
  `BLENDER_BIN` — tests run against REAL Blender, not the wheel. All other
  tasks run headless in-process with the bpy wheel.
- A writable `bpy.app.cachedir`: the asset-listing generator caches blend
  hashes there; in sandboxes set `XDG_CACHE_HOME` to a writable temp dir
  before `import bpy`.

## Task reference

| task | does | output |
|---|---|---|
| `pixi run generate-spec` | converts the reference DCTL into the semantic IR JSON (embeds `--tag v1.1.0 --commit` provenance; byte-stable) | `build/spec/opendrt-v1.1.0.json` |
| `pixi run build-shim` | compiles the C++ DCTL oracle shim | `tools/dctl_shim/build_dctl_shim` binary |
| `pixi run gen-goldens` | renders the deterministic input sets through the shim, 9 encodings x 3 sets, twice (byte-identical determinism gate) + writes `index.json` with per-file sha256 | `build/golden/` |
| `pixi run build-nodes` | full build: IR -> 33 CompositorNodeTree node groups (main + 7 preset wrappers + internal fn/branch groups), asset marks + catalog + remote listing + landing page | `build/online-asset-pack/` |
| `pixi run build-local-pack` | same emission with `--local` (skips remote listing + landing page) | `build/local-pack/OpenDRT_v1.1.0/` |
| `pixi run check-nodes` | smoke gate: opens the built blend in the real Blender binary and runs `tools/rt_smoke.py` (interface audits, render, shim parity for enc0). Needs `BLENDER_BIN` | console PASS/FAIL report |

Task flags worth knowing:

- `python tools/build_nodes.py --spec PATH --out PATH [--local]` — the
  defaults are the pixi-task paths; `--local` skips remote-listing +
  landing. `OPENDRT_PACK_NAME` / `OPENDRT_PACK_CONTACT_NAME` /
  `OPENDRT_PACK_CONTACT_URL` / `OPENDRT_PACK_CONTACT_EMAIL` override the
  remote-listing metadata.
- `python tools/rt_smoke.py --parity none|enc0|all --shim PATH --blend
  PATH` — `--parity all` runs every display encoding (the full gate used
  before releases; slower).
- `python tools/verify_shim.py [--shim PATH] [--out DIR]` — goldens are
  deterministic: deleting `build/golden/` and re-running must reproduce
  byte-identical bins.
- `python tools/convert_dctl.py --print selection.look` (etc.) — read-only
  spec inspection; never writes.

## Full local build, from a clean checkout

```sh
pixi run generate-spec     # DCTL -> build/spec/opendrt-v1.1.0.json
pixi run build-shim        # C++ oracle shim (gen-goldens depends on this)
pixi run gen-goldens       # deterministic oracle data -> build/golden/
pixi run build-nodes       # online pack -> build/online-asset-pack/
pixi run build-local-pack  # local pack  -> build/local-pack/
BLENDER_BIN=/path/to/blender
"$BLENDER_BIN" -b --factory-startup \
  build/online-asset-pack/OpenDRT_v1.1.0.blend \
  -P tools/rt_smoke.py -- \
  --blend build/online-asset-pack/OpenDRT_v1.1.0.blend --parity all
  # rt_smoke.py runs INSIDE the Blender binary (not `pixi run python`):
  # it imports bpy as the live app and needs the compositor.
pixi run python tools/probes/probe_width.py
pixi run python tools/probes/probe_pt_enable.py
```

Notes:

- `build/spec/opendrt-v1.1.0.json` is never tracked or hand-edited. When
  targeting a new OpenDRT release, bump BOTH `--tag` and `--commit` in the
  `generate-spec` task together.
- The builder emits deterministic MATH (guarded by the spec/DCTL sha256
  checks, `build/golden/` bins, and shim parity), not deterministic blend
  bytes — a same-path rebuild differs in a few bytes inside the blend
  container and that is expected and harmless.
- `default_group_node_width = 240` applies to instances created in the
  Blender UI (Shift+A / asset drag); `bpy.nodes.new()` bypasses that tree
  default in scripts — informational only.