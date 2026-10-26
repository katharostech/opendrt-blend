# PROBE REPORT — empirical Blender facts for the OpenDRT node-graph builder

Provenance: run 2026-10-25 by `tools/probes/*` in this repo.
- Runtime A: **bpy 5.2.3 LTS wheel** (pixi env, Python 3.13.17) — `pixi run python tools/probes/<script>`.
- Runtime B: **Blender 5.3.0 Beta binary** (user's daily driver, hash `3db45062d058`, built 2026-10-19) — `$BLENDER_BIN -b --factory-startup -P tools/probes/<script> -- ...`.
- Engine for all renders: **Cycles CPU** (1 sample, denoise off, adaptive off), tiny resolution, `scene.render.compositor_device = 'CPU'`, `scene.render.compositor_precision = 'FULL'` (both props exist in 5.2.3 and 5.3).
- Raw result files: `tools/probes/results/*.json|log`. Scratch JSONs in `/tmp/opendrt_probes/`.

Scripts: `probe_xyz_equiv.py`, `probe_lazy.py`, `probe_interface.py`, `probe_math_semantics.py`.

---

## Probe 1 — Blender `Convert Color Space` vs OpenDRT spec input matrices (XYZ D65)

Method: real headless compositor renders (Cycles CPU, 4x3 px). Test values (linear, one per pixel): white 1.0, 0.18 gray, 0.5, HDR 2.0, HDR 10.0, black, R/G/B primaries, yellow/cyan/magenta, mixed chroma (0.35, 0.08, 0.22). Source = in-memory float32 image tagged `Non-Color` (identity; `Raw` does NOT exist as an image colorspace in 5.2/5.3). Pipeline: Image → `CompositorNodeConvertColorSpace` → tree Group Output → float32 EXR (`color_depth='32'`, `exr_codec='NONE'`, `view_transform='Standard'`), read back with `Non-Color`. Reference: numpy float32 (and float64) application of `spec/opendrt-v1.1.0.json` constants, using the float32-rounded stored input values.

Controls (both runtimes, must be exact):
| control | result |
|---|---|
| passthrough (Image → GroupOutput, no convert node) | max_abs = 0.0, **bitwise equal** |
| identity conversion (from = to = `Linear Rec.709` through the node) | max_abs = 0.0 |
So EXR float32 round-trip + non-Color readback + compositor are bit-transparent; the comparisons below measure the conversion alone.

Accepted names (assigned successfully on `from_color_space` AND `to_color_space`; both lists identical, 63 options each via `bl_rna.properties[...].enum_items`):
- among the 11 candidates probed, accepted: `Linear Rec.709`, `Linear Rec.2020`, `ACEScg`, `ACES2065-1`, `Linear CIE-XYZ D65`
- rejected: `XYZ`, `CIE-XYZ D65`, `Linear CIE-XYZ`, `CIE XYZ`, `Linear ACEScg`, `Linear Rec.1886`
- `to_color_space` (and from) are NODE properties (enums exist as `CompositorNodeConvertColorSpace.from_color_space/to_color_space`), default value `'scene_linear'`.
- Full 63-option enum captured in `probe1_*.json → accepted_enums.options` (e.g. also `Linear CIE-XYZ E`, `ADX10`/`ADX16`, camera-log encodings…).
- RNA error-string truncation: the accepted-option list embedded in RNA assignment errors is clipped (error message length 1363 chars) — the full list must come from `enum_items`, not from parsing the exception.

Results vs spec matrices (12 pixels x 3 channels; per-timeout identical on wheel and binary):
| from_color_space | spec matrix | max_abs diff | max_rel diff (all) | max_rel (abs ref ≥ 1e-3) | verdict |
|---|---|---|---|---|---|
| `Linear Rec.709` | `matrix_rec709_to_xyz` | **2.384e-07** | 3.10e-07 | 3.10e-07 | identical (float32, ~1 ulp) |
| `Linear Rec.2020` | `matrix_rec2020_to_xyz` | **9.537e-07** | 8.17e-07 | 8.17e-07 | identical (float32, ~2 ulp) |
| `ACEScg` | `matrix_ap1_to_xyz` | **3.814e-03** | 13.0 | 0.74 | **DIVERGES** |
| `ACES2065-1` | `matrix_ap0_to_xyz` | **4.529e-03** | 5.53 | 2.65 | **DIVERGES** |

Wheel (5.2.3) and binary (5.3 Beta) numbers agree to sub-float32-ulp for every cell — the OCIO conversion value is stable across both versions.

Extracted Blender matrices (linear, exact: columns = compositored responses to basis inputs) vs OpenDRT spec:

- `ACEScg` → XYZ D65:
  - Blender: [[0.652237535, 0.128236130, 0.169982255], [0.267672181, 0.674340010, 0.057987832], [-0.005381816, 0.001369060, 1.093070507]]
  - OpenDRT: [[0.652418718, 0.127179926, 0.170857284], [0.268064059, 0.672464479, 0.059471462], [-0.005469929, 0.005182800, 1.089344879]]
- `ACES2065-1` → XYZ D65:
  - Blender: [[0.938279867, -0.004451446, 0.016627524], [0.337368906, 0.729521573, -0.066890456], [0.001173951, -0.003710706, 1.091594458]]
  - OpenDRT: [[0.938630949, -0.005741921, 0.017566899], [0.338093595, 0.727213903, -0.065307498], [0.000723122, 0.000818442, 1.087516187]]

**Verdict 1:** Blender's `Convert Color Space` node is numerically equal to the OpenDRT spec input matrices for `Linear Rec.709` and `Linear Rec.2020` (≤ ~1e-6, float32 ulp-level — the node is usable for those spaces). For `ACEScg`/`ACES2065-1` inputs it is NOT identical (max_abs ≈ 4e-3 real divergence, different internal AP0/AP1→XYZ transforms, not float noise) — the input-contract identity (`in_gamut` → `Linear CIE-XYZ D65`) must be realized for ACES spaces with a **baked OpenDRT matrix node group**, per the fallback already identified in STATUS "Final build decisions". Both runtimes agree, so the builder can trust ONE set of numbers.

---

## Probe 2 — Unlinked output-branch cost (lazy evaluation)

Heavy node group: 1 input socket, 9 output sockets, each output heading a serial `ShaderNodeMix` RGBA chain of **400** nodes (3600 eval nodes in the group). Group placed in a compositor tree; downstream consumer chain mixes linked outputs into the tree's output socket. Timing: `time.perf_counter` around `bpy.ops.render.render()`; Cycles CPU 1-sample 64x64; CPU full-float compositor; first render timed separately (exposes graph-compile overhead), then 3 timed renders, median reported.

| links | wheel median | wheel first | binary median | binary first |
|---|---|---|---|---|
| 0 (no outputs linked, input still linked) | **0.106 s** | 0.225 s | **0.123 s** | 0.222 s |
| 1 | 0.202 s | 0.159 s | 0.135 s | 0.180 s |
| 9 | 0.466 s | 0.506 s | 0.552 s | 0.604 s |
| 18 (each output consumed twice) | 0.630 s | 0.688 s | 0.575 s | 0.580 s |

Derived:
- inferred per-branch cost (9-branch delta / 9): **0.040 s** (wheel) / **0.048 s** (binary) per 400-node branch.
- unlinked cost: base vs 9-linked residual after subtracting 9 × branch cost: **~0.0** in both runtimes.
- 9 → 18 links: +0.163 s (wheel) / +0.022 s (binary) — consistent with the extra downstream combiner/consumer work, NOT with each branch re-evaluating per additional consumer (that would have added ≈ 9 × 40-48 ms more).

**Verdict 2:** the 5.x CPU compositor compiles/evaluates **only actively-linked output branches**. An unlinked output of a node group costs nothing measurable (base renders include the heavy group node present-but-unlinked at bare-Cycles cost). Laziness per output socket is confirmed in the bpy 5.2.3 wheel AND the 5.3.0 Beta binary (CPU compositor; property names: `scene.render.compositor_device`, `scene.render.compositor_precision`). This validates the 9-output-socket lazy design (one per display encoding; branch evaluation = linked outputs only).

---

## Probe 3 — Node-tree interface API (panels, sockets, limits, round-trip)

All facts below hold in the bpy 5.2.3 wheel AND (identical assert results) the 5.3 binary; round-trip performed as wheel-writes → binary-opens-and-places-instance → wheel-reopens-and-asserts. Full logs: `tools/probes/results/probe3_*.log`.

Interface (`NodeTreeInterface` on `CompositorNodeTree`):
- `interface.new_panel(name, description="", default_closed=False)` — **description is keyword-only** (positional raises TypeError); returns a `NodeTreeInterfacePanel`. Returned panel properties: `item_type, parent, position, index, name, description, default_closed, select, interface_items, persistent_uid, identifier`. **`default_closed` = the collapse flag; round-trips through file save/load in both runtimes.** No `panel_depth` property.
- `interface.new_socket(name, description="", in_out='INPUT', socket_type='DEFAULT', parent=None)` — **no position parameter**; creation order = initial order at the parent; reordering via `interface.move(item, to_position)` and `interface.move_to_parent(item, parent, to_position)` (both measured working: socket moved within panel to position 0; panel moved to top position 0; socket + nested panel moved across panels).
- `items_tree` is a FLAT list: first all OUTPUT socket items, then all input items in order (each panel immediately followed by its children). `position` is **per-parent** (panel children are numbered within the panel; top-level outputs/inputs/panels form one list beginning with the outputs — measured: `position` 0..1 = outputs, input socket = 2, panels = 3..4). `index` = flat ordinal. Do not compare `parent` by identity (`getattr(item, "parent", None) is x` fails across RNA wrappers AND a null parent returns an empty-`name` pointer rather than None) — compare `parent.name` (empty string ⇒ top level).
- socket types accepted on a compositor interface: `NodeSocketFloat, NodeSocketColor, NodeSocketBool, NodeSocketInt, NodeSocketString, NodeSocketVector, NodeSocketMenu` — **`NodeSocketShader` is rejected** (RNA enum error).
- `NodeSocketBool` input default: starts `False`, `default_value = True` round-trips.
- `NodeSocketFloat`: `default_value` (0.18 → 0.18000000715 float32), `min_value`/`max_value` set and read back EXACTLY (0.0/1.0, -10.0/10.0) through the round-trip in both runtimes.
- interface sockets expose `description` (verified settable + preserved; usable as legends) — also on panels.
- `NodeFrame` is **legal inside a compositor node group** (instanced and survives round-trip).
- The compositor tree container: free-standing `bpy.data.node_groups.new(name, "CompositorNodeTree")` datablock attached with `scene.compositing_node_group = ng` (property is deprecation-warned in 5.3 "expected to be removed in Blender 6.0" but still the attach point in 5.2.3 AND 5.3 Beta; `Scene.node_tree`/`Scene.use_nodes` legacy path: `use_nodes` deprecation-warned, `scene.node_tree` GONE). There is **no `CompositorNodeComposite` node in 5.x** — the render result is read from the tree's own Group Output (`NodeGroupOutput` + interface OUTPUT sockets; the fresh tree ships empty with no interface items).

Group INSTANTCE side (after `nodes.new("CompositorNodeGroup")` + `node_tree = ng` in a compositor tree):
- instance sockets mirror the interface inputs/outputs (names + socket classes: `NodeSocketFloat/Color/Bool/Int`).
- instance float sockets expose **`default_value`** (NOT `.value` — `.value` raises AttributeError).
- instance sockets DO NOT expose `min_value`/`max_value` (all-False on every instance input socket) — limits live only on the interface item.
- setting an instance `default_value` OUT OF RANGE (2.0 with interface max 1.0) is **NOT clamped** and the out-of-range value SURVIVES the whole wheel→binary→wheel round-trip (clamping is UI-side only; a builder cannot rely on socket min/max to sanitize inputs at evaluation time).

Round-trip assertions (counts + order + defaults + panel names/descriptions/collapse + min/max + NodeFrame + instance sockets): **ALL PASS** on writer(wheel) and verify(wheel reopen of the binary-saved tree); binary-saved tree reopens in the wheel with identical group interface + instance.

**Verdict 3:** the interface API needed by the builder is fully usable in both runtimes: collapsible panels + socket ordering via `move`/`move_to_parent`, per-parent positions, bool/float/color/int/string/vector/menu socket types, `description` legend fields, exact min/max round-trip on interface sockets, NodeFrames allowed. Panel layout, socket limits and defaults survive `libraries.write` (5.2.3 wheel) → real 5.3 binary load → `.blend` re-save → wheel reopen. No clamping behavior exists at runtime; input sanitization (if needed) is the responsibility of the node group math itself.

---

## Probe 4 — ShaderNodeMath semantics inside compositor trees (requested extra)

Method: one 1x1 Cycles-CPU compositor render per case; literal float sources (ShaderNodeValue) → `ShaderNodeMath` → CombineColor (mode RGB) → Group Output; GPU variant in the binary requested `compositor_device='GPU'` (render values identical, so CPU/GPU parity below is only guaranteed for the CPU path unless noted).

- `ShaderNodeCompare` DOES NOT EXIST in 5.2/5.3 (no such registered type). Comparisons = `ShaderNodeMath` ops: `LESS_THAN`, `GREATER_THAN`, `SIGN`, and **`COMPARE` (== within epsilon; epsilon = input socket 3)** — measured: |0.1−0.1002| > 1e-6 → 0.0; with epsilon 1e-3 → 1.0.
- `ShaderNodeMix` accepts `data_type` in {'FLOAT','VECTOR','RGBA'} (all instantiate in compositor trees; measured ok).
- `ShaderNodeMath` operation enum (40 operators, from RNA): ADD, SUBTRACT, MULTIPLY, DIVIDE, MULTIPLY_ADD, POWER, LOGARITHM, SQRT, INVERSE_SQRT, ABSOLUTE, EXPONENT, MINIMUM, MAXIMUM, LESS_THAN, GREATER_THAN, SIGN, COMPARE, SMOOTH_MIN, SMOOTH_MAX, ROUND, FLOOR, CEIL, TRUNC, FRACT, MODULO, FLOORED_MODULO, WRAP, SNAP, PINGPONG, SINE, COSINE, TANGENT, ARCSINE, ARCCOSINE, ARCTANGENT, ARCTAN2, SINH, COSH, TANH, RADIANS, DEGREES. (Note names: `CEIL`, `FRACT`, `TRUNC`.)

Measured semantics (1x1 EXR readback, CPU; identical on `compositor_device='GPU'` in 5.3 Beta):
| case | result | note |
|---|---|---|
| DIVIDE 1/0, −1/0, 2/0, 0/0 | **0.0** | divide-by-zero yields 0.0, not inf/NaN |
| MODULO 7 % 4 | 3.0 | |
| MODULO −7 % 4 | **−3.0** | sign follows dividend (GLSL fmod), NOT Python's floored 1.0 |
| MODULO 7 % −4 | 3.0 | |
| FLOORED_MODULO 7 % −4 | −1.0 | Python-style floored modulo |
| FLOORED_MODULO −7 % 4 | 1.0 | |
| POWER (−2)^0.5, (−2)^0.75, (−2)^0.25 | **0.0** | fractional exponent of negative base → 0.0, **no NaN** |
| POWER (−2)^0 | 1.0 | |
| POWER 0^0 | 1.0 | |
| POWER (−2)^2 | 4.0 | integer exponent fine |
| SQRT 2 | 1.414213538 | |
| INVERSE_SQRT 2 | 0.707106769 | |
| ROUND 2.5 / −2.5 | 3.0 / −2.0 | ROUND = floor(x+0.5); not half-to-even, not half-away for negatives |
| FLOOR 2.9 / −2.9 | 2.0 / −3.0 | true floor |
| CEIL 2.1 / −2.1 | 3.0 / −2.0 | true ceil |
| FRACT 1.25 / −1.25 | 0.25 / **0.75** | FRACT = x − floor(x) (Python-style); negative inputs give a POSITIVE fraction |
| TRUNC 2.9 / −2.9 | 2.0 / −2.0 | |
| ABSOLUTE −1.5 | 1.5 | |
| SNAP 2.34→0.5 / 2.75→0.5 | 2.0 / 2.5 | SNAP = floor(x/inc)·inc (rounds toward −inf) |

**Load-bearing gotcha (measured):** linking a **Color** output into a **Value** (float) socket applies an implicit luminance conversion with Rec.709 weights rounded to 4 decimals — measured `luma(c) = 0.2126·R + 0.7152·G + 0.0722·B` (e.g. input R=1.4142135, G=B=0 → 0.3006616 = 1.4142135·0.2126 bit-exact). A value pipeline MUST use float source sockets (`ShaderNodeValue` outputs Value) or explicit channel extraction (`CompositorNodeSeparateColor` Red/Green/Blue) — never feed color sockets directly into Math nodes.
- float value → Color socket (e.g. `CombineColor`'s R/G/B inputs, mode `RGB`) passes through unchanged (measured exact).
- `CompositorNodeCombineColor.mode` enum = ('RGB','HSV','HSL','YCC','YUV'); if not explicitly `RGB`, channel inputs are reinterpreted per that mode (HSV default behavior produced wrong values — pinned empirically).
- CPU vs GPU compositor: all 41 probe cases identical between CPU (wheel + binary) and GPU-compositor in the 5.3 binary (div-by-zero = 0.0, POWER(neg,frac) = 0.0 both devices; **no NaN on either device**).

## Extra 5.x structural facts (blockers hit during probe development)
- There is NO composite/viewer output node in 5.x compositor trees: render result = Group Output socket(s) of the `CompositorNodeTree` attached via `scene.compositing_node_group`.
- Blender saved EXR buffers are padded (4x3 resolution → 4x4 buffer; data pixels contiguous bottom rows first). Probes slice `[:W*H]`.
- `read_factory_settings(use_empty=True)` resets ALL datablocks — build scene before images/trees in one runtime pass.
- Running the binary: `-b --factory-startup -P script.py -- args` (WITHOUT `--factory-startup` the 5.3 binary aborts script execution when it cannot load the user's userpref/startup.blend). In binary mode, `sys.argv` retains Blender's own flags — script args come after `--`.
- EEVEE (5.x engine id `BLENDER_EEVEE`) DOES render inside the bpy wheel headlessly in this environment (a control render succeeded, ~25 s incl. shader-cache compile) — available as an alternative to Cycles if it ever matters; per steering all probes above use Cycles CPU.