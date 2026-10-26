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

### Output side (probed 2026-10-25, bpy 5.2.3 + 5.3.0 Beta)
- Compositor has NO Menu Switch node (`NodeMenuSwitch`/`ShaderNodeMenuSwitch`/
  `CompositorNodeMenuSwitch` all undefined in BOTH 5.2.3 and 5.3.0 Beta);
  matrix multiply nodes also undefined -> matrix sockets unusable for math
  (flat 3x mul+add fallback stands). Available gating: `ShaderNodeMath`
  (COMPARE/EQ), `CompositorNodeSwitch` (bool A/B, Color sockets),
  `ShaderNodeMix` (color).
- Node-group interface accepts `NodeSocketMenu` items, but menu-item lists
  have no Python API and are only populated via Menu Switch propagation in
  geometry trees -> NO dropdown possible in a baked compositor group today;
  selector has to be a stepped float/int input (0-8) into a compare +
  CompositorNodeSwitch cascade.
- display_encoding presets set ONLY {tn_su, display_gamut, eotf}
  (src 858-902) -> the DRT core stays shared under a live display-encoding
  selector; per-encoding scalars (eotf_p = 2+0.2*eotf, ts_dsc, cwp_norm
  table, CAT base d65/d60/dci, xyz->rec709 vs xyz->p3d65 vs DCI-XYZ-keep)
  are live-computable switches. One node group total is feasible.
- EOTF semantics note: OpenDRT sRGB Display = pure pow 2.2, NOT the true
  piecewise sRGB curve -> do NOT defer output EOTF to Blender's display
  colorspaces (bit-exactness). Output stays baked-in-tree, display-encoded;
  viewer view transform = Raw/Standard.
- Output-architecture candidate (user proposal): ONE node group with 9 OUTPUT
  sockets, one per display_encoding; selection = which output is linked.
  Lazy evaluation holds (compositor compiles only actively-linked outputs;
  CPU full-frame + GPU) - verify with a timing benchmark during validation.
  Structural nuance: encoding reaches the CORE via `ts_dsc` (src 953: 0.01
  for PQ, 0.1 for HLG, else 100/tn_Lp) and via `ts_p`/`tn_su` (src 948 ->
  `compress_hyperbolic_power` @1047-1049), so each output branch carries its
  own post-compression chain; v1 may emit 9 naive branch copies (simple
  compiler, bigger file), variant-dedup later if file size matters.
- Blender view transform must be Raw/Standard around the node (OpenDRT
  already encodes for the display) - avoid double transform.
- Node group interface panels (`NodeTreeInterfacePanel`) give the Nuke-style
  tabbed slider UI; group input sockets = the DCTL's parameters.
- PROBED (bpy 5.2.3 wheel + 5.3 Beta CPU/GPU compositor, tools/probes/):
  - Convert-Color-Space XYZ (to "Linear CIE-XYZ D65") is float32-identical
    to the spec matrices for Linear Rec.709 (max-abs 2.4e-7) and Linear
    Rec.2020 (9.5e-7) but DIVERGES for ACEScg (3.8e-3) and ACES2065-1
    (4.5e-3): Blender's AP1/AP0->XYZ config differs from OpenDRT's ->
    ACES-class inputs need a baked-matrix converter group, 709/2020 inputs
    may use Blender's node directly. (docs/PROBE_REPORT.md)
  - Lazy branch evaluation CONFIRMED by timing: 3600-node group, unlinked
    outputs: 0.106 s vs 9-linked 0.466 s (CPU full-frame); force device =
    scene.render.compositor_device + compositor_precision='FULL'.
  - Math node semantics (CPU AND GPU identical): ShaderNodeCompare doesn't
    exist; Math COMPARE has epsilon on input 3; DIVIDE x/0 = 0.0 (NO inf);
    POWER(neg, frac) = 0.0 (NO NaN) - diverges from C powf NaN; MODULO =
    GLSL fmod (sign of dividend); ROUND=floor(x+0.5); Color->Value implicit
    link = Rec.709 luma 0.2126/0.7152/0.0722 (never rely on it - explicit
    Value/SeparateColor only); CombineColor.mode pin 'RGB'.
  - Interface: new_panel(name, description=, default_closed=); ordering via
    interface.move/move_to_parent (new_socket has NO position param);
    items_tree flat OUTPUTS-first; NodeSocketShader rejected; float
    min/max on interface items round-trip; instance sockets: default_value
    (not .value), NO min/max, NOT clamped on assignment; NodeFrame legal.
  - NO CompositorNodeComposite in 5.x: the compositor tree is
    scene.compositing_node_group and the render result = the tree's Group
    Output (deprecation-warned in 5.3).

### Final build decisions (user-confirmed 2026-10-25)

- ONE node group "OpenDRT v1.1.0". Input socket: "Image" = **linear CIE XYZ
  D65** (in_gamut -> identity + in_oetf -> Linear are baked out; upstream
  conversion = Blender's Convert Color Space node, From:<scene space> To:
  "Linear CIE-XYZ D65"; numeric equivalence vs the spec matrices to be
  verified). Tree starts at src 964 (xyz_to_p3d65 multiply).
- **9 OUTPUT sockets**, one per display_encoding preset (selection = which
  output is linked; lazily evaluated). Emission v1: naive 9x duplication of
  964..1210 per branch, with branch-local baked constants (display_gamut,
  eotf, tn_su, ts_dsc instance, CAT matrix family, cwp_norm ladder); crv
  overlay omitted (crv_enable baked 0); functions inlined flat; matrix ops
  = flat 9x mul-add; literals cast float32 at emission.
- **All remaining UI params (~69) exposed** as group-input sockets with
  defaults = the standard preset's resolved values (extract from
  spec.selection), organized in collapsible panels, high-level artistic
  knobs first. NOT exposed: in_gamut/in_oetf/tn_su/display_gamut/eotf/
  crv_enable + the 3 preset combos.
- Preset selectors (Look/Tonescale ladders): DEFERRED; later as wrapping
  node groups. v1 = standard look only.
- Build/deps rule: ALL build tooling lives in the pixi env (g++ added as
  `gxx_linux-64`, jq, python 3.13 + bpy wheel); host tools must not be
  assumed (except the user's Blender at ~/bin/blender for round-trip
  checks - export via BLENDER_BIN, never hardcode). Shim compilation flags:
  -std=c++17 -O2 -ffp-contract=off to stay version-stable.

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

1. ~~Review spec against the DCTL - user sign-off~~ DONE (BuilderDesign
   cross-checked 11 items, zero blocker queue).
2. Node builder: `tools/build_nodes.py` per docs/BUILDER_DESIGN.md +
   docs/PROBE_REPORT.md - WRITTEN, emits successfully (~10 min per build:
   ~6.1k nodes / ~9.4k links / 1.02 MB .blend), but VALIDATION IS OPEN (see
   "Build-wave status" below).
3. Validation oracle setup (g++ DCTL shim) - DONE (tools/dctl_shim/shim.cpp +
   tests/golden + tools/verify_shim.py; goldens for enc0..enc8).
4. Preset assets (wrapping node groups) + packaging.

## Build-wave status (2026-10-26, BuilderImpl agent hand-off)

### Delivered in this wave

- `tools/build_nodes.py` - the full IR -> node-graph compiler. CLI:
  `pixi run python tools/build_nodes.py --spec spec/opendrt-v1.1.0.json
  --out dist/OpenDRT_v1.1.0.blend` (= the added `pixi run build-nodes`
  task). Inventory cross-check vs spec.parameters + the Standard look chain
  (mismatch = build failure); emits ONE CompositorNodeTree "OpenDRT v1.1.0"
  (SeparateColor + tonescale cluster 943-960 shared; 9x naive duplication
  of src 964..1210 with per-branch constants; functions inlined flat;
  matrices as 9x MUL + 6 ADD; const folding in f32 via numpy; node naming
  `b{B} r{src}#{k}`, "bS" = shared; NodeFrames per fn call / region / tail);
  writes via libraries.write(compress=True, path_remap="NONE"); exits 1
  (BUILD_ERROR) on any unmappable IR element. Measured-semantics
  reconciliations D1-D9 vs the design doc are in the module docstring
  (COMPARE eps-0 equality; Mix NaN contamination -> eotf_hlg ln-arg
  MAXIMUM guard; sdivf = plain DIVIDE; contrast_high inner gate A/B from
  the IR; pt_hdr = 69th socket; folded 1.0/eotf_p = f32 division).
- `pixi.toml` - `[tasks.build-nodes]` + `[tasks.check-nodes]` (the latter
  runs tools/rt_smoke.py under $BLENDER_BIN, 5.3 Beta).
- `tools/rt_smoke.py` - round-trip asserts + 64x64 smoke + shim-parity gate
  (phase A interface/default/name/R-14 audits; phase B smoke render; phase C
  `--parity enc0|all` compares against tools/dctl_shim/build_dctl_shim).
- `dist/OpenDRT_v1.1.0.blend` - built artifact for user testing:
  sha256 150e76e3e72ebe6875524481563d99a4d03b7ae391b966ed5151fd5704a9cf7d
  (nodes=6126 links=9430, size=1020094, pre-fake-user-fix build; the
  deterministic rebuild below replaces it).

### Build/render facts measured this wave

- Build ~10 min (Blender node create/name is O(n) per node => ~O(n^2) at
  n~6k; acceptable).
- libraries.write DROPS use_fake_user: the 5.3 Beta reads the written file
  with the flag off - the builder now re-opens the written file and
  re-saves with use_fake_user=True (fix applied 2026-10-26, rebuild
  pending verification).
- Round-trip asserts (5.3.0 Beta reading build A): group exists/type/asset
  OK; 8 panels + names + open/collapse states OK; 1+69+9 sockets OK;
  9 output names in DCTL order OK; spot-checked exact-f32 defaults OK
  (tn_Lp 100.0, Contrast 1.66(0x3FD47AE1), rs_bw, hs_b, HDR Purity 0.5,
  Clamp 1, hcon/lcon-enable False = the Standard-look divergence);
  output descriptions "Target display: ..." OK; region/cluster/tail frames
  + 50+ fn frames OK; single GroupInput/Output, 1 SeparateColor,
  9 CombineColor, no nested groups OK; naming `b{B} r{src}#{k}` OK;
  no RGBA-into-math links (R-14) OK.
- USER-VISIBLE BUG (next session's job): the node's render matches the
  OCIO bake structurally but shows a strong pink/red cast
  (/tmp/cmp/ocio.png vs /tmp/cmp/node1.png; view Raw; scene-linear ->
  Convert Color Space "Linear CIE-XYZ D65" -> group -> sRGB output).
  DECISIVE discriminator ready: node-vs-shim parity
  (`pixi run check-nodes` phases B/C). If parity FAILS - debug emission
  per stage (node labeling `r{src}` maps 1:1 to DCTL stage boundaries);
  if parity PASSES - the difference is upstream (Convert Color Space vs
  the OCIO bake's input conversion) or in the OCIO bake itself - compare
  with zicklag/opendrt-1.1-ocio scripts.

### Open for the next session

1. Rerun the two determinism builds (`pixi run build-nodes` twice; sha256
   both) - note two builds to DIFFERENT paths already produced different
   bytes (~all of the compressed stream; not just an embedded path), so if
   same-path runs differ too, hunt the nondeterminism (per-ID asset UUID?
   ID-block ordering?) before relying on byte-identity.
2. Verify the fake-user re-save fix lands (load the rebuilt blend in 5.3
   Beta; expect use_fake_user=True).
3. Diagnose/fix the pink cast (user-visible bug above).
4. Finish the smoke gate: finite + non-uniform + 3 sample pixels for the
   enc-0 render (rt_smoke.py phase B - in flight at hand-off).


Legacy next-steps kept from before this wave are superseded by the list
above; the validation plan (sections below) still applies for the Validate
wave after node-vs-shim parity passes.


Provenance rule: any committed spec JSON must carry tag + commit + source
sha256 that match the DCTL actually parsed. Do not regenerate lazily and
commit with stale provenance.