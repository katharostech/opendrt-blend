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
2. Node builder: `tools/build_nodes.py` - done through TWO rewrite waves;
   current: nested node-group emitter (see "Build-wave 2026-10-26 (perf)"
   below), emits in ~13.5 s, VALIDATION still open (parity vs shim fails -
   pre-existing, see below).
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
  sha256 7c4e7474480d8cb72b294f56e64e186b4f41ced061cfd5033d3fd7e24dfab9ea
  (33 node groups, size 899937; main 65 nodes / 735 links - the folded
  conversion; totals 3831 nodes / 7948 links; single libraries.write).
  Image socket description now exactly "Image in the scene linear working
  space." (2026-10-26 user decision; semantics unchanged - the fold-in
  'raw2xyz' convert + parity noise numbers above describe the math).
  Preset nodes are THIN WRAPPERS: "OpenDRT 1.1 [look]" = GroupInput + one
  "base" instance of OpenDRT 1.1 Base + GroupOutput, inputs/outputs
  forwarded one-to-one, interface defaults = the look's chain values.
  The user's external Convert Color Space step is folded in-tree:
  'raw2xyz' node (from = scene_linear working-space role, to = 'Linear
  CIE-XYZ D65') between the Image input and the SeparateColor; gates
  --parity enc0 PASS; --parity all 172 checks PASS (matrix roundtrip
  noise <= 8.2e-06, tolerance 1e-04).
- All 8 user-facing groups (main + 7 preset wrappers) are assets in the
  one deterministic "OpenDRT" catalog (uuid 68af1827, uuid5);
  dist/blender_assets.cats.txt sidecar carries it; branch/fn/dgpw/vdot
  groups stay unmarked; fake-user uniform on all 33 groups.
- Build ~10 min (Blender node create/name is O(n) per node => ~O(n^2) at
  n~6k; acceptable).
- libraries.write's default fake_user=False silently drops the fake-user
  bit: the 5.3 Beta reads use_fake_user=False. Fix: pass `fake_user=True`
  to libraries.write (2026-10-26, verified flag survives in 5.3 Beta).
  The former open_mainfile/save_as_mainfile re-save workaround also
  embedded factory startup data (11 workspaces, `Empty` scene, WinMan,
  screens) into dist/ - the build now writes a pure node-group library.
- Blend-file byte determinism DROPPED (2026-10-26 decision): the direct
  libraries.write flow is NOT byte-reproducible on the same path - two
  same-path rebuilds differ in one 4-byte field per node (3809 nodes =
  3809 4-byte clusters, ~15 kB decompressed); tiny probe writes are
  byte-identical, so it is a container/per-save bit, not builder logic.
  Rationale: .blend format is not designed for byte determinism; the real
  contract is deterministic MATH, already gated by spec/DCTL sha256 +
  golden bins + rt_smoke parity. Structural/behavior checks replace any
  artifact-hash equality comparison in CI.

### Build-wave 2026-10-26 (perf rework, nested groups; parity RESOLVED)

The pink-cast session's open items were resolved or re-scoped:

- **Determinism (was open #1): DONE.** Two builds to the SAME path are
  byte-identical (sha256 56c588207cf9218864b208e9995b36ce4f5daf602d81d6
  05536f5bda8b6e1be0). Builds to DIFFERENT paths still differ (as before;
  likely path embedded in the file) - same-path identity is what CI needs.
- **fake-user (was open #2): VERIFIED** - rt_smoke reads use_fake_user=True
  on every written group.
- **Smoke gate (was open #4): DONE** - pattern is 6 flat color bands so the
  old `unique>100` expectation was wrong; now asserted uniq==6 + 3 sample
  pixels + finite. rt_smoke also had two latent test bugs fixed: interface
  iteration hit PANEL items with `.in_out` (must filter item_type first)
  and compared socket_type against enum names ("FLOAT"/"COLOR") while 5.3
  Beta returns ids ("NodeSocketFloat"/"NodeSocketColor").
- **Pink cast (was open #3): RESOLVED 2026-10-26.** Root cause: dgpw's
  A633 blend in `tools/build_nodes.py` fed the CAT-ladder result with the
  call-site `rgb_h` head (pre-`matrix_p3d65_to_xyz`, P3-D65-linear) instead
  of the post-A598 XYZ value; for identity-CAT configs (cwp==2, the pinned
  D65) the blend mixed P3D65-linear with XYZ (DCTL mixes the value with
  itself). Fixed at src 633 (blend input `rgb_h` -> post-A598 `rgb`).
  Bisection: instrumented builder emission (var->node heads per statement)
  vs an f32 IR interpreter seeded from the shim's --json-info; first
  divergent tap = src 1140 (dgpw); all prior taps <= 2.4e-7. After the fix,
  full parity (9 encodings x patch-grid + 4096-pt neutral ramp) passes:
  ramps bit-identical for enc0-6/8, enc7 <= 6e-8, worst patch (enc8) <=
  1.7e-6 (gate 1e-4).

Nested-group perf rework (user plan, this wave):

- ONE fn group per DCTL function (bodies emitted ONCE; call sites are
  CompositorNodeGroup instances with per-instance arg defaults), one dgpw
  group per baked gamut, a dedicated 12-input vdot group (m{ri}{ci} +
  v.{xyz} inputs; per-instance matrix defaults), and ONE group per
  display encoding ("OpenDRT b{enc}") holding its own region incl. ts_p
  (948) / ts_dsc (953) and the CombineColor; the main tree is down to 64
  nodes / 734 links and wires each branch group's live vars via its
  interface.
- NO NodeFrames and NO node layout anywhere (user decision; also removes
  ~16% of the old build time; the r{src} names remain the stage mapping).
- Cross-boundary values are late-bound "P" heads - a value needed inside a
  group registers its consumers when attached and the builder creates the
  group-input sockets + wires after the body emits (exact read sets, no
  static pre-pass drift).
- Fn-group gating safety: a group gate evaluates BOTH arms (Blender Mix
  propagates untaken-arm NaN, D3) so fns carrying sqrt/ln inside gated
  arms stay INLINE: GROUP_EXEMPT = {sdivf (D1), display_gamut_whitepoint
  (variants), hypotf2, hypotf3, compress_toe_quadratic, compress_toe_cubic,
  eotf_hlg}. Remaining gates evaluate the same arm as today (instance arg
  default => same value into a live compare).
- NUMBERS: build 13.5 s solo (was ~10 min => ~44x); 26 trees; total
  3809 nodes / 7394 links (was 6126 / 9430); blend 792,360 B (was
  1,020,094). Attribution of the OLD build (contended, 2192 s):
  attach/links/new/name/default writes each ~52-85 ms per call (each bpy
  write triggers an O(n) tree update at n~6k => O(n^2)); layout() alone
  359 s; node.parent<-frame ~0 s. The tree split fixed the quadratic;
  frames were a minor part.
- rt_smoke phase A: ALL GREEN on this structure (26 trees, 9 branch
  instances, 16 fn groups, zero frames, naming b*/g* per tree, spot f32
  defaults exact, no RGBA-into-math in ANY tree (R-14 extended to all
  trees + per-tree duplicate-name audits in the builder)).

### Open for the next session

1. ~~Diagnose/fix the parity/pink-cast divergence~~ DONE 2026-10-26
   (dgpw A633 blend input; see "Pink cast" above).
2. ~~Rebuild the production artifact (`pixi run build-nodes` => dist/)~~
   DONE 2026-10-26: dist/OpenDRT_v1.1.0.blend rebuilt with the fix and the
   full parity suite green.
3. Optional: re-probe lazy evaluation THROUGH nested groups (9-linked vs
   1-linked timing on the new structure; the flat-tree fact carried from
   PROBE_REPORT should transfer since each output is fed by its own
   branch-group instance, but it is now nested two levels).
4. Optional perf levers if build time ever matters again (unlikely):
   VectorMath packing of per-channel triples (bg-2 verified 3 Math -> 1
   VectorMath; parity-safe for add/sub/mul/div), branch-group instance
   parallelization (per-tree emission is context-isolated already).


Provenance rule: any committed spec JSON must carry tag + commit + source
sha256 that match the DCTL actually parsed. Do not regenerate lazily and
commit with stale provenance.