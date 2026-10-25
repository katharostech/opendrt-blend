import bpy
from pathlib import Path

print("bpy module version:", bpy.app.version_string)

# Fresh data: in module mode bpy starts with an empty mainfile - no factory
# scene to clear. Create a compositor node group.
ng = bpy.data.node_groups.new("OpenDRT v1.1.0 Test", "CompositorNodeTree")

# Interface: one panel + input sockets + output socket (the 4.x/5.x API look).
panel = ng.interface.new_panel("Tonescale")
s_in = ng.interface.new_socket(
    "Image", in_out="INPUT", socket_type="NodeSocketColor"
)
s_out = ng.interface.new_socket(
    "Image", in_out="OUTPUT", socket_type="NodeSocketColor"
)
s_ex = ng.interface.new_socket(
    "Exposure", in_out="INPUT", socket_type="NodeSocketFloat", parent=panel
)
s_ex.default_value = 0.5

gin = ng.nodes.new("NodeGroupInput")
gout = ng.nodes.new("NodeGroupOutput")

# One real node + link to prove the graph path.
mix = ng.nodes.new("ShaderNodeMix")
mix.data_type = "RGBA"
ng.links.new(gin.outputs["Image"], mix.inputs[6])
ng.links.new(mix.outputs[2], gout.inputs["Image"])

# Asset + fake user (unused data blocks survive the write).
ng.asset_mark()
ng.use_fake_user = True
ng.asset_data.description = "OpenDRT v1.1.0 display transform (smoke test)"

out = Path("/tmp/opendrt-smoke.blend").resolve()
bpy.data.libraries.write(str(out), datablocks={ng}, compress=True, path_remap="NONE")
print("wrote", out.stat().st_size, "bytes")

bpy.data.orphans_purge(do_local_ids=False, do_recursive=True)
print("SMOKE_OK")