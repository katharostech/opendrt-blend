"""PROBE (2026-10-27) - 'Enable Purity Compress High' removed from node UI?

The param is defunct in DCTL v1.1.0 (preset chain writes pt_enable = 1 but no
stage reads it; purity compression is unconditional). It was removed from
SOCKET_TABLE; this asserts the shipped .blend no longer exposes it anywhere:
no interface input, no group-input output, in any written tree.
"""
import sys
import bpy

BLEND = "build/local-pack/OpenDRT_v1.1.0/OpenDRT_v1.1.0.blend"
TARGET = "Enable Purity Compress High"
CONTROL = "Enable Mid Purity"  # live checkbox, must still exist + be consumed

with bpy.data.libraries.load(BLEND, link=False) as (src, dst):
    dst.node_groups = [n for n in src.node_groups
                       if n.startswith("OpenDRT")]

fail = []
ifaces = gin_outs = 0
for tree in bpy.data.node_groups:
    for it in tree.interface.items_tree:
        if it.item_type != "SOCKET" or it.in_out != "INPUT":
            continue
        if it.name == TARGET:
            fail.append(f"{tree.name}: interface input still exists")
        if it.name == CONTROL:
            ifaces += 1
    gin = next((n for n in tree.nodes if n.bl_idname == "NodeGroupInput"),
               None)
    if gin is None:
        continue
    if TARGET in gin.outputs:
        fail.append(f"{tree.name}: group-input output still exists "
                    f"({len(gin.outputs[TARGET].links)} links)")
    if CONTROL in gin.outputs and len(gin.outputs[CONTROL].links) == 0:
        fail.append(f"{tree.name}: control '{CONTROL}' unexpectedly dead")

print(f"groups={len(bpy.data.node_groups)} "
      f"control-sockets(presets)={ifaces}")
print("RESULT:", "REMOVED EVERYWHERE" if not fail else "STILL PRESENT")
for f in fail:
    print("  " + f, flush=True)
sys.exit(1 if fail else 0)