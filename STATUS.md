# STATUS.md

Current state and plans for `blender-opendrt`. Read this before doing anything else.

## Goal

Create an OpenDRT node for the Blender compositor: a node group that re-implements
OpenDRT's display transform with every look-development option exposed as a
tweakable slider, plus the official OpenDRT look presets applied as preset
parameter sets. OpenDRT is release-specific: we target the **v1.1.0** DCTL
exactly, deriving the math from `reference/OpenDRT_v1.1.0.dctl`.

Motivation and rejected alternatives (for context, do NOT re-litigate):

- OCIO configs (e.g. `zicklag/opendrt-1.1-ocio`) are baked per-look/per-encoding
  and are not interactively tweakable in Blender.
- Per-pixel Python in Blender node trees **does not exist and is not planned**
  (Jacques Lucke, devtalk 2025: no plans to evaluate Python during node
  evaluation; Python-defined nodes in the compositor are just wrappers around
  static node groups). So the DCTL math must be compiled into a *static,
  native Blender node tree*.
- The plan: a converter toolchain (this repo) where Python is the *compiler
  and validator*, and the *runtime artifact* is native Blender nodes (GPU
  compositor, full float32, better fidelity than baked 3D LUTs).

## Architecture (committed decision)

```
OpenDRT_v1.1.0.dctl ──(tools/convert_dctl.py)──> spec/opendrt-v1.1.0.json   [DONE]
spec/opendrt-v1.1.0.json ──(node builder, not started)──> Blender node groups
node groups ──(validation, not started)──> checked against oracle + OCIO bakes
```

- **spec/opendrt-v1.1.0.json is the intermediate.** It describes the *transform*
  (parameters / selection tables / constants / functions / pipeline IR), NOT a
  Blender node tree. Blender-specific concerns (node layout, socket types,
  enum baking, preset variants) live in the node builder, which is another
  file that consumes the spec.
- The IR is self-describing: see `spec.ir.statement_kinds` and
  `spec.ir.expression_ops` in the JSON for the exact vocabulary.
- One DCTL -> one JSON. When a new OpenDRT release is targeted, re-run the
  converter with new `--tag/--commit` and `git diff` the spec to review
  semantic changes.

## Current state

### Done and verified

- `reference/`: `OpenDRT_v1.1.0.dctl` + `.nk` + `docs/` from the official
  v1.1.0 release (jedypod/open-display-transform, tag `v1.1.0`, commit
  `8804747def7cdc9089f2b2ea5011596792134304`). Release asset is byte-identical
  to the `af68332` branch commit that `zicklag/opendrt-1.1-ocio` pinned.
  `reference/SOURCE.sha256` carries content hashes.
  - StickShift variant intentionally NOT stored: it is the same file with one
    block un-commented; the converter derives both views.
- `tools/convert_dctl.py`: stdlib-only converter, runs clean under pixi
  (Python 3.13). `pixi run generate-spec` regenerates the spec **byte-stably**
  (output must not depend on time of day - do not re-add timestamp fields).
- `spec/opendrt-v1.1.0.json` output sha256 (whole file):
  `ceee882bb02af3eca673b44a56ec4927fe500e43f6a96c99f893a0aa0ca1d097`.
  Source DCTL sha256: `645ba89745691742c5403f8de5964b5934cd1c59bf06fb1f1a1f6b45f54830a1`.
- Converter verification (all pass, re-run after ANY converter change):
  - line coverage 1211/1211 classified, 0 unclassified (unclassifiable lines
    hard-fail the run),
  - symbol resolution: 0 errors; every identifier/target/call/arity resolves
    (params, constants, function args, locals with C-like block scoping),
  - spot checks vs source: Standard look 64 values, ACES-1.x tonescale,
    9 display encodings, 15 input gamuts -> matrices, creative-white map,
    matrix row literals, HLG/HLG-inverse swizzle statements, `(float)` casts,
    compound assignments, comma-assign preset bodies.
- Spec contents today: 78 parameter ids (12 active / 66 stickshift-only /
  3 dual-mode: `look_preset`, `_cwp`, `_cwp_lm`), 78 tooltips attached,
  35 matrix + 2 scalar constants, 40 functions, 5 selection groups
  (look 8 / tonescale 13 / creative_white 6 / display_encoding 9 /
  input_gamut 15 entries), pipeline 88 statements, 8 dead functions
  (defined but never called in IR: `clampmaxf3, complement_power, fmaxf3,
  fminf3, make_float3x3, sdivf3f3, sigmoid_cubic, softplus_constraint`).

### Not started

- Node builder (spec JSON -> Blender compositor node groups).
- Validation of node output (see Validation plan below).
- Official-look preset assets (per-look default value sets; trivial once the
  builder exists - presets are data in `spec.selection.look`).
- Packaging as asset library / optional Blender extension.

## Facts future agents must know

DCTL quirks (already encoded in spec.notes, verified in source):

- `#line 2` on line 1 is a Resolve logging directive; ignored.
- transform() has `#ifdef INIT_PARAMS` (StickShift: parameter passthrough,
  each param = its `_`-prefixed UI id) / `#else` (preset-driven) / `#endif`.
  The converter keeps the `#else` content as the pipeline and drops the ifdef
  branch (its content IS `spec.parameters`).
- Selection chains were lifted to `spec.selection`; their pipeline positions
  are marked with `k="select"` entries in the original order.
- **Preset application order matters and is preserved**: the tonescale preset
  chains use bare `if` (not else-if) for the first two entries, so later
  statements override earlier ones for shared `tn_*` variables. Within a
  group, entries apply in listed order; groups apply in pipeline order.
- `look_preset` index 7 ("Base") exists only in the StickShift combo (8
  options); the active combo exposes 7. Branch 7 is real and kept.
- `_cwp` semantics differ per mode: active combo has 7 options (index 0 =
  "USE LOOK PRESET", remapped by the creative_white chain to internal cwp
  0..5); stickshift combo has 6 options mapping directly. The
  creative-white `cwp_lm` passthrough (`if (_cwp != 0) cwp_lm = _cwp_lm;`)
  is kept as `rules` in `selection.creative_white`.
- `PI` / `SQRT3` are `__CONSTANT__` globals (collected into `spec.constants`).
- Number literals are emitted as JSON float64 of the C literal text; DCTL
  compiles float32 (`f` suffix). Blender's GPU compositor is float32, so the
  builder MUST cast emitted literals to float32 (or accept <=1ulp-of-float32
  drift from float64 sources).
- The tonescale overlay (`crv_enable` statements, src lines ~925-930 and
  ~1162-1208) is position-dependent (uses `p_X/p_Y`, `pos`, `res`); it is a
  diagnostic, not part of the transform. Builder may skip it (documented as
  the intended deviation) or implement via an Image Coordinates node.
- In the IR, `make_float3x3` is dead only *because* matrix defines are
  extracted into `spec.constants` before body parsing; `vdot(matrix_*_to_xyz, x)`
  call sites reference those constant names. Not a parse bug.

Blender-side facts:

The user's daily-driver Blender is **5.3.0 Beta** (hash 3db45062d058, built
2026-10-19) at `~/bin/blender`. STATUS previously said 5.2 - stale.
The compositor uses unified `ShaderNode*` nodes (ShaderNodeMath,
ShaderNodeVectorMath, ShaderNodeMix, ...) since 5.0. Matrix sockets exist in
geometry nodes (5.1); compositor support in 5.2/5.3 is UNVERIFIED - check
during builder work; the fallback is a flat 9x mul + add per matrix op.

Node-graph generation toolchain (decided, verified 2026-10):

- Builder target: official `bpy==5.2.3` PyPI wheel (Blender as a Python
  module, cp313 == the pixi env's Python 3.13; conda-forge has no bpy
  package). Creates `CompositorNodeTree`s and writes standalone library
  .blends via `bpy.data.libraries.write(path, datablocks={ng})`; set
  `asset_mark()` + `use_fake_user=True` on each group so they survive.
- The real 5.3 Beta binary (headless: `--background --factory-startup
  --python-expr ...`) is the authority for API probes and final validation
  renders; a .blend authored by 5.2.3 loads in 5.3 with asset + interface
  intact (verified). Opening it directly warns "Library file, loading empty
  scene" - expected, it is a library file, consume via Asset Browser/append.
- Version guard: `pixi run smoke-bpy` (`tools/bpy_smoke.py`) re-creates a
  minimal group and round-trips it through the binary.
- Blender view transform must be Raw/Standard around the node (OpenDRT
  already encodes for the display) - avoid double transform.
- Node group interface panels (`NodeTreeInterfacePanel`) give the Nuke-style
  tabbed slider UI; group input sockets = the DCTL's parameters.

## Validation plan (after the builder exists)

1. **g++ compiled-DCTL oracle** (strongest): the DCTL compiles as C++ with a
   small shim. Reuse the approach from `scripts/20-bake-luts.py` +
   `scripts/dctl_shim.h` in `https://github.com/zicklag/opendrt-1.1-ocio`:
   stream RGB through the compiled DCTL with arbitrary `--param value` combos
   and compare against node-group output. This tests the *exact* DCTL
   semantics, including non-preset slider values, at float32.
2. **Cross-check the official preset+encoding combos** against the already
   shipped OCIO bakes (same repo) and Jed's released cubes.
3. **Per-stage checks**: the converter's line refs (`src` fields) map every
   IR statement to DCTL lines; validate the node graph stage-by-stage against
   the same statement boundaries rather than only end-to-end.
4. Adopt tolerance-gate style checks (max-abs-diff per channel, ramp sweeps +
   random HDR cube) as in that repo's `40-validate.py`.

## Commands

- `pixi run generate-spec` - regenerate `spec/opendrt-v1.1.0.json` from the
  reference DCTL (byte-stable; args embed tag/commit - update both together).
- `python tools/convert_dctl.py --print <path>` - print a spec subtree
  (e.g. `--print selection.look`); READ-ONLY: it never writes the spec
  file, so it can be run without --tag/--commit. Regeneration happens only
  via the pixi task (which pins provenance).
- Converter failure modes: parse error prints DCTL line; VERIFY-ERROR lines
  go to stderr; nonzero exit on any unclassified line or unresolved symbol.
  There is no silent-drop path - keep it that way.

## Immediate next steps

1. Review `spec/opendrt-v1.1.0.json` against the DCTL (sampling, plus the
   reports section) - user sign-off on the converter output shape.
2. Node builder: `tools/build_nodes.py` (name TBD) consuming the spec;
   per (input gamut x display encoding) combo, emit one
   `CompositorNodeTree` with interface panels per module. Look/tonescale
   sliders = group input sockets; enums with non-creative semantics baked at
   build time; creative white possibly live via compare/mix ladder.
3. Validation oracle setup (g++ DCTL shim) BEFORE trusting any node output.
4. Preset assets + packaging.

Provenance rule: any committed spec JSON must carry tag + commit + source
sha256 that match the DCTL actually parsed. Do not regenerate lazily and
commit with stale provenance.