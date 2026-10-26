#!/usr/bin/env python3
"""RT+smoke gate for dist/OpenDRT_v1.1.0.blend (run via the 5.3 binary).

  - main group "OpenDRT 1.1 Base" + the 7 official preset nodes
    "OpenDRT 1.1 [look]" exist (asset+fake-user; asset set is exactly those
    8; branch/fn/dgpw groups stay unmarked); identical interface shape.
  - spot-checked defaults: chain knobs per look entry (spec), non-chain
    knobs fixed.
  - node/link/frame counts + naming-prefix sanity.

Phase B (smoke render 64x64 Cycles CPU):
  - wrapper tree: test image (0.18 gray + saturated patches + white/black)
    -> group -> ONLY enc-0 "Rec.1886" linked -> Group Output.
  - render; assert finite + non-uniform; report 3 sample pixels.

Phase C (bonus parity): the same group rendered per patch-grid/neutral-ramp/
  hdr-sweep inputs vs the compiled DCTL shim (tests/golden inputs regenerated
  via tools/verify_shim.py functions; enc outputs compared).

Usage: $BLENDER_BIN -b --factory-startup /path/to/dist/OpenDRT_v1.1.0.blend \
         -P rt_smoke.py -- [--parity enc0|all] [--shim PATH]
"""
import json
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import bpy
sys.path.insert(0, str(Path("tools").resolve()))
import build_nodes as bn  # noqa: E402  (module import; main() is guarded)

SPEC = json.loads(Path("spec/opendrt-v1.1.0.json").read_text())
LOOKS = {e["name"]: e["values"] for e in SPEC["selection"]["look"]["entries"]}
LOOK_ORDER = [e["name"] for e in SPEC["selection"]["look"]["entries"]]
UI_KIND = {bn.ui_to_internal(r[1]): r[2] for r in bn.SOCKET_TABLE}
NAME_OF_UI = {bn.ui_to_internal(r[1]): r[0] for r in bn.SOCKET_TABLE}


def group_for(look):
    return bn.GROUP_NAME if look == "Base" else bn.PRESET_PREFIX + look

args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def argval(flag, default):
    return args[args.index(flag) + 1] if flag in args else default


PARITY = argval("--parity", "enc0")  # none | enc0 | patchall
SHIM = Path(argval("--shim", "tools/dctl_shim/build_dctl_shim"))


def argval(flag, default):
    return args[args.index(flag) + 1] if flag in args else default


BLEND_PATH = Path(argval("--blend", "dist/OpenDRT_v1.1.0.blend"))
PARITY = argval("--parity", "enc0")  # none | enc0 | all
SHIM = Path(argval("--shim", "tools/dctl_shim/build_dctl_shim"))

FAILS = []


def check(name, cond, extra=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name} {extra}", flush=True)
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# Phase A - round-trip
# ---------------------------------------------------------------------------
ng = bpy.data.node_groups.get(bn.GROUP_NAME)
check("group-exists", ng is not None)
if ng is None:
    bpy.ops.wm.quit_blender()
    sys.exit(1)

check("tree-type", ng.type == "COMPOSITING" or ng.bl_idname == "CompositorNodeTree",
      str(ng.bl_idname))
check("asset-marked", ng.asset_data is not None)
check("fake-user", bool(ng.use_fake_user), f"use_fake_user={ng.use_fake_user}")

items = list(ng.interface.items_tree)
outs = [i for i in items if i.item_type == "SOCKET" and
        i.in_out == "OUTPUT"]
ins = [i for i in items if i.item_type == "SOCKET" and
       i.in_out == "INPUT"]
panels = [i for i in items if i.item_type == "PANEL"]
floats = [i for i in ins if i.socket_type in ("FLOAT", "NodeSocketFloat")]
bools = [i for i in ins if i.socket_type in ("BOOL", "NodeSocketBool")]
colors_out = [i for i in outs
              if i.socket_type in ("COLOR", "NodeSocketColor")]
check("input-count",
      len([i for i in ins
           if i.socket_type in ("COLOR", "NodeSocketColor")]) == 1,
      f"{len([i for i in ins
              if i.socket_type in ('COLOR', 'NodeSocketColor')])}")
check("param-count", len(floats) + len(bools) == 69,
      f"floats={len(floats)} bools={len(bools)}")
check("output-count", len(colors_out) == 9, f"{len(colors_out)}")
check("panel-count", len(panels) == 8, f"{len(panels)}")
panel_names = [p.name for p in panels]
expected_panels = ["Exposure & Tonescale", "Creative White",
                   "Look (Tonescale Contrast)", "Saturation",
                   "Purity Compress", "Brilliance", "Hue Shifts", "Advanced"]
check("panel-names-order", panel_names == expected_panels, str(panel_names))
if panels:
    check("panel-1-open", panels[0].default_closed is False)
    check("panels-2..8-collapsed",
          all(p.default_closed is True for p in panels[1:]))

EXP_OUT_NAMES = ["Rec.1886", "sRGB Display", "Display P3", "P3-D60",
                 "P3-DCI", "DCI P3 XYZ", "Rec.2100 PQ", "Rec.2100 HLG",
                 "Dolby PQ"]
check("output-names-order", [o.name for o in outs] == EXP_OUT_NAMES,
      str([o.name for o in outs]))

# default asserts: the 5 non-chain knobs are fixed for every preset (UI
# defines); the 64 chain knobs are asserted per look against the spec entry.
GROUPS = {}
for look in LOOK_ORDER:
    GROUPS[look] = bpy.data.node_groups.get(group_for(look))
    check(f"exists:{look}", GROUPS[look] is not None, group_for(look))
NON_CHAIN_SPOTS = [
    ("Display Peak Luminance", 100.0, "0x42C80000", None, None),
    ("Display Grey Luminance", 10.0, "0x41200000", 3.0, 25.0),
    ("HDR Grey Boost", 0.13, "0x3E051EB8", None, None),
    ("HDR Purity", 0.5, None, None, None),
    ("Render Space Weight B", None, "0x3F0CCCCD", None, None),
    ("Clamp", True, None, None, None),
]
by_name = {i.name: i for i in ins if i.item_type == "SOCKET"}
for name, num, hexv, mn, mx in NON_CHAIN_SPOTS:
    it = by_name.get(name)
    if it is None:
        check(f"default:{name}", False, "missing")
        continue
    want = struct.unpack("f", struct.pack("I", int(hexv, 16)))[0] \
        if hexv is not None else num
    got = it.default_value
    check(f"default:{name}", float(got) == want, f"got {float(got)!r} want {want}")
    if mn is not None:
        check(f"minmax:{name}", it.min_value == mn and it.max_value == mx,
              f"got {it.min_value}/{it.max_value}")
CHAIN_SPOTS = ["tn_con", "tn_lcon_w", "hs_b", "brlp_r", "brl_enable",
               "tn_hcon_enable", "cwp", "ptl_c"]
for look, vals in LOOKS.items():
    grp = GROUPS[look]
    if grp is None:
        continue
    gby = {i.name: i for i in grp.interface.items_tree
           if i.item_type == "SOCKET" and i.in_out == "INPUT"}
    for uid in CHAIN_SPOTS:
        nm, kind = NAME_OF_UI[uid], UI_KIND[uid]
        it = gby.get(nm)
        if it is None:
            check(f"default:{look}:{nm}", False, "missing")
            continue
        want = bool(int(vals[uid])) if kind == "bool" \
            else float(np.float32(vals[uid]))
        check(f"default:{look}:{nm}", float(it.default_value) == want,
              f"got {float(it.default_value)!r} want {want}")
    rng = {i.name: (i.min_value, i.max_value)
           for i in grp.interface.items_tree
           if i.item_type == "SOCKET" and i.in_out == "INPUT"
           and i.socket_type in ("FLOAT", "NodeSocketFloat")}
    check(f"minmax:{look}:Contrast", rng.get("Contrast") == (1.0, 2.0),
          str(rng.get("Contrast")))
    check(f"minmax:{look}:Creative White", rng.get("Creative White") == (0.0, 5.0),
          str(rng.get("Creative White")))

inp = ins[0]
check("image-desc", "Linear CIE XYZ D65" in (inp.description or ""),
      (inp.description or "")[:60])

# outputs have target-display descriptions
check("output-descs", all(o.description.startswith("Target display: ")
                          for o in outs))

# 2026-10-26 architecture: nested groups, NO NodeFrames, no fn inlining
frame_count = sum(1 for g in bpy.data.node_groups
                  for n in g.nodes if n.bl_idname == "NodeFrame")
check("no-frames", frame_count == 0, f"{frame_count}")
branch_trees = [g for g in bpy.data.node_groups
                if g.name.startswith("OpenDRT b")]
check("nine-branch-groups", len(branch_trees) == 9, f"{len(branch_trees)}")
fn_trees = [g for g in bpy.data.node_groups
            if g.name.startswith("OpenDRT fn ")]
check("fn-groups", len(fn_trees) >= 10, f"{len(fn_trees)} fn groups")

for look, grp in GROUPS.items():
    if grp is None or look == "Base":
        continue
    ins_l = [i for i in grp.interface.items_tree
             if i.item_type == "SOCKET" and i.in_out == "INPUT"]
    outs_l = [i for i in grp.interface.items_tree
              if i.item_type == "SOCKET" and i.in_out == "OUTPUT"]
    inst = [n for n in grp.nodes if n.bl_idname == "CompositorNodeGroup"]
    check(f"wrapper:{look}:nodes", len(grp.nodes) == 3 and len(inst) == 1
          and inst[0].name == "base"
          and inst[0].node_tree.name == bn.GROUP_NAME,
          f"nodes={len(grp.nodes)} inst={len(inst)} tree="
          f"{inst[0].node_tree.name if inst else None}")
    into_base = {lk.to_socket.name: lk.from_socket.name
                 for lk in grp.links
                 if lk.to_node.bl_idname == "CompositorNodeGroup"}
    want_in = {i.name for i in ins_l}
    check(f"wrapper:{look}:1to1-inputs",
          set(into_base) == want_in
          and all(into_base[n] == n for n in want_in),
          f"{len(into_base)}/{len(want_in)}")
    from_base = {lk.from_socket.name: lk.to_socket.name
                 for lk in grp.links
                 if lk.from_node.bl_idname == "CompositorNodeGroup"}
    want_out = {o.name for o in outs_l}
    check(f"wrapper:{look}:1to1-outputs",
          set(from_base) == want_out
          and all(from_base[o] == o for o in want_out),
          f"{len(from_base)}/{len(want_out)}")
    check(f"shape:{look}:outputs",
          [o.name for o in outs_l] == EXP_OUT_NAMES,
          str([o.name for o in outs_l]))
# main (Base): global shape over all trees - one sep, nine combines (one per
# branch tree), nine branch instances in main
seps_all = sum(1 for g in bpy.data.node_groups for n in g.nodes
               if n.bl_idname == "CompositorNodeSeparateColor")
combs_all = sum(1 for g in bpy.data.node_groups for n in g.nodes
                if n.bl_idname == "CompositorNodeCombineColor")
check("one-separate-total", seps_all == 1, f"{seps_all}")
check("nine-combines-total", combs_all == 9, f"{combs_all}")
gins = [n for n in ng.nodes if n.bl_idname == "NodeGroupInput"]
gouts = [n for n in ng.nodes if n.bl_idname == "NodeGroupOutput"]
check("single-gin-gout", len(gins) == 1 and len(gouts) == 1)
branch_instances = [n for n in ng.nodes if n.bl_idname == "CompositorNodeGroup"]
check("nine-branch-instances", len(branch_instances) == 9,
      f"{len(branch_instances)}")

# naming contract per tree: b{B} r{src}#{k} (call-site + branch interiors)
# or "g r{src}#{k}" (fn-group interiors)
bad = []
tags = set()
for g in bpy.data.node_groups:
    for n in g.nodes:
        nm = n.name
        if nm.startswith("b") and " r" in nm and "#" in nm:
            tags.add(nm.split(" ")[0])
        elif nm.startswith("g r") and "#" in nm:
            tags.add("g")
        elif n.bl_idname == "CompositorNodeGroup" and nm == "base":
            pass  # preset wrapper: the single OpenDRT 1.1 Base instance
        elif n.bl_idname not in ("NodeGroupInput", "NodeGroupOutput"):
            bad.append(f"{g.name}/{nm}")
check("naming-contract", len(bad) == 0, f"{len(bad)} bad names")
check("naming-branches", {f"b{i}" for i in range(9)} <= tags, str(sorted(tags)))

# assets: exactly the 8 user-facing groups (main + 7 presets); plumbing stays
# unmarked; fake-user on everything written
assets = sorted(g.name for g in bpy.data.node_groups
                if g.asset_data is not None)
check("asset-set", assets == sorted(group_for(l) for l in LOOK_ORDER),
      str(assets))
check("all-fake-user", all(g.use_fake_user for g in bpy.data.node_groups))


# R-14: no RGBA links into math nodes
bad_links = 0
for lk in ng.links:
    if lk.from_socket.type == "RGBA" and lk.to_node.bl_idname in (
            "ShaderNodeMath",):
        bad_links += 1
    if lk.from_socket.type == "RGBA" and lk.to_node.bl_idname == "ShaderNodeMix":
        bad_links += 1
check("no-color-to-math", bad_links == 0, f"{bad_links}")
print(f"COUNTS main nodes={len(ng.nodes)} main links={len(ng.links)} "
      f"trees={len(bpy.data.node_groups)} "
      f"total_nodes={sum(len(g.nodes) for g in bpy.data.node_groups)} "
      f"total_links={sum(len(g.links) for g in bpy.data.node_groups)}", flush=True)
NODES_TOTAL = len(ng.nodes)
LINKS_TOTAL = len(ng.links)

# ---------------------------------------------------------------------------
# Phase B - smoke render (64x64)
# ---------------------------------------------------------------------------
RES = 64

test = np.zeros((RES * RES, 3), dtype=np.float32)
D65 = (0.95046, 1.0, 1.08906)
for y in range(RES):  # y = image row from bottom (blender buffer order)
    for x in range(RES):
        i = y * RES + x
        if y < 16:
            px = (D65[0] * 1.0, 1.0, D65[2] * 1.0)          # D65 white Y=1
        elif y < 32:
            px = (D65[0] * 0.18, 0.18, D65[2] * 0.18)       # 0.18 D65 gray
        elif y < 40:
            px = (0.349091, 0.18, 0.016364)                  # sat red (709 prim)
        elif y < 48:
            px = (0.0325, 0.013, 0.171167)                   # sat blue
        elif y < 56:
            px = (0.186667, 0.08, 0.266667)                  # magenta vivid
        else:
            px = (0.171083, 0.18, -0.05)                     # incl. negative-Z
        test[i] = px

scene_setup_calls = []


LENFILES = {"patch": (5, 8), "ramp": (64, 64), "hdr": (64, 32)}
LENDIMS = {"enc0": (64, 64)}
def render_group(gname, enc_index, triplets, w, h,
                 out_png=None, exr="/tmp/smoke.exr"):
    """Render triplets (len = w*h) through group output enc_index; return px."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 1
    sc.cycles.use_denoising = False
    sc.cycles.use_adaptive_sampling = False
    sc.render.resolution_x = w
    sc.render.resolution_y = h
    sc.render.resolution_percentage = 100
    sc.render.compositor_device = "CPU"
    sc.render.compositor_precision = "FULL"
    sc.render.use_compositing = True
    sc.view_settings.view_transform = "Standard"
    ims = sc.render.image_settings
    ims.file_format = "OPEN_EXR"
    ims.color_depth = "32"
    ims.exr_codec = "NONE"
    w_ = bpy.data.worlds.new("W")
    w_.color = (0.0, 0.0, 0.0)
    sc.world = w_
    cam = bpy.data.objects.new("C", bpy.data.cameras.new("C"))
    sc.collection.objects.link(cam)
    sc.camera = cam
    sc.frame_start = sc.frame_end = 1

    # load the group from the library file
    with bpy.data.libraries.load(str(BLEND_PATH), link=False) as (src, dst):
        dst.node_groups = [name for name in src.node_groups if name == gname]
    g = bpy.data.node_groups.get(gname)
    assert g is not None

    img = bpy.data.images.new("SmokeIn", w, h, float_buffer=True)
    img.colorspace_settings.name = "Non-Color"
    flat = []
    for r, g_, b in triplets:
        flat += [r, g_, b, 1.0]
    img.pixels.foreach_set(flat)

    tree = bpy.data.node_groups.new("Wrapper", "CompositorNodeTree")
    sc.compositing_node_group = tree
    tree.interface.new_socket("Image", in_out="OUTPUT",
                              socket_type="NodeSocketColor")
    wr_out = tree.nodes.new("NodeGroupOutput")
    nimg = tree.nodes.new("CompositorNodeImage")
    nimg.image = img
    gnode = tree.nodes.new("CompositorNodeGroup")
    gnode.node_tree = g
    # instance sockets initialize from the interface automatically
    for it in g.interface.items_tree:
        pass  # (defaults live in the interface; instance copies mirror them)
    tree.links.new(nimg.outputs[0], gnode.inputs["Image"])
    tree.links.new(gnode.outputs[enc_index], wr_out.inputs[0])
    p = Path(exr)
    sc.render.filepath = str(p)
    bpy.ops.render.render(write_still=True)
    im = bpy.data.images.load(str(p))
    im.colorspace_settings.name = "Non-Color"
    buf = np.array(im.pixels[:], dtype=np.float32)
    bpy.data.images.remove(im)
    n = buf.size // 4
    rgb = buf.reshape(-1, 4)[:, :3][:n]
    return rgb


print("PHASE: smoke (enc0, 64x64)", flush=True)
smoke = render_group(bn.GROUP_NAME, 0, test, RES, RES, exr="/tmp/smoke.exr")
vals = smoke.reshape(-1, 3)
finite = bool(np.isfinite(vals).all())
uniq = len(np.unique(vals.round(5), axis=0))
check("smoke-finite", finite)
check("smoke-bands", uniq == 6,
      f"unique={uniq} (pattern is 6 flat color bands)")
def px_at(x, y):
    i = y * RES + x
    return tuple(round(float(v), 6) for v in vals[i])

s1, s2, s3 = px_at(0, 0), px_at(32, 24), px_at(16, 36)
print(f"SAMPLES white(0,0)={s1} gray0.18(32,24)={s2} satred(16,36)={s3}",
      flush=True)
check("samples-finite", all(all(np.isfinite(v) for v in s) for s in (s1, s2, s3)))

# ---------------------------------------------------------------------------
# Phase C - parity vs compiled DCTL shim
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path("tools").resolve()))
import verify_shim  # noqa: E402


def fmt_shim(v):
    """f32-exact value serialization: C strtof(str) round-trips f32 bits."""
    return str(int(v)) if float(v) == int(v) else repr(float(np.float32(v)))


def shim_run(enc, triplets, overrides=None):
    cmd = [str(SHIM), "--enc", str(enc)]
    for k, v in (overrides or {}).items():
        cmd += ["--param", f"{k}={fmt_shim(v)}"]
    proc = subprocess.run(cmd, input=verify_shim.pack_triplets(triplets),
                          capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"shim rc={proc.returncode}: "
                           f"{proc.stderr.decode()[:200]}")
    return np.frombuffer(proc.stdout, dtype="<f4").reshape(-1, 3)


print("PHASE: parity", flush=True)

# The shim is pinned to look=Standard; per-look parity reaches the preset
# state via --param post-selection overrides with the look's chain values
# (the same values the preset node's sockets default to - see builder).
triplets = [(v, v, v) for v in [i / 4095 for i in range(4096)]]
for look in LOOK_ORDER:
    exp = shim_run(0, triplets, overrides=LOOKS[look])
    got = render_group(group_for(look), 0, triplets, 64, 64,
                       exr=f"/tmp/parity_{LOOK_ORDER.index(look)}.exr")
    d = np.abs(exp.reshape(-1, 3) - got.reshape(-1, 3))
    print(f"PARITY {look} enc0 ramp4096 max_abs={d.max():.5g} "
          f"p99={np.percentile(d, 99):.5g}", flush=True)
    check(f"parity:{look}:enc0-ramp", float(d.max()) <= 1e-4,
          f"max={float(d.max()):.3g}")
if PARITY == "all":
    patch = [(x, y, z) for _, x, y, z in verify_shim.PATCH_GRID]
    exp = shim_run(0, patch, overrides=LOOKS["Base"])
    got = render_group(bn.GROUP_NAME, 0, patch, 5, 8,
                       exr="/tmp/parity_base_pg.exr")
    d = np.abs(exp - got)
    check("parity:Base:patch-enc0", float(d.max()) <= 1e-4,
          f"max={float(d.max()):.3g}")
    for enc in range(9):
        got = render_group(bn.GROUP_NAME, enc, triplets, 64, 64,
                           exr=f"/tmp/parity_ramp_{enc}.exr")
        exp = shim_run(enc, triplets, overrides=LOOKS["Base"])
        d = np.abs(exp.reshape(-1, 3) - got.reshape(-1, 3))
        print(f"PARITY Base ramp enc{enc} max_abs={d.max():.5g}", flush=True)
        check(f"parity:Base-ramp-enc{enc}", float(d.max()) <= 1e-4,
              f"max={float(d.max()):.3g}")

print("RESULT", "PASS" if not FAILS else f"FAIL: {FAILS}",
      f"nodes={NODES_TOTAL} links={LINKS_TOTAL}", flush=True)