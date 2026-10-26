"""PROBE 3 - Node tree interface API facts for CompositorNodeTree (Blender 5.x)
+ round-trip of panels/sockets/limits through the real 5.3 binary.

Roles:
  introspect  - API surface of interface.new_panel/new_socket/move/move_to_parent
                (run in BOTH runtimes; prints everything)
  writer      - build RT_Group with panels+sockets, write /tmp/opendrt_probes/if_writer.blend
                (run in the bpy 5.2.3 wheel)
  binary      - open writer blend in the 5.3 binary, ASSERT structure, place the
                group instance in a compositor tree, save bin_tree.blend
  verify      - wheel opens bin_tree.blend and re-ASSERTs everything survived

Assertions FAIL => nonzero exit. JSON facts printed on stdout.

  pixi run python tools/probes/probe_interface.py --role introspect --wheel
  pixi run python tools/probes/probe_interface.py --role writer --wheel
  env BLENDER_BIN=... $BLENDER_BIN -b /tmp/opendrt_probes/if_writer.blend -P tools/probes/probe_interface.py -- --role binary
  pixi run python tools/probes/probe_interface.py --role verify --wheel
"""
import argparse
import json
import sys

from pathlib import Path

import bpy

OUT_DIR = Path("/tmp/opendrt_probes")
OUT_DIR.mkdir(parents=True, exist_ok=True)

GROUP_NAME = "RT_Group"
EXPECTED = {
    "order": ["Image Out", "Clamp Out", "Image", "Panel A", "Panel B"],
    "panels": {
        "Panel A": {"description": "panel A desc", "default_closed": False,
                    "inputs": ["Exp", "Clamp"]},
        "Panel B": {"description": "panel B desc", "default_closed": True,
                    "inputs": ["Tint", "Gamma", "Iters"]},
    },
    "inputs": {
        "Image": {"type": "NodeSocketColor", "default": [0.1, 0.2, 0.3, 1.0], "description": "input image"},
        "Panel A/Exp": {"type": "NodeSocketFloat", "default": 0.18, "min": 0.0, "max": 1.0, "description": "exposure"},
        "Panel A/Clamp": {"type": "NodeSocketBool", "default": False, "description": "clamp on"},
        "Panel B/Tint": {"type": "NodeSocketColor", "default": [0.25, 0.5, 0.75, 1.0], "description": "tint"},
        "Panel B/Gamma": {"type": "NodeSocketFloat", "default": -2.5, "min": -10.0, "max": 10.0, "description": "gamma"},
        "Panel B/Iters": {"type": "NodeSocketInt", "default": 3, "min": 0, "max": 16, "description": "iterations"},
    },
    "outputs": [
        {"name": "Image Out", "type": "NodeSocketColor", "description": "out image"},
        {"name": "Clamp Out", "type": "NodeSocketBool", "description": "out clamp"},
    ],
}

FAILURES = []


def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    if not ok:
        FAILURES.append(label + (" :: " + detail if detail else ""))


def parent_name(it):
    p = getattr(it, "parent", None)
    if p is None:
        return None
    try:
        return p.name or None  # null bpy pointer yields '' for top-level items
    except Exception:
        return None


def find_panel(tree, name):
    for it in tree.interface.items_tree:
        if it.item_type == "PANEL" and it.name == name:
            return it
    return None


def assert_tree_structure(tree):
    """EXPECT vs current tree.interface; used by binary + verify roles."""
    top = [it.name for it in tree.interface.items_tree if parent_name(it) is None]
    check("interface top-level order", top == EXPECTED["order"], f"got {top}")

    for pname, pspec in EXPECTED["panels"].items():
        p = find_panel(tree, pname)
        check(f"panel {pname} exists", p is not None)
        if p is None:
            continue
        check(f"panel {pname} default_closed", p.default_closed == pspec["default_closed"],
              f"got {p.default_closed}")
        desc = getattr(p, "description", "<missing>")
        check(f"panel {pname} description", desc == pspec["description"], f"got {desc!r}")
        sock_names = [s.name for s in tree.interface.items_tree
                      if s.item_type == "SOCKET" and parent_name(s) == pname and s.in_out == "INPUT"]
        check(f"panel {pname} socket order", sock_names == pspec["inputs"], f"got {sock_names}")

    for key, ispec in EXPECTED["inputs"].items():
        panel_name = key.split("/")[0] if "/" in key else None
        sname = key.split("/")[-1]
        parent = find_panel(tree, panel_name) if panel_name else None
        it = None
        for cand in tree.interface.items_tree:
            if cand.item_type == "SOCKET" and cand.name == sname and \
               cand.in_out == "INPUT" and parent_name(cand) == (panel_name if parent else None):
                it = cand
                break
        check(f"socket {key} exists", it is not None)
        dv = it.default_value
        dv = [float(v) for v in dv] if hasattr(dv, "__iter__") else float(dv)
        if isinstance(ispec["default"], (int, float, bool)) and not hasattr(dv, "__iter__"):
            check(f"socket {key} default (tol 1e-6)", abs(dv - ispec["default"]) <= 1e-6, f"got {dv}")
        else:
            check(f"socket {key} default", len(dv) == len(ispec["default"]) and all(
                abs(a - b) <= 1e-6 for a, b in zip(dv, ispec["default"])), f"got {dv}")
        for lim in ("min_value", "max_value"):
            if lim in ispec:
                check(f"socket {key} {lim}", getattr(it, lim) == ispec[lim],
                      f"got {getattr(it, lim)}")
        desc = getattr(it, "description", "<missing>")
        check(f"socket {key} description", desc == ispec["description"], f"got {desc!r}")

    outs = [it.name for it in tree.interface.items_tree
            if it.item_type == "SOCKET" and it.in_out == "OUTPUT"]
    check("output sockets order", outs == [o["name"] for o in EXPECTED["outputs"]], f"got {outs}")
    out_types = {it.name: it.bl_socket_idname for it in tree.interface.items_tree
                 if it.item_type == "SOCKET" and it.in_out == "OUTPUT"}
    for o in EXPECTED["outputs"]:
        check(f"output {o['name']} type", out_types.get(o["name"]) == o["type"], f"got {out_types.get(o['name'])}")
    check("NodeFrame inside group", any(n.bl_idname == "NodeFrame" for n in tree.nodes),
          f"nodes: {[(n.bl_idname) for n in tree.nodes]}")


def role_introspect():
    tree = bpy.data.node_groups.new("Disc", "CompositorNodeTree")
    r = {}
    p1 = tree.interface.new_panel("P1", description="d1", default_closed=False)
    p1props = [p.identifier for p in p1.bl_rna.properties if not p.identifier.startswith("bl_")]
    r["panel_props"] = p1props
    r["panel_defaults"] = {k: getattr(p1, k) for k in ("name", "description", "default_closed", "panel_depth") if hasattr(p1, k)}
    r["panel_attrs_extra"] = [k for k in dir(p1) if not k.startswith("_")][:40]
    # ordering control
    p2 = tree.interface.new_panel("P2", description="d2", default_closed=False)
    s1 = tree.interface.new_socket("S1", in_out="INPUT", socket_type="NodeSocketFloat", parent=p1)
    s2 = tree.interface.new_socket("S2", in_out="INPUT", socket_type="NodeSocketFloat", parent=p1)
    s3 = tree.interface.new_socket("S3", in_out="INPUT", socket_type="NodeSocketFloat", parent=p1)
    p1name = p1.name
    p2name = p2.name
    order0 = [s.name for s in tree.interface.items_tree if s.item_type == "SOCKET" and getattr(s, "parent", None) is not None and s.parent.name == p1name]
    tree.interface.move(s3, 0)
    order1 = [s.name for s in tree.interface.items_tree if s.item_type == "SOCKET" and getattr(s, "parent", None) is not None and s.parent.name == p1name]
    r["panel_order_after_move_socket_first"] = order1
    tree.interface.move(s1, 2)
    order2 = [s.name for s in tree.interface.items_tree if s.item_type == "SOCKET" and getattr(s, "parent", None) is not None and s.parent.name == p1name]
    r["panel_socket_order_final"] = order2
    top0 = [it.name for it in tree.interface.items_tree]
    tree.interface.move(p2, 0)
    top1 = [it.name for it in tree.interface.items_tree]
    r["panel_move_reorders_top"] = {"before": top0, "after": top1}
    # nested panel
    child = tree.interface.new_panel("Child", description="", default_closed=False)
    tree.interface.move_to_parent(child, p2, 0)
    r["child_in_parent_p2"] = [it.name for it in tree.interface.items_tree
                               if it.item_type == "PANEL" and getattr(it, "parent", None) is not None and it.parent.name == p2name]
    # cross-panel socket move
    tree.interface.move_to_parent(s1, p2, 0)
    r["s1_moved_to_p2"] = [s.name for s in tree.interface.items_tree
                           if s.item_type == "SOCKET" and getattr(s, "parent", None) is not None and s.parent.name == p2name]
    # socket-type acceptance
    ok_types, bad = [], {}
    for t in ("NodeSocketFloat", "NodeSocketColor", "NodeSocketBool", "NodeSocketInt",
              "NodeSocketString", "NodeSocketVector", "NodeSocketMenu", "NodeSocketShader"):
        try:
            tree.interface.new_socket("probe", in_out="INPUT", socket_type=t)
            ok_types.append(t)
        except Exception as e:
            bad[t] = str(e)[:120]
        it = [x for x in tree.interface.items_tree if x.name == "probe"]
        if it:
            tree.interface.remove(it[0])
    r["socket_types_ok"] = ok_types
    r["socket_types_rejected"] = bad
    # bool default + roundtrip
    doomed = [it for it in tree.interface.items_tree if it.item_type == "SOCKET"]
    for it in doomed:
        if it.bl_socket_idname in ("NodeSocketFloat", "NodeSocketColor", "NodeSocketInt", "NodeSocketBool", "NodeSocketString", "NodeSocketVector", "NodeSocketMenu"):
            try:
                tree.interface.remove(it)
            except Exception:
                pass
    b = tree.interface.new_socket("B", in_out="INPUT", socket_type="NodeSocketBool")
    dv0 = b.default_value
    b.default_value = True
    r["bool_default"] = {"initial": dv0, "after_set": b.default_value}
    f = tree.interface.new_socket("F", in_out="INPUT", socket_type="NodeSocketFloat")
    f.default_value = 0.18
    f.min_value = 0.0
    f.max_value = 1.0
    r["float_minmax_roundtrip"] = {"min": f.min_value, "max": f.max_value, "value": f.default_value}
    r["float_socket_writable_props"] = [p.identifier for p in f.bl_rna.properties
                                        if not p.identifier.startswith("bl_") and not p.is_readonly]
    r["socket_has_description"] = hasattr(f, "description")
    f.description = "legend text"
    r["float_description"] = f.description
    # NodeFrame legality
    try:
        tree.nodes.new("NodeFrame")
        r["node_frame_in_compositor_group"] = "legal"
    except Exception as e:
        r["node_frame_in_compositor_group"] = "REJECTED: " + str(e)[:120]
    # instance side in plain compositor tree
    outer = bpy.data.node_groups.new("Outer", "CompositorNodeTree")
    g = outer.nodes.new("CompositorNodeGroup")
    g.node_tree = tree
    inst_in = {s.name: (s.bl_idname, [round(float(v), 4) for v in s.default_value] if hasattr(s, "default_value") and isinstance(s.default_value, (tuple, list)) else s.default_value) for s in g.inputs}
    r["instance_input_names"] = list(inst_in)
    r["instance_float_socket_props"] = [p.identifier for p in g.inputs[1].bl_rna.properties if not p.identifier.startswith("bl_")]
    try:
        g.inputs[1].value = 2.0
        r["instance_clamp_on_assign"] = g.inputs[1].value
    except Exception as e:
        r["instance_clamp_on_assign"] = "ERR " + str(e)[:100]
    print(json.dumps(r, indent=2, default=str))
    if FAILURES:
        print("FAILURES:", FAILURES)
        sys.exit(1)
    print("PROBE3_INTROSPECT_DONE")


def build_rt_group():
    tree = bpy.data.node_groups.new(GROUP_NAME, "CompositorNodeTree")
    pa = tree.interface.new_panel("Panel A", description="panel A desc", default_closed=False)
    pb = tree.interface.new_panel("Panel B", description="panel B desc", default_closed=True)
    exp = tree.interface.new_socket("Exp", in_out="INPUT", socket_type="NodeSocketFloat", parent=pa)
    exp.default_value = 0.18
    exp.min_value = 0.0
    exp.max_value = 1.0
    exp.description = "exposure"
    cl = tree.interface.new_socket("Clamp", in_out="INPUT", socket_type="NodeSocketBool", parent=pa)
    cl.default_value = False
    cl.description = "clamp on"
    tint = tree.interface.new_socket("Tint", in_out="INPUT", socket_type="NodeSocketColor", parent=pb)
    tint.default_value = (0.25, 0.5, 0.75, 1.0)
    tint.description = "tint"
    gam = tree.interface.new_socket("Gamma", in_out="INPUT", socket_type="NodeSocketFloat", parent=pb)
    gam.default_value = -2.5
    gam.min_value = -10.0
    gam.max_value = 10.0
    gam.description = "gamma"
    ite = tree.interface.new_socket("Iters", in_out="INPUT", socket_type="NodeSocketInt", parent=pb)
    ite.default_value = 3
    ite.min_value = 0
    ite.max_value = 16
    ite.description = "iterations"
    img_in = tree.interface.new_socket("Image", in_out="INPUT", socket_type="NodeSocketColor")
    img_in.default_value = (0.1, 0.2, 0.3, 1.0)
    img_in.description = "input image"
    o1 = tree.interface.new_socket("Image Out", in_out="OUTPUT", socket_type="NodeSocketColor")
    o1.description = "out image"
    o2 = tree.interface.new_socket("Clamp Out", in_out="OUTPUT", socket_type="NodeSocketBool")
    o2.description = "out clamp"
    # real node graph + frame
    tree.nodes.new("NodeFrame")
    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    mix = tree.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.inputs["Factor"].default_value = 0.5
    tree.links.new(gin.outputs["Image"], mix.inputs["A"])
    tree.links.new(mix.outputs["Result"], gout.inputs[0])
    # top-order target: Panel A, Panel B, Image, Image Out, Clamp Out
    tree.interface.move(img_in, 2)
    tree.asset_mark()
    tree.use_fake_user = True
    return tree


def role_writer():
    tree = build_rt_group()
    out = OUT_DIR / "if_writer.blend"
    bpy.data.libraries.write(str(out), datablocks={tree}, compress=True, path_remap="NONE")
    print("wrote", out, flush=True)
    assert_tree_structure(tree)
    print("wrote blend:", out)
    if FAILURES:
        sys.exit(1)


def role_binary():
    tree = bpy.data.node_groups.get(GROUP_NAME)
    check("group opened from blend", tree is not None)
    if tree is None:
        sys.exit(1)
    assert_tree_structure(tree)
    # place instance in a real compositor tree
    outer = bpy.data.node_groups.new("OuterBin", "CompositorNodeTree")
    outer.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    bpy.context.scene.compositing_node_group = outer
    g = outer.nodes.new("CompositorNodeGroup")
    g.node_tree = tree
    inst = {
        "inputs": [(s.name, s.bl_idname) for s in g.inputs],
        "has_min_max": [hasattr(s, "min_value") for s in g.inputs],
        "outputs": [(s.name, s.bl_idname) for s in g.outputs],
    }
    try:
        fsock = g.inputs["Exp"]
        attr_used = None
        for attr in ("value", "default_value"):
            try:
                setattr(fsock, attr, 2.0)
                attr_used = attr
                break
            except Exception:
                continue
        if attr_used:
            inst["exp_set_to_2_readback"] = {"attr": attr_used, "value": float(getattr(fsock, attr_used))}
        else:
            inst["exp_set_to_2_readback"] = "ERR no writable value attr"
    except Exception as e:
        inst["exp_set_to_2_readback"] = "ERR " + str(e)[:100]
    print("INSTANCE_FACTS:", json.dumps(inst, default=str), flush=True)
    out = OUT_DIR / "bin_tree.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(out))
    print("saved", out, flush=True)
    if FAILURES:
        print("FAILURES:", FAILURES)
        sys.exit(1)
    print("PROBE3_BINARY_DONE")


def role_verify():
    bpy.ops.wm.open_mainfile(filepath=str(OUT_DIR / "bin_tree.blend"))
    tree = bpy.data.node_groups.get(GROUP_NAME)
    check("group present after wheel reopen of binary tree", tree is not None)
    if tree is None:
        sys.exit(1)
    assert_tree_structure(tree)
    outer = bpy.data.node_groups.get("OuterBin")
    inst = {"found": outer is not None}
    if outer:
        g = [n for n in outer.nodes if n.bl_idname == "CompositorNodeGroup"]
        inst["group_nodes"] = len(g)
        if g:
            inst["input_names"] = [s.name for s in g[0].inputs]
            inst["output_names"] = [s.name for s in g[0].outputs]
            for attr in ("value", "default_value"):
                if hasattr(g[0].inputs.get("Exp"), attr):
                    inst["exp_value"] = {"attr": attr, "value": float(getattr(g[0].inputs["Exp"], attr))}
                    break
            else:
                inst["exp_value"] = "no value attr"
    print("INSTANCE_REOPEN:", json.dumps(inst, default=str), flush=True)
    if FAILURES:
        print("FAILURES:", FAILURES)
        sys.exit(1)
    print("PROBE3_VERIFY_DONE")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["introspect", "writer", "binary", "verify"], default="introspect")
    argv = sys.argv[1:]
    if "--" in sys.argv:  # binary mode: blender flags precede '--'
        argv = sys.argv[sys.argv.index("--") + 1:]
    args, unknown = ap.parse_known_args(argv)
    fn = {"introspect": role_introspect, "writer": role_writer,
          "binary": role_binary, "verify": role_verify}[args.role]
    try:
        fn()
        sys.stdout.flush()
        sys.stderr.flush()
        import os
        os._exit(0)  # bypass bpy module teardown noise that overwrites exit codes
    except SystemExit:
        sys.stdout.flush()
        sys.stderr.flush()
        import os
        os._exit(1)


if __name__ == "__main__":
    main()