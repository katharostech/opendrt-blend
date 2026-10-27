"""PROBE (2026-10-27) - user-facing OpenDRT node width = 240?

Checks on a shipped .blend:
1. the 8 user-facing group trees carry default_group_node_width = 240
   (the width of instances created via the UI - Shift+A / asset drag;
   verified by user in Blender UI 2026-10-27);
2. the wrapper trees' inner "base" instance stores width 240;
3. internal fn groups keep the 140 default (width change is scoped).
bpy nodes.new() instantiation is informational only (the RNA constructor
bypasses the tree default).
Run: pixi run python tools/probes/probe_width.py
"""
import bpy
import sys

BLEND = "local-pack/OpenDRT_v1.1.0/OpenDRT_v1.1.0.blend"

LOOKS = ["Standard", "Arriba", "Sylvan", "Colorful", "Aery", "Dystopic",
         "Umbra"]
ASSETS = ["OpenDRT 1.1 Base"] + [f"OpenDRT 1.1 {n}" for n in LOOKS]
WIDTH = 240.0

bpy.ops.wm.read_homefile(use_empty=True)

# load only node groups from the pack blend
with bpy.data.libraries.load(BLEND, link=False) as (src, dst):
    dst.node_groups = [n for n in src.node_groups if n.startswith("OpenDRT")]

fail = []


def tree_of(name):
    ng = bpy.data.node_groups.get(name)
    if ng is None:
        fail.append(f"missing tree {name!r}")
    return ng


# 1: tree default (this is what Shift+A / asset-drag instances honor -
# verified 2026-10-27 in the Blender UI; bpy nodes.new() bypasses the
# tree default and returns 140, that path is NOT a failure)
scratch = bpy.data.node_groups.new("scratch", "CompositorNodeTree")
for name in ASSETS:
    ng = tree_of(name)
    if ng is None:
        continue
    got = getattr(ng, "default_group_node_width", None)
    if got != WIDTH:
        fail.append(f"{name}: default_group_node_width={got}")
    inst = scratch.nodes.new("CompositorNodeGroup")
    inst.node_tree = ng
    if got == WIDTH and abs(inst.width - WIDTH) > 1e-3:
        print(f"info: {name}: bpy nodes.new() width={inst.width} "
              "(RNA constructor bypasses default_group_node_width)")

# 2: wrapper inner "base" instance
for name in (f"OpenDRT 1.1 {n}" for n in LOOKS):
    ng = tree_of(name)
    if ng is None:
        continue
    inner = next((n for n in ng.nodes if n.type == "GROUP" and
                  n.name == "base"), None)
    if inner is None:
        fail.append(f"{name}: no inner base instance")
    elif abs(inner.width - WIDTH) > 1e-3:
        fail.append(f"{name}: inner base width={inner.width}")

# 4: internal fn groups must keep the stock default
fn = tree_of("OpenDRT fn softplus")
if fn is not None and fn.default_group_node_width != 140:
    fail.append(f"fn spillover: softplus width={fn.default_group_node_width}")

print(f"trees={len(bpy.data.node_groups)} checked={len(ASSETS) + len(LOOKS) + 1}")
print("RESULT:", "ALL 240 OK" if not fail else "FAILURES")
for f in fail:
    print(" " + f, flush=True)
sys.exit(1 if fail else 0)