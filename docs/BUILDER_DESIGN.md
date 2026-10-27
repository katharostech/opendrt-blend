# BUILDER_DESIGN.md — Blender node-graph builder contract (OpenDRT v1.1.0)

Contract for `tools/build_nodes.py` (Build wave). Ground truth: `STATUS.md`
(committed decisions) + `spec/opendrt-v1.1.0.json` (the IR) +
`reference/OpenDRT_v1.1.0.dctl` (resolves IR gaps). This doc adds no new
algorithms: every emission is a spec fact (src-cited) or a flagged decision.

## 0. Builder output

Eight Blender **CompositorNodeTree** node groups (§0a): the main
`OpenDRT 1.1 Base`
(`bpy.data.node_groups.new(...)`) in a .blend asset. Interface: input socket
`Image (Linear CIE XYZ D65)` (NodeSocketColor; tooltip: values used as-is, no
color-management conversion) + 68 input sockets (§E) + **9 color OUTPUT
sockets**, one per display_encoding preset (§F). Interior: flat `ShaderNodeMath`/
`ShaderNodeMix` graph [measured names in PROBE_REPORT §math: ShaderNodeCompare
does NOT exist; equality = ShaderNodeMath `COMPARE` op] — no nested groups, no
matrix sockets, no Menu Switch, no CompositorNodeSwitch (unusable in 5.2.3,
STATUS). 5.x render path [measured, PROBE_REPORT]: Composite node removed; the
tree is a free-standing datablock on `scene.compositing_node_group`, read from
its own output sockets. Harness/render settings = Build wave.


## 0a. Preset nodes (user decision 2026-10-26)

- Main group renamed "OpenDRT 1.1 Base"; its input defaults = the official
  **Base** look chain (DCTL 779-781; supersedes the Standard-default
  decision). The 64 chain-driven sockets get the chain value; the 5
  non-chain knobs (tn_Lp/tn_Lg/tn_gb/pt_hdr/clamp) keep their UI defines.
- Every other official look (Standard, Arriba, Sylvan, Colorful, Aery,
  Dystopic, Umbra) = a **thin wrapper** "OpenDRT 1.1 [look]": exactly 3
  nodes (GroupInput, one CompositorNodeGroup instance of OpenDRT 1.1 Base
  named "base", GroupOutput), every input/output forwarded one-to-one by
  name; only the wrapper's socket defaults differ. Wrappers share
  branch/fn/dgpw groups; totals 3831 nodes / 7948 links (main 65/735
  with the folded convert node); branch count unchanged.
- The user's Convert Color Space step is folded IN-TREE (2026-10-26 user
  decision): the main tree's head = 'raw2xyz' CompositorNodeConvertColorSpace
  (from left at the node's 'scene_linear' default = working-space role;
  to pinned 'Linear CIE-XYZ D65', measured to equal OpenDRT's input
  matrices for 709/2020 <=1e-6) between the Image group input and the
  SeparateColor. Branches receive rgb.x/y/z group inputs - no double
  conversion anywhere. rt_smoke measures the node's runtime matrix per
  run and pre-converts parity inputs with its inverse.
- Math-exact by construction: look chains (DCTL 758-781) set only exposed
  params; tonescale combos (787-825) only contrast knobs; "USE LOOK
  PRESET" (tonescale_preset==0) emits nothing; clamp=1 hard-coded in
  presets mode (828).
- spec look entries are keyed by internal names (tn_con); builder ids are
  underscored (_tn_con) - bridged via ui_to_internal (unknown/non-node
  keys fail the build).
- rt_smoke: per-look default asserts (spec-driven) + per-preset parity;
  the shim stays look=Standard and presets are reached via `--param`
  post-selection overrides with that look's chain values (f32-exact
  serialization). Branch/fn/dgpw/vdot groups are NOT assets; the 8
  user-facing groups are, in the one "OpenDRT" catalog.
- User-facing instance width (2026-10-27 user request): the 8 asset groups
  set `default_group_node_width = 240` (stock 140); wrapper trees also set
  their inner "base" instance `width = 240` explicitly. Internal fn/branch/
  dgpw/vdot trees keep 140. Note: bpy `nodes.new()` bypasses the tree
  default (probe prints an info line); the UI paths honor it (verified
  Shift+A 2026-10-27). Probe: tools/probes/probe_width.py.


## A. IR inventory

### A.1 Statement kinds + counts

Top-level = items of `spec.pipeline` (88) / function bodies (75); nested =
with all `if` branch/`els` bodies expanded (pipeline 164, functions 189).

| kind | pipe top | pipe nested | fn top | fn nested | shape |
|---|---|---|---|---|---|
| declare | 38 | 63 | 17 | 22 | `{type, names[], inits?{name: expr}, qualifier?=const}`; inits may be absent ("-", e.g. ptm_low_f 1123) |
| assign | 18 | 63 | 5 | 89 | `{ops:[{tgt:"var"|"var.x", expr}]}` dotted-path; 1 multi-op stmt only (make_float3x3, dead); NO compound ops anywhere |
| return | 1 | 1 | 35 | 52 | `{expr: expr\|null}` — null occurs ONLY at sdivf src 329, DCTL returns `0.0f` (R-01) |
| if | 19 | 25 | 18 | 26 | `{branches:[{cond, body[], note?, src}], els?: body[]}`; pipe 29 branches+2 else, fns 73+9 |
| select | 8 | 8 | — | — | look(758-784), tonescale×3(787-789/790-792/793-825), creative_white×2(831 rule, 833-838), display_encoding(858-902), input_gamut(909-923) |
| stage | 3 | 3 | — | — | "Parameter Setup"(741), "LOOK PRESETS"(757), "CREATIVE WHITE PRESETS"(830) |
| skipped_preprocessor | 1 | 1 | — | — | `#ifdef INIT_PARAMS` passthrough (751); ignore |
| pass_through_ifdef/if | 0 | 0 | 0 | 0 | vocabulary entry, unused in v1.1.0 |

All 8 `select` statements bake to **zero emitted nodes** (§D).

### A.2 Expression operator vocabulary

S-expressions `[op, args...]`; scalar identifiers = bare strings. **61 unique
atoms**; counts = pipeline/functions (incl. nested). Class: S scalar, V tracked
3-ch value (emitted as scalar channels), V2 2-ch, B bool-as-0/1 float, M
constant 3x3, U user-fn (expanded inline).

| op | class | pipe/fn | JSON shape |
|---|---|---|---|
| add sub mul div | S/V | 37/48, 36/54, 106/89, 35/68 | `[op,a,b]` |
| neg | S/V | 7/1 | `[neg,a]` |
| min max | S | 1/6, 0/10 | `[min,a,b]` |
| abs | S | —/2 | (only in dead softplus_constraint) |
| sqrt | S | —/7 | `[sqrt,a]` |
| exp | S | 5/9 | `[exp,a]` |
| exp2, exp10 | S | —/3, —/5 | oetf bodies only → pruned |
| ln | S | —/5 | hlg body, softplus |
| log2 | S | 1/0 | ts_y0 (946) |
| fmod | S | 1/1 | hue (992), hue_offset |
| atan2 | S | 1/0 | `[atan2,a,b]` = DCTL call order (R-07) |
| pow | S | 22/10 | `[pow,a,b]` |
| lt gt le ge | B | 6/17, 4/10, 0/10, 0/1 | `[le,a,b]` (spowf/chp gates), `[ge,·]` (gamut-5 cwp_norm ladder) |
| eq ne | B | 36/87, 0/0 | compare, same lhs/rhs order as DCTL `==` |
| and or | B | 4/0, 4/9 | boolean gates |
| cond | S/V | 4/17 | `[cond,c,a,b]` |
| attr | access | 77/123 | `[attr,base,comp]` = `.x/.y/.z` or matrix row |
| f2 f3 | ctor | 2/1, 16/10 | float2/float3 tuples |
| m3x3 | ctor | —/1 | `[m3x3,r0,r1,r2]` (dead identity only) |
| cast_float | noop | 1/0 | `[cast_float,a]` → folds into ts_p branch const (948) |
| vdot | M×V→V | 3/20 | `[vdot,matrix,var]` |
| sdivf | S | —/6 | safe-div (R-01) |
| sdivf3f | V×S→V | 1/0 | rgb ratios (983) |
| sdivf3f3 | V→V | 0 | dead |
| spowf | S | —/8 | signed-safe power (le gate) |
| spowf3 | V×S→V | 2/4 | 3× spowf |
| hypotf2 | V2→S | 2/0 | opp length |
| hypotf3 | V→S | 1/0 | tsn (980) |
| clampf | S | —/3 | clampf3 body |
| clampf3 | V×S→V | 2/0 | clamp gate (1183) |
| clampminf3 | V×S→V | 1/0 | P3-limit (1178) |
| fmaxf3 fminf3 clampmaxf3 make_float3x3 identity complement_power sigmoid_cubic softplus_constraint | — | 0 | 8 dead function atoms — no emission |
| compress_toe_quadratic | S | 5/0 | `[ctq,x,toe,inv]`, inv literal ∈{0,1} |
| compress_toe_cubic | S | 3/0 | `[ctc,x,m,w,inv]` |
| compress_hyperbolic_power | S | 5/0 | `[chp,x,s,p]` |
| contrast_high | S | 2/0 | `[ch,x,p,pv,pv_lx,inv]`, inv literal |
| softplus | S | 3/0 | `[softplus,x,s]` |
| gauss_window | S | 8/0 | hue-angle gaussian |
| hue_offset | S | 8/0 | gaussian offset |
| opponent | V→V2 | 2/0 | chromaticity pair (985,1144) |
| eotf_pq | V×I→V | 2/0 | inverse literal 1 both call sites (1188) |
| eotf_hlg | V×I→V | 2/0 | inverse literal 1 (1189) |
| display_gamut_whitepoint | V×S×S×I×I→V | 2/0 | `[dgpw,rgb,tsn_const,cwp_lm,display_gamut,cwp]`; 1171 call pruned with crv |
| linearize | V×I→V | 1/0 | pruned (in_oetf baked Linear, 933) |
| oetf_davinci_intermediate / filmlight_tlog / acescct / arri_logc3 / arri_logc4 / red_log3g10 / panasonic_vlog / sony_slog3 / fujifilm_flog2 | S | ≤4/3 | linearize bodies (pruned); filmlight_tlog's one pipeline use src 930 = crv → pruned |

Dead (0 call sites): make_float3x3, identity, sdivf3f3, fmaxf3, fminf3,
clampmaxf3, complement_power, sigmoid_cubic, softplus_constraint. Reachable but
pruned in the baked tree: linearize + all oetf_* bodies.

### A.3 Constants (37)

35 m3x3 `{kind, rows:[[3],[3],[3]]}` — **row-major**: `vdot.out.x/y/z` =
rows `r0/r1/r2` dotted with v — + 2 scalars: `PI = 3.14159265358979323846`
(304), `SQRT3 = 1.73205080756887729353f` (303). f64 in JSON, cast f32 at
emission (§G). Used in the baked tree: matrix_xyz_to_p3d65 (head + dgpw back),
matrix_p3d65_to_xyz (dgpw entry, all branches), CAT family per gamut (§B.6),
matrix_xyz_to_rec709 (gamut 0), matrix_cat_d65_to_dci (gamut 5),
matrix_p3_to_rec2020 (gamut 2 tail), PI, SQRT3. The 14 input-gamut matrices
(ap0/ap1/arriwg3/4/rec2020/rec709/p3dci/davinciwg/egamut/egamut2/redwg/
sonysgamut3/3cine/vgamut) are **unused** (in_gamut = XYZ identity baked).

## B. Operator → Blender emission mapping

Node idnames (5.2.3, measured): ShaderNodeMath (40-op enum: ADD SUBTRACT
MULTIPLY DIVIDE MULTIPLY_ADD POWER LOGARITHM SQRT INVERSE_SQRT ABSOLUTE EXPONENT
MINIMUM MAXIMUM LESS_THAN GREATER_THAN SIGN COMPARE SMOOTH_MIN SMOOTH_MAX ROUND
FLOOR CEIL TRUNC FRACT MODULO FLOORED_MODULO WRAP SNAP PINGPONG SINE COSINE
TANGENT ARCSINE ARCCOSINE ARCTANGENT ARCTAN2 SINH COSH TANH RADIANS DEGREES;
epsilon of COMPARE = 3rd input), ShaderNodeMix (FLOAT data_type),
ShaderNodeSeparateColor (Image entry only), ShaderNodeCombineColor mode pinned
'RGB' (inside each branch group), CompositorNodeGroup (fn/branch instances),
NodeGroupInput, NodeGroupOutput. Amendment 2026-10-26 (user decision):
- NO NodeFrames and NO node layout anywhere (they were compiler-side
  decoration; rt_smoke asserts zero frames).
- Functions and encoding-branches are NESTED GROUPS, not inline code: one
  fn group per function (bodies emitted once; >= 2 static call sites from
  the emitted regions, minus GROUP_EXEMPT), one dgpw group per baked gamut,
  a dedicated 12-input vdot group, and one group per encoding. See
  STATUS.md "Build-wave status" for the full contract update.

### B.0 Group-exemption rule (2026-10-26)

`GROUP_EXEMPT = {sdivf, display_gamut_whitepoint, hypotf2, hypotf3,
compress_toe_quadratic, compress_toe_cubic, eotf_hlg}` stays inline:
sdivf's body is one DIVIDE (D1); dgpw is emitted as per-gamut variant
groups (its ladders/back-matrices are baked per display_gamut); the rest
carry sqrt/ln inside gated arms - with live group args the gate would emit
BOTH arms and an untaken-arm NaN would contaminate the Mix (measured, D3).
Group gates evaluate the same arm as today's pruned per-site gate (the
instance arg default feeds the same value into a live compare); parity +
bit-identical node-vs-node ramp proven 2026-10-26.

**Rules (B-R):** (1) all-interior pipelines float — `attr` = builder channel
tracking (vdot/f3 vectors are 3 scalar chains; zero Separate/Combine nodes
inside). (2) NO MULTIPLY_ADD (fused mad ≠ C's MUL-then-ADD two-rounding; DCTL
parity); literal-only subexpressions MAY fold to one f32 constant (numpy f32
op-order). (3) constants = f32 (§G). (4) every `if` (live condition) = compare
tree → factor of ShaderNodeMix (data_type FLOAT, clamp_factor=False,
A=false-side, B=true-side); baked conditions (branch consts) resolve statically
(no nodes). (5) NEVER link Color sockets into Math nodes — Color→Value implicit
conversion = Rec709 LUMA (measured bit-exact); scalarize via SeparateColor
R/G/B, floats only in the interior; CombineColor inputs accept float values
unchanged (mode='RGB' pinned).

| IR op | emitted (exact pattern) |
|---|---|
| add/sub/min/max | 1× Math ADD / SUBTRACT / MINIMUM / MAXIMUM |
| mul / div | 1× MULTIPLY / DIVIDE (sdivf → R-01 pattern) |
| neg | 1× SUBTRACT(0.0, a) |
| abs | 1× ABSOLUTE (unreachable in baked tree) |
| sqrt | 1× SQRT |
| exp | 1× EXPONENT (op name measured) |
| ln | 1× LOGARITHM, base-input const e_f32 (LOGARITHM base-input semantics = measured log(a)/log(b); ULP tolerance) |
| log2 | 1× LOGARITHM, base 2.0f |
| exp2 / exp10 | 1× POWER(2.0f,x) / POWER(10.0f,x) (pruned in baked tree) |
| pow | 1× POWER [measured: POWER(neg,fractional)=0.0 no NaN, 0^0=1 — every live pow site has non-negative base per §D; crv-pruned ctc inv=1 site would differ from C NaN — stay pruned] |
| fmod | 1× MODULO [measured: sign-of-dividend = C fmod ==, CPU+GPU; Python-style ≠ used] |
| atan2 | 1× ARCTAN2(arg0, arg1) (first input = CUDA y) |
| lt / gt | 1× LESS_THAN / GREATER_THAN |
| le / ge | 2× SUBTRACT(1.0, GREATER_THAN(a,b)) / SUBTRACT(1.0, LESS_THAN(a,b)) |
| eq | 1× Math COMPARE (epsilon = 3rd input = const 0.0) — folds to const if both operands baked |
| ne | 2× SUBTRACT(1.0, COMPARE) (0 live uses) |
| and / or | 1× MULTIPLY / MAXIMUM on 0/1 floats (never bool sockets) |
| cond | 2×: compare tree → Mix(FLOAT) factor |
| attr f2 f3 m3x3 cast_float | 0× |
| vdot(M,v) | flat 3x3 mul-add: per row r: x=MUL(vx,m_r0), y=MUL(vy,m_r1), z=MUL(vz,m_r2), ADD(x,y)→ADD(→,z) — **9 MUL + 6 ADD** for 3 output channels; v1 emits all 3 rows |
| sdivf(a,b) | 1× DIVIDE [measured: Blender DIVIDE by 0.0 → 0.0 on CPU+GPU compositor = DCTL's `return 0.0f` exactly — no guard needed] |
| sdivf3f(v,s) | 3× sdivf per channel |
| spowf(a,b) | 4×: GREATER_THAN(a,0) → SUBTRACT(1.0,·) → POWER(a,b) → Mix(A=POWER, B=a) (a≤0 → a) |
| spowf3(v,s) | 3× spowf (12×) |
| hypotf2(v) | 6×: MUL(x,x), MUL(y,y), ADD, MAXIMUM(0.0), SQRT |
| hypotf3(v) | 8×: MUL×3, ADD(x²+y²)→ADD(→z²), MAXIMUM(0.0), SQRT |
| clampf / clampf3 / clampminf3 | 2× MAX+MIN / 6× / 3× MAXIMUM |
| compress_toe_quadratic(x,toe,inv) | gate eq(toe,0)→x (Compare+Mix). inv=0: DIVIDE(POWER(x,2.0), ADD(x,toe)); inv=1: DIVIDE(ADD(x, SQRT(MUL(x, ADD(MUL(4.0,toe), x)))), 2.0); ≈8× gate to toe=0.25-const site (989) folds false |
| compress_toe_cubic(x,m,w,inv) | gate eq(m,1.0)→x; x2=MUL(x,x); inv=0 (live sites 1031/1033): DIVIDE(MUL(x, ADD(x2,MUL(m,w))), ADD(x2,w)); inv=1 (only crv-pruned site, emitted for completeness): p0=SUB(x2,MUL(MUL(3.0,m),w)); p1=SUB(ADD(MUL(2.0,x2),MUL(27.0,w)),MUL(MUL(9.0,m),w)); p2=POWER(ADD(DIVIDE(SQRT(SUB(MUL(MUL(x2,p1),p1),MUL(MUL(MUL(4.0,p0),p0),p0))),2.0), DIVIDE(MUL(x,p1),2.0)), DIVIDE(1.0,3.0)); ret=ADD(ADD(DIVIDE(p0,MUL(3.0,p2)),DIVIDE(p2,3.0)),DIVIDE(x,3.0)); ≈18× |
| compress_hyperbolic_power(x,s,p) | 6×: DIVIDE(x, ADD(x,s)) → spowf(·,p) |
| contrast_high(x,p,pv,pv_lx,inv) | x0=MUL(0.18,POWER(2.0,pv)); outer gate MAXIMUM(LT(x,x0), COMPARE(eq p,1.0)) → Mix(A=body, B=x) (inv=0 exit); body: o=SUB(x0,DIV(x0,p)); s0=DIV(POWER(x0,SUB(1.0,p)),p); x1=MUL(x0,POWER(2.0,pv_lx)); k1=DIV(MUL(MUL(p,s0),POWER(x1,p)),x1); y1=ADD(MUL(s0,POWER(x1,p)),o); inner COMPARE(gt x,x1) → Mix(B=ADD(MUL(s0,POWER(x,p)),o), A=ADD(MUL(k1,SUB(x,x1)),y1)); ≈20× |
| softplus(x,s) | gate MAXIMUM(GT(x,MUL(10.0,s)), LT(s,0.0001)) → Mix(A=body, B=x); body: MUL(s, LOGARITHM(MAXIMUM(ADD(1.0,EXP(DIVIDE(x,s))),0.0), e_f32)); ≈11× |
| gauss_window(x,w) | 4×: SUBTRACT(0,x), MUL(·,x), DIVIDE(·,w), EXP |
| hue_offset(h,o) | 5×: SUBTRACT(h,o), ADD(·,PI_f32), MODULO(·,6.2831853f), SUBTRACT(·,PI_f32) |
| opponent(rgb) | 3×: x=SUBTRACT(rx,rz); y=SUBTRACT(ry, DIVIDE(ADD(rx,rz),2.0)) → 2 scalars |
| eotf_pq(rgb,1) | consts pre-baked f32 (dyadic-exact): m1=2610/16384=0.1593017578125 (0x3E232000), m2=2523/32=78.84375 (0x429DB000), c1=107/128 (0x3F560000), c2=2413/128=18.8515625 (0x4196D000), c3=2392/128=18.6875 (0x41958000); per channel: spowf(ch,m1) [4×]; DIVIDE(ADD(c1,MUL(c2,ch)), ADD(1.0,MUL(c3,ch))) then spowf(·,m2) [7×]; ≈36× |
| eotf_hlg(rgb,1) | Yd=ADD(ADD(MUL(0.2627,rx),MUL(0.6780,ry)),MUL(0.0593,rz)) [5]; MUL(rgb·, spowf(Yd,−0.16666667f const 0xBE2AAAAB)) [4+3]; per ch: gate SUBTRACT(1,GT(ch,0.083333336f)) → Mix( B=MUL(3.0,ch)+SQRT, A=MUL(0.17883277, LOGARITHM(MUL(12.0,ch)−0.28466892, e_f32)) + 0.55991073 ); ≈60× |
| display_gamut_whitepoint | inline steps below (§B.6) |

**§B.6 display_gamut_whitepoint inline (src 593-731, per branch):**
1. `A598 rgb = vdot(matrix_p3d65_to_xyz, rgb)` (15×).
2. `D601 cwp_neutral = rgb` (channel alias).
3. `D603 cwp_f = POWER(tsn_arg, MUL(2.0, cwp_lm_socket))` (tsn_arg = tsn_const chain).
4. CAT ladder (live `cwp` socket; baked family): 5× COMPARE(eq cwp,k) shared +
   per-element chain of 5× Mix(FLOAT) selecting the matrix constant (candidates
   ascending k=1..5, acc = candidate 0; matrix constants as Mix default inputs):
   - gamut 0/1/2 → [cat_d65_to_d93, cat_d65_to_d75, IDENTITY, cat_d65_to_d60, cat_d65_to_d55, cat_d65_to_d50]
   - gamut 3 → [cat_d60_to_d93, cat_d60_to_d75, cat_d60_to_d65, IDENTITY, cat_d60_to_d55, cat_d60_to_d50]
   - gamut 4/5 → [cat_dci_to_d93, cat_dci_to_d75, cat_dci_to_d65, cat_dci_to_d60, cat_dci_to_d55, cat_dci_to_d50]
   IDENTITY = (1,0,0,0,1,0,0,0,1) — DCTL skips the vdot then; mixing then one
   mul-add is float-exact for it. Then ONE flat mul-add (15×) of the mixed matrix.
5. `A633 rgb = ADD(MUL(rgb_catted, cwp_f), MUL(cwp_neutral, SUB(1.0, cwp_f)))`;
   for family dci, cwp_neutral = flat mul-add of matrix_cat_dci_to_d65 (15×);
   families d65/d60: cwp_neutral = pre-ladder rgb.
6. Back-conversion (baked): gamut 0 → matrix_xyz_to_rec709; gamut 5 →
   matrix_cat_d65_to_dci; else matrix_xyz_to_p3d65 (15×).
7. `D656 cwp_norm = 1.0` + live-cwp ladder (5× COMPARE + 5× Mix, f32 consts):
   gamut 0 = [0.744192699063, 0.873470832146, 1.0, 0.955936992163, 0.905671332781, 0.850004385027]; gamut 1/2 = [0.762687057298, 0.884054083328, 1.0, 0.964320186739, 0.92307651886, 0.876572837784]; gamut 3 = [0.704956321013, 0.816715709816, 0.923382193663, 1.0, 0.956138500287, 0.906801453023]; gamut 4 = [0.665336141225, 0.770397131382, 0.870572343302, 0.891354547503, 0.855327825187, 0.814566436117]; gamut 5 = [0.707142784007, 0.815561082617, 0.91655527974 (cwp≥2 → all of 2..5)].
8. `A729 rgb = MUL(rgb, SUB(ADD(MUL(cwp_norm, cwp_f), 1.0), cwp_f))`.

## C. Statement-kind emission; naming; frames

**Walk:** iterate statements in src order with a per-branch symbol table
`var → {x,y,z}` of scalar chain-heads (a vector = tracked 3-tuple; last-write-
wins rewiring = C sequential semantics). declare/assign insert/replace;
if = compare tree (B-R-4) — every var assigned in a gated body that is used
later gets `prev = Mix(A=prev, B=body-value, fac=cond)` at gate close;
declare-with-no-init inside a gate (ptm_low_f 1123, ptm_high_f 1126) = Mix of
its two arm values against literal 1.0 (both arms always compute); return =
CombineColor → that branch's group output socket. select/stage/
skipped_preprocessor → 0 nodes.

- **Naming (validator contract, amended 2026-10-26):** call-site nodes and
  branch-group interiors `node.name = "b{B} r{firstsrc}#{k}"` (e.g.
  `b6 r1047#2`; k = 1-based per-statement expansion index, per-tree counter);
  fn-group interiors `"g r{firstsrc}#{k}"`. `node.label` = human text + full
  src range, e.g. `"compress_hyperbolic_power (src 1047-1049)"`. Folded
  constants get `#c{k}` (D7). The per-stage validator parses the name prefix
  → src (branch lives on the CALL-site node / branch group).
- **Frames: REMOVED (user decision 2026-10-26)** - NodeFrames and node
  layout are compiler-side decoration only; the consumer reads the group
  from outside, and the r{src} names already localize any emission bug to
  its DCTL statement. rt_smoke asserts zero frames.

## D. Exact tree composition

1. **Baked away (0 nodes):** select look (758-784) → socket defaults; select
   tonescale ×3 → default 0 = "USE LOOK PRESET" (bare-if chain: no entry fires
   at 0) → look's tn_* values stand; A 828 `clamp = 1` (DCTL 827-828) → _clamp
   socket default; select creative_white (831 rule, 833-838) → cwp/cwp_lm =
   look's values = socket defaults (the `_cwp` socket itself = stickshift
   semantics: direct 0..5, DCTL 54 — see §E, R-08); select display_encoding
   (858-902) → the 9 branches; D 908 + select input_gamut (909-923) →
   identity; A 933 linearize (tf=0 returns rgb); A 963 vdot(in_to_xyz)
   (identity) — **tree head = src 964**:
   `rgb = vdot(matrix_xyz_to_p3d65, SeparateImage.x/y/z)`.
2. **Shared cluster (once, live from tn_* sockets):** D 943 ts_x1 = pow(2, 6·tn_sh+4); 944 ts_y1 = tn_Lp/100; 945 ts_x0 = 0.18+tn_off; 946 ts_y0 = (tn_Lg/100)(1+tn_gb·log2(ts_y1)); 947 ts_s0 = ctq(ts_y0, tn_toe, 1); 949 ts_s10 = ts_x0(pow(ts_s0, −1/tn_con)−1); 950 ts_m1 = ts_y1/pow(ts_x1/(ts_x1+ts_s10), tn_con); 951 ts_m2 = ctq(ts_m1, tn_toe, 1); 952 ts_s = ts_x0(pow(ts_s0/ts_m2, −1/tn_con)−1); 956 pt_cmp_Lf = pt_hdr·min(1, (tn_Lp−100)/900); 958 s_Lp100 = ts_x0(pow(tn_Lg/100, −1/tn_con)−1); 959 ts_s1 = ts_s·pt_cmp_Lf + s_Lp100(1−pt_cmp_Lf). **Per-branch exceptions:** 948 ts_p = DIVIDE(tn_con, const: 1+f32(su·0.05f)); 953 ts_dsc (branch table).
3. **Per-branch naive dup of src 964..1210** (crv pruned): 964 head; 969 rs_w = f3(rs_rw, 1−rs_rw−rs_bw, rs_bw); 970 sat_L; 971 lerp; 975 rgb+=tn_off; 980 tsn = hypotf3(rgb)/SQRT3; 983 rgb = sdivf3f(rgb, tsn); 985-989 opp/ach_d (ach_d = 1.25·ctq(ach_d, 0.25, 0)); 992 hue = fmod(atan2(opp.x, opp.y)+PI+1.10714931f, 2·PI); 996-999 ha_rgb (3× gauss_window(hue_offset(h,·), 0.66: offsets 0.1/4.3/2.3)); 1002-1005 ha_rgb_hs (offsets −0.4/2.5, y passthrough); 1010-1013 ha_cmy (3× gauss 0.5: 3.3/1.3/−1.15); IF 1017-1022 brl (live _brl_enable; 1018 brl_tsf, 1019 brl_exf, 1020 brl_ex, 1021 tsn·=); IF 1025-1036 lcon (live _tn_lcon_enable; 1031 lcon_cnst_sc, 1032-1033 tsn chain); IF 1039-1044 hcon (live _tn_hcon_enable; 1040 hcon_p, 1041 contrast_high(tsn, hcon_p, tn_hcon_pv, tn_hcon_st, 0)); IF 1062-1076 hc (live _hc_enable: 1063 hc_ts, 1066 hc_c, 1067·=, 1068 pow, 1070 hc_f, 1071 rgb z·y scaled); IF 1080-1088 hs_rgb (live _hs_rgb_enable; 1081-1085 hs_rgb/hsf, 1086-1087 rgb+=hsf); IF 1091-1104 hs_cmy (live _hs_cmy_enable; 1092-1096, 1097-1099); 1109-1114 pt_lml_p/ptf/pt_lmh_p (ptf=POWER(ptf,·)); IF 1122-1130 ptm (live _ptm_enable; inner OR-gates 1124/1127 → Mix vs 1.0; 1129 ptf·=low_f·high_f); 1133 rgb = rgb·ptf+1−ptf; 1136-1137 inverse render space; 1140 display_gamut_whitepoint inline (§B.6); IF 1143-1152 brlp (live _brlp_enable; 1144-1150, 1151 rgb·=brlp_ex); IF 1155 ptl (live _ptl_enable; 3× softplus); 1158-1160 tsn·=ts_m2, ctq(tsn, tn_toe, 0), tsn·=ts_dsc; 1174 rgb·=tsn; IF 1177-1180 P3-limit tail — baked: ONLY gamut-2 branches (1178 clampminf3(rgb,0.0), 1179 vdot(matrix_p3_to_rec2020)); IF 1183 clamp (live _clamp → clampf3(rgb,0,1)+Mix); 1187-1189 EOTF select → static per branch (table); R 1210 CombineColor → output socket.
4. **crv overlay PRUNED (exact stmts the builder must skip):** D 925 (crv_tsn=0.0), D 926 (pos), D 927 (res), A 930 (oetf_filmlight_tlog), A 976 (crv_tsn+=tn_off), A 1035 + A 1043 (crv arms), D 1051 (crv_tsn_const=0.0), A 1053-1054, A 1163-1165, A 1167 (×10), D 1170 (crv_rgb), A 1171, A 1192-1194, D 1200 (crv_rgb_dst), D 1201 (crv_w0=0.35), A 1202-1204, D 1205 (crv_lm), A 1206-1207 (overlay blend). Not sockets: in_gamut, in_oetf, _tn_su, _display_gamut, _eotf, crv_enable, look/tonescale/display_encoding_preset.
5. **Live gates (Compare-tree+Mix):** brl_enable 1017, tn_lcon_enable 1025, tn_hcon_enable 1039, hc_enable 1062, hs_rgb_enable 1080, hs_cmy_enable 1091, ptm_enable 1122 (+inner 1124/1127), brlp_enable 1143, ptl_enable 1155, clamp 1183, + all inlined-function gates (spowf a≤0, ctq toe==0, ctc m==1, softplus, contrast_high, dgpw cwp/cwp_norm ladders). sdivf needs NO gate (div-by-0 → 0.0f = DCTL, measured).

**Branch table** (display_encoding entries = 9 outputs; verified vs DCTL 858-902):

| # | socket name | tn_su | gamut | eotf | ts_p divisor | ts_dsc | 1187-1189 body | CAT family / back matrix | extra tail |
|---|---|---|---|---|---|---|---|---|---|
| 0 | Rec.1886 | 1 | 0 Rec.709 | 2 | 1.05f | div(100.0f, tn_Lp) live | spowf3(rgb, f32(1/2.4f)=0x3ED55555) | d65 / matrix_xyz_to_rec709 | — |
| 1 | sRGB Display | 2 | 0 | 1 | 1.10f | 100/tn_Lp live | spowf3(rgb, f32(1/2.2f)=0x3EE8BA2E) | d65 / matrix_xyz_to_rec709 | — |
| 2 | Display P3 | 2 | 1 P3D65 | 1 | 1.10f | 100/tn_Lp live | spowf3(rgb, 1/2.2f) | d65 / matrix_xyz_to_p3d65 | — |
| 3 | P3-D60 | 0 | 3 P3D60 | 3 | 1.00f | 100/tn_Lp live | spowf3(rgb, f32(1/2.6f)=0x3EC4EC4F) | d60 / matrix_xyz_to_p3d65 | — |
| 4 | P3-DCI | 0 | 4 P3DCI | 3 | 1.00f | 100/tn_Lp live | spowf3(rgb, 1/2.6f) | dci / matrix_xyz_to_p3d65 | — |
| 5 | DCI P3 XYZ | 0 | 5 XYZ | 3 | 1.00f | 100/tn_Lp live | spowf3(rgb, 1/2.6f) | dci / matrix_cat_d65_to_dci (XYZ kept) | — |
| 6 | Rec.2100 PQ | 0 | 2 Rec.2020 | 4 | 1.00f | 0.01f const (0x3C23D70A) | eotf_pq(rgb,1) | d65 / matrix_xyz_to_p3d65 | clampminf3+p3_to_rec2020 |
| 7 | Rec.2100 HLG | 0 | 2 | 5 | 1.00f | 0.1f const (0x3DCCCCCD) | eotf_hlg(rgb,1) | d65 / matrix_xyz_to_p3d65 | same tail |
| 8 | Dolby PQ | 0 | 1 P3D65 | 4 | 1.00f | 0.01f const | eotf_pq(rgb,1) | d65 / matrix_xyz_to_p3d65 | — |

f32: f32(2.2)=0x400CCCCD, f32(2.4)=0x4019999A, f32(2.6)=0x40266666; ts_p
divisors f32(1.0+0.05f·su): 0x3F866666 (su=1), 0x3F8CCCCD (su=2), 1.0 (su=0);
`eotf_p` (1186) + `div(1.0,eotf_p)` bake to the col-6 constants per branch.
Estimated size: ≈370 nodes/branch ×9 → **~3.4k nodes** [INFERENCE; Build wave
re-measures; dgpw ≈90-115, contrast_high ≈20, hlg ≈60, pq ≈36].

## E. Socket table (68 inputs)

Socket types per DCTL control: check_box→Bool, slider_float→Float,
enum (_cwp)→**Int 0..5** (2026-10-26 user decision: integer socket for
enum clarity; int links coerce into the float COMPARE/LESS_THAN
ladders + dgpw float params implicitly - verified wheel+5.3; the D9
fractional-cwp arm becomes structurally unreachable). DCTL
stickshift define maps _cwp DIRECTLY to
internal cwp with options D93..D50 and default 2 (DCTL 54); the preset-mode
combo has idx 0 "USE LOOK PRESET" + 1..6 = D93..D50 (DCTL 126) — UI sugar,
unreachable with look baked (Standard look's cwp = 2 = D65 = socket default;
DCTL combo idx k∈{1..6} ↔ socket k−1).
DEFAULT = **the base socket-table value** (the Standard look's resolved
value, hex col, f32-exact) **overridden by §0a**: main carries the Base
look chain (DCTL 779-781), preset copies carry their own chain; the 5
non-chain knobs use the `_`-param UI defines (DCTL 17-20, 828). UI
min/max/step from the `_`-param defines → socket description.
Excluded (2026-10-27, user decision): `_pt_enable` "Enable Purity Compress
High" (DCTL 59) — the DCTL presets write `pt_enable` (759+) but no stage
reads it (purity compression is unconditional, 1104-1114), so exposing it
was a dead, greyed-out checkbox; the node omits it (DROPPED_PARAMS, D8).

| socket (name) | id | kind | f32 default (hex) | min/max/step | src |
|---|---|---|---|---|---|
| Display Peak Luminance | tn_Lp | float | 100.0 (0x42C80000) | 100..1000/1 | 17 |
| Display Grey Luminance | tn_Lg | float | 10.0 (0x41200000) | 3..25/0.1 | 20 |
| HDR Grey Boost | tn_gb | float | 0.12999999523162842 (0x3E051EB8) | 0..1/0.001 | 18 |
| Contrast | _tn_con | float | 1.659999966621399 (0x3FD47AE1) | 1..2/0.01 | 43 |
| Shoulder Clip | _tn_sh | float | 0.5 (0x3F000000) | 0..1/0.01 | 44 |
| Toe | _tn_toe | float | 0.003000000026077032 (0x3B449BA6) | 0..0.1/0.001 | 45 |
| Offset | _tn_off | float | 0.004999999888241291 (0x3BA3D70A) | 0..0.02/0.0002 | 46 |
| Enable Contrast High | _tn_hcon_enable | bool | 0 (0x00000000) | 0/1 | 47 |
| Contrast High | _tn_hcon | float | 0.0 | -1..1/0.02 | 48 |
| Contrast High Pivot | _tn_hcon_pv | float | 1.0 | 0..4/0.04 | 49 |
| Contrast High Strength | _tn_hcon_st | float | 4.0 | 0..4/0.04 | 50 |
| Enable Contrast Low | _tn_lcon_enable | bool | 0 | 0/1 | 51 |
| Contrast Low | _tn_lcon | float | 0.0 | 0..3/0.03 | 52 |
| Contrast Low Width | _tn_lcon_w | float | 0.5 | 0..2/0.02 | 53 |
| Creative White | _cwp | int | 2.0 (D65) | 0..5/1 | 54,126 |
| Creative White Limit | _cwp_lm | float | 0.25 | 0..1/0.01 | 55,127 |
| Render Space Strength | _rs_sa | float | 0.3499999940395355 (0x3EB33333) | 0..0.6/0.006 | 56 |
| Render Space Weight R | _rs_rw | float | 0.25 | 0..0.8/0.008 | 57 |
| Render Space Weight B | _rs_bw | float | 0.550000011920929 (0x3F0CCCCD) | 0..0.8/0.008 | 58 |
| Purity Limit Low | _pt_lml | float | 0.25 | 0..1/0.01 | 60 |
| Purity Limit Low R | _pt_lml_r | float | 0.5 | 0..1/0.01 | 61 |
| Purity Limit Low G | _pt_lml_g | float | 0.0 | 0..1/0.01 | 62 |
| Purity Limit Low B | _pt_lml_b | float | 0.10000000149011612 (0x3DCCCCCD) | 0..1/0.01 | 63 |
| Purity Limit High | _pt_lmh | float | 0.25 | 0..1/0.01 | 64 |
| Purity Limit High R | _pt_lmh_r | float | 0.5 | 0..1/0.01 | 65 |
| Purity Limit High B | _pt_lmh_b | float | 0.0 | 0..1/0.01 | 66 |
| Enable Purity Softclip | _ptl_enable | bool | 1 | 0/1 | 67 |
| Purity Softclip C | _ptl_c | float | 0.05999999865889549 (0x3D75C28F) | 0..0.25/0.0025 | 68 |
| Purity Softclip M | _ptl_m | float | 0.07999999821186066 (0x3DA3D70A) | 0..0.25/0.0025 | 69 |
| Purity Softclip Y | _ptl_y | float | 0.05999999865889549 (0x3D75C28F) | 0..0.25/0.0025 | 70 |
| Enable Mid Purity | _ptm_enable | bool | 1 | 0/1 | 71 |
| Mid Purity Low | _ptm_low | float | 0.4000000059604645 (0x3ECCCCCD) | 0..2/0.02 | 72 |
| Mid Purity Low Range | _ptm_low_rng | float | 0.25 | 0..1/0.01 | 73 |
| Mid Purity Low Strength | _ptm_low_st | float | 0.5 | 0.1..1/0.01 | 74 |
| Mid Purity High | _ptm_high | float | -0.800000011920929 (0xBF4CCCCD) | -0.9..0/0.01 | 75 |
| Mid Purity High Range | _ptm_high_rng | float | 0.3499999940395355 (0x3EB33333) | 0..1/0.01 | 76 |
| Mid Purity High Strength | _ptm_high_st | float | 0.4000000059604645 (0x3ECCCCCD) | 0.1..1/0.01 | 77 |
| Enable Brilliance | _brl_enable | bool | 1 | 0/1 | 78 |
| Brilliance | _brl | float | 0.0 | -6..2/0.08 | 79 |
| Brilliance R | _brl_r | float | -2.5 | -6..2/0.08 | 80 |
| Brilliance G | _brl_g | float | -1.5 | -6..2/0.08 | 81 |
| Brilliance B | _brl_b | float | -1.5 | -6..2/0.08 | 82 |
| Brilliance Range | _brl_rng | float | 0.5 | 0..1/0.01 | 83 |
| Brilliance Strength | _brl_st | float | 0.3499999940395355 (0x3EB33333) | 0..1/0.01 | 84 |
| Enable Post Brilliance | _brlp_enable | bool | 1 | 0/1 | 85 |
| Brilliance Post | _brlp | float | -0.5 | -1..0/0.01 | 86 |
| Post Brilliance R | _brlp_r | float | -1.25 | -3..0/0.03 | 87 |
| Post Brilliance G | _brlp_g | float | -1.25 | -3..0/0.03 | 88 |
| Post Brilliance B | _brlp_b | float | -0.25 | -3..0/0.03 | 89 |
| Enable Hue Contrast | _hc_enable | bool | 1 | 0/1 | 90 |
| Hue Contrast R | _hc_r | float | 1.0 | 0..2/0.02 | 91 |
| Hue Contrast R Range | _hc_r_rng | float | 0.30000001192092896 (0x3E99999A) | 0..1/0.01 | 92 |
| Enable Hueshift RGB | _hs_rgb_enable | bool | 1 | 0/1 | 93 |
| Hueshift R | _hs_r | float | 0.6000000238418579 (0x3F19999A) | 0..1/0.01 | 94 |
| Hueshift R Range | _hs_r_rng | float | 0.6 | 0..2/0.02 | 95 |
| Hueshift G | _hs_g | float | 0.3499999940395355 (0x3EB33333) | 0..1/0.01 | 96 |
| Hueshift G Range | _hs_g_rng | float | 1.0 | 0..2/0.02 | 97 |
| Hueshift B | _hs_b | float | 0.6600000262260437 (0x3F28F5C3) | 0..1/0.01 | 98 |
| Hueshift B Range | _hs_b_rng | float | 1.0 | 0..4/0.02 | 99 |
| Enable Hueshift CMY | _hs_cmy_enable | bool | 1 | 0/1 | 100 |
| Hueshift C | _hs_c | float | 0.25 | 0..1/0.01 | 101 |
| Hueshift C Range | _hs_c_rng | float | 1.0 | 0..1/0.01 | 102 |
| Hueshift M | _hs_m | float | 0.0 | 0..1/0.01 | 103 |
| Hueshift M Range | _hs_m_rng | float | 1.0 | 0..1/0.01 | 104 |
| Hueshift Y | _hs_y | float | 0.0 | 0..1/0.01 | 105 |
| Hueshift Y Range | _hs_y_rng | float | 1.0 | 0..1/0.01 | 106 |
| Clamp | _clamp | bool | 1 (DCTL 828) | 0/1 | 108 |

**Default-resolution gotcha:** look-chained defaults come from the Standard look
chain, NOT the `_`-param UI defines. Exactly TWO ids diverge:
`_tn_hcon_enable` (UI 1 → standard 0) and `_tn_lcon_enable` (UI 1 → standard 0);
a builder copying UI defines renders wrongly (extra contrast modules active).

**Outputs (9, NodeSocketColor):** names = branch table col 2; description =
"Target display: " + verbatim DCTL combo label — "Rec.1886 - 2.4 Power /
Rec.709", "sRGB Display - 2.2 Power / Rec.709", "Display P3 - 2.2 Power /
P3-D65", "DCI - 2.6 Power / P3-D60", "DCI - 2.6 Power / P3-DCI", "DCI - 2.6
Power / XYZ", "Rec.2100 - PQ / Rec.2020 (P3 Limited)", "Rec.2100 - HLG /
Rec.2020 (P3 Limited)", "Dolby - PQ / P3-D65". INTERFACE ORDER: sRGB
Display first (2026-10-26 change: a node dragged into an existing wire
auto-links to sRGB; builder `OUTPUT_UI_ORDER`; branch wiring keys on the
spec/DCTL table order regardless). Unlinked outputs are not
evaluated; each branch is an independent DAG ending at one output.

## F. Panel layout proposal

Root order: `Image (Linear CIE XYZ D65)`, 9 outputs, then panels
(NodeTreeInterfacePanel, `default_closed=True`, first panel open;
`interface.move` ordering; measured: `new_panel(name, description=…)` —
description keyword-only):

| # | panel (open?) | items in order |
|---|---|---|
| 1 | Exposure & Tonescale (open) | Display Peak Luminance, Display Grey Luminance, HDR Grey Boost, Contrast, Shoulder Clip, Toe, Offset |
| 2 | Creative White | Creative White, Creative White Limit |
| 3 | Look (Tonescale Contrast) | Enable Contrast High, Contrast High, Contrast High Pivot, Contrast High Strength, Enable Contrast Low, Contrast Low, Contrast Low Width |
| 4 | Saturation | Render Space Strength, Render Space Weight R, Render Space Weight B |
| 5 | Purity Compress | HDR Purity, Purity Limit Low, Low R, Low G, Low B, Purity Limit High, High R, High B, Enable Purity Softclip, Softclip C, M, Y, Enable Mid Purity, Mid Purity Low, Low Range, Low Strength, Mid Purity High, High Range, High Strength |
| 6 | Brilliance | Enable Brilliance, Brilliance, R, G, B, Brilliance Range, Brilliance Strength, Enable Post Brilliance, Brilliance Post, Post R, Post G, Post B |
| 7 | Hue Shifts | Enable Hue Contrast, Hue Contrast R, Range, Enable Hueshift RGB, Hueshift R, R Range, G, G Range, B, B Range, Enable Hueshift CMY, Hueshift C, C Range, M, M Range, Y, Y Range |
| 8 | Advanced | Clamp |

(HDR Purity `_pt_hdr` (DCTL 19) sits in Purity Compress — it drives pt_cmp_Lf.
DCTL tooltips → socket descriptions verbatim.)

## G. Risks / uncertain emissions (validator-relevant)

| # | risk | mitigation |
|---|---|---|
| R-01 | IR return-null @ sdivf 329 vs DCTL `return 0.0f` | 1× DIVIDE — measured Blender div-by-0 = 0.0 (CPU+GPU) = DCTL behavior; golden black-pixel case in tests/golden asserts |
| R-02 | DCTL `_`-fast intrinsics (_powf/_sqrtf/_expf/_exp10f/_logf/_fmod/_atan2f) ≈ fast-math — ULP divergence vs Blender f32 | validator vs C++-compiled DCTL (OracleShim) with ULP tolerance + per-stage boundary comparisons at src 964..1210 stage splits |
| R-03 | spowf negative/zero base | gate = le(a,0) Mix per §B; a=0 stays 0 (never POWER(0, negative)) |
| R-04 | fmod sign convention | **RESOLVED measured**: MODULO = GLSL fmod sign-of-dividend (-7%4=-3) = C fmod, CPU+GPU identical; fallback pattern unnecessary |
| R-05 | Math ops semantics | **RESOLVED measured** (PROBE_REPORT §math, CPU==GPU on 41 cases): COMPARE with epsilon input, DIVIDE/0 = 0.0 no NaN, POWER(neg,frac)=0.0, MODULO=fmod, CEIL/TRUNC/FRACT names; residual = ULP only (R-02 tolerance); never Color→Math (LUMA coercion, R-14) |
| R-06 | sRGB 2.2 vs Rec.1886 2.4 confusion | baked per-branch constants in §D table; validator asserts consts |
| R-07 | atan2 arg convention (IR keeps DCTL `_atan2f(opp.x, opp.y)` = CUDA atan2f(y,x)) | ARCTAN2(arg0, arg1); golden hue catches a flip |
| R-08 | `_cwp` preset-mode (0=USE LOOK) vs stickshift (direct 0..5) index semantics | contract: direct 0..5, default 2 = D65 = Standard look's cwp (verified DCTL 54 = 759); documented in socket description |
| R-09 | order-sensitive float equality ((1−rs_rw)−rs_bw, (x²+y²)+z², (rgb·ptf+1)−ptf) | IR-order verbatim emission; no reassociation/MULTIPLY_ADD; validator = same order |
| R-10 | divide-by-zero parity for sdivf sites | plain DIVIDE returns 0.0 (measured CPU+GPU) = DCTL sdivf; golden black-pixel case asserts |
| R-11 | ~3.4k-node tree; file size; 5.3 beta eval time | STATUS: naive v1 accepted (variant-dedup deferred); lazy outputs keep live eval = 1 branch |
| R-12 | hlg/pq per-channel Mix trees (~60/36 nodes) | kept inline per branch (no groups); validator stage boundaries at 1188/1189 clusters |
| R-13 | Feeding CIE-XYZ input: Convert Color Space exists (measured bit-identical 5.2.3 wheel vs 5.3.0 Beta; equals OpenDRT matrices for Rec.709/Rec.2020 to ~1e-6) but its ACEScg/ACES2065-1 transforms DIVERGE from OpenDRT (3.8e-3/4.5e-3) | node group must see RAW values (no Convert node in-graph; in_gamut is baked identity); Validate wave injects XYZ data without color-management re-interpretation (probe: tools/probes/probe_xyz_equiv.py) |
| R-14 | Color→Value implicit link conversion = Rec709 luma coefficients (0.2126/0.7152/0.0722, measured bit-exact) — silent corruption if a Color socket feeds Math/Mix inputs | rule B-R-5: SeparateColor at Image entry; all interior links float; CombineColor.mode='RGB' pinned; validator asserts no color→math links |

## Cross-checks vs DCTL (reported)

Hand-verified against `reference/OpenDRT_v1.1.0.dctl`:
1. Standard look chain 758-759 ↔ spec look[0] — **all 64 values verbatim** (tn_con 1.66f … hs_y_rng 1.0f).
2. `_rs_sa`/`_rs_bw` defines (56/58) = 0.35/0.55 = look Standard values ✓.
3. tn_Lp/tn_Lg/tn_gb/pt_hdr actives (17-20) = 100.0/10.0/0.13/0.5 = §E defaults ✓.
4. `_cwp` stickshift define (54, default 2, direct D93..D50) ↔ look Standard cwp=2 (759) ✓.
5. clamp hard-code 827-828 `clamp = 1` + `_clamp` UI 108 default 1 → socket 1 ✓.
6. All 9 display-encoding triples (858-902) = §D branch table ✓.
7. ts formulas 943-960 IR ↔ DCTL verbatim (incl. ctq(...,1) inv and `(float)tn_su*0.05f`) ✓.
8. sdivf 328-331 `return 0.0f` ✓ (R-01 authority).
9. eotf_pq 476-498 / eotf_hlg 453-473 constants + op order verbatim (PQ consts dyadic-exact in f32) ✓.
10. compress_toe_cubic 527 `4*p0*p0*p0` term ✓.
11. Pipeline spot-checks 969-992, 1105-1140, 1142-1210 IR↔DCTL verbatim ✓.

## Build blocker queue (IR shapes NOT mappable)

**None.** The single IR gap (sdivf return-null, R-01) was resolved from the
DCTL (0.0f = measured Blender DIVIDE-by-0). Math-op semantics were
**measured** (docs/PROBE_REPORT.md §math: 40-op enum, COMPARE epsilon input,
DIVIDE/0=0, POWER(neg,frac)=0, MODULO=fmod, CPU==GPU) and are folded into §B
rows. All 8 statement kinds and all 61 operator atoms have mapping rows; all 68
sockets have concrete f32 defaults.