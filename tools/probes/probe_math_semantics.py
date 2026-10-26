"""PROBE 4 - Builder facts: shader-node math semantics inside compositor trees.

Runs in a real compositor render (Cycles CPU; compositor device via --device,
CPU or GPU). Each math case renders as its own 1x1 EXR (R->RGB):

  - ShaderNodeMath available operations (RNA enum, measured)
  - ShaderNodeCompare does NOT exist in 5.2/5.3: compare = ShaderNodeMath ops
    COMPARE (== within epsilon, epsilon = input 3) / LESS_THAN / GREATER_THAN
  - ShaderNodeMix data_type availability (FLOAT / VECTOR / RGBA)
  - divide-by-zero, modulo sign, floored modulo, pow(neg, fraction) etc.

  pixi run python tools/probes/probe_math_semantics.py [--device CPU]
  env BLENDER_BIN=... $BLENDER_BIN -b --factory-startup -P tools/probes/probe_math_semantics.py -- --device GPU
JSON: /tmp/opendrt_probes/probe4_<runtime>_<device>.json
"""
import json
import sys
from pathlib import Path

import bpy

OUT_DIR = Path("/tmp/opendrt_probes")
OUT_DIR.mkdir(parents=True, exist_ok=True)

CASES = [
    ("passthrough_value_1.4142", "PASSTHROUGH", 1.414213562373095, 0.0),
    ("passthrough_direct_1.4142", "PASSTHROUGH_DIRECT", 1.414213562373095, 0.0),
    ("div_neg1_0", "DIVIDE", -1.0, 0.0),
    ("div_0_0", "DIVIDE", 0.0, 0.0),
    ("div_2_0", "DIVIDE", 2.0, 0.0),
    ("mod_7_-4", "MODULO", 7.0, -4.0),
    ("mod_-7_4", "MODULO", -7.0, 4.0),
    ("mod_7_4", "MODULO", 7.0, 4.0),
    ("fmod_7_-4", "FLOORED_MODULO", 7.0, -4.0),
    ("fmod_-7_4", "FLOORED_MODULO", -7.0, 4.0),
    ("pow_neg_0.5", "POWER", -2.0, 0.5),
    ("pow_neg_0.75", "POWER", -2.0, 0.75),
    ("pow_neg_0.25", "POWER", -2.0, 0.25),
    ("pow_neg_0", "POWER", -2.0, 0.0),
    ("pow_0_0", "POWER", 0.0, 0.0),
    ("pow_neg_2", "POWER", -2.0, 2.0),
    ("pow_2_3", "POWER", 2.0, 3.0),
    ("sqrt_2", "SQRT", 2.0, 0.0),
    ("inv_sqrt_2", "INVERSE_SQRT", 2.0, 0.0),
    ("abs_-1.5", "ABSOLUTE", -1.5, 0.0),
    ("trunc_2.9", "TRUNC", 2.9, 0.0),
    ("trunc_-2.9", "TRUNC", -2.9, 0.0),
    ("round_2.5", "ROUND", 2.5, 0.0),
    ("round_-2.5", "ROUND", -2.5, 0.0),
    ("floor_2.9", "FLOOR", 2.9, 0.0),
    ("floor_-2.9", "FLOOR", -2.9, 0.0),
    ("ceil_2.1", "CEIL", 2.1, 0.0),
    ("ceil_-2.1", "CEIL", -2.1, 0.0),
    ("fract_-1.25", "FRACT", -1.25, 0.0),
    ("fract_1.25", "FRACT", 1.25, 0.0),
    ("snap_2.34_0.5", "SNAP", 2.34, 0.5),
    ("snap_2.75_0.5", "SNAP", 2.75, 0.5),
    ("pingpong_5_1_3", "PINGPONG", 5.0, 1.0),   # (b = length 3? note semantics)
    ("wrap_5_1_3", "WRAP", 5.0, 1.0),           # b=1? no: wrap uses 3 inputs (a,min,max)
    ("cmp_eq_0.1_0.2_eps1e-6", "COMPARE", 0.1, 0.2),
    ("cmp_eq_0.1_0.1002_eps1e-6", "COMPARE", 0.1, 0.1002),
    ("cmp_eq_0.1_0.1002_eps1e-3", "COMPARE", 0.1, 0.1002),
    ("cmp_lt_1_2", "LESS_THAN", 1.0, 2.0),
    ("cmp_gt_1_2", "GREATER_THAN", 1.0, 2.0),
]


def build_case(device, keep_case):
    """Fresh scene + compositor tree evaluating exactly one math case -> 1x1 EXR."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 1
    sc.cycles.use_denoising = False
    sc.cycles.use_adaptive_sampling = False
    sc.render.resolution_x = 1
    sc.render.resolution_y = 1
    sc.render.resolution_percentage = 100
    sc.render.compositor_device = device
    sc.render.compositor_precision = "FULL"
    sc.render.use_compositing = True
    sc.view_settings.view_transform = "Standard"
    ims = sc.render.image_settings
    ims.file_format = "OPEN_EXR"
    ims.color_depth = "32"
    ims.exr_codec = "NONE"
    world = bpy.data.worlds.new("W")
    world.color = (0.0, 0.0, 0.0)
    sc.world = world
    cam_data = bpy.data.cameras.new("C")
    cam = bpy.data.objects.new("C", cam_data)
    sc.collection.objects.link(cam)
    sc.camera = cam
    sc.frame_start = sc.frame_end = 1
    tree = bpy.data.node_groups.new("MathProbe", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    sc.compositing_node_group = tree
    label, op, a, b = CASES[keep_case]
    va = tree.nodes.new("ShaderNodeValue")
    va.outputs[0].default_value = a
    vb = tree.nodes.new("ShaderNodeValue")
    vb.outputs[0].default_value = b
    if op == "PASSTHROUGH_DIRECT":
        gout = tree.nodes.new("NodeGroupOutput")
        tree.links.new(va.outputs[0], gout.inputs[0])
        return tree, va, label, op, a, b
    if op == "PASSTHROUGH":
        src = va.outputs[0]
    else:
        n = tree.nodes.new("ShaderNodeMath")
        n.operation = op
        if op == "COMPARE":
            n.inputs[2].default_value = 1e-3 if "eps1e-3" in label else 1e-6
        tree.links.new(va.outputs[0], n.inputs[0])
        tree.links.new(vb.outputs[0], n.inputs[1])
        src = n.outputs[0]
    combine = tree.nodes.new("CompositorNodeCombineColor")
    combine.mode = "RGB"
    for s in range(3):
        tree.links.new(src, combine.inputs[s])
    combine.inputs[3].default_value = 1.0
    gout = tree.nodes.new("NodeGroupOutput")
    tree.links.new(combine.outputs[0], gout.inputs[0])
    return tree, (n if op != "PASSTHROUGH" else va), label, op, a, b


def main():
    runtime = "wheel" if "--wheel" in sys.argv else "binary"
    dev = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
    math_ops = [it.identifier for it in bpy.types.ShaderNodeMath.bl_rna.properties["operation"].enum_items]
    results = {
        "runtime": runtime, "bpy": bpy.app.version_string, "device": dev,
        "math_ops": math_ops, "cases": [],
    }

    for i, (label, op, a, b) in enumerate(CASES):
        try:
            tree, n, label, op, a, b = build_case(dev, i)
            sc = bpy.context.scene
            p = OUT_DIR / f"probe4_{dev}_{i}.exr"
            sc.render.filepath = str(p)
            bpy.ops.render.render(write_still=True)
            img = bpy.data.images.load(str(p))
            img.colorspace_settings.name = "Non-Color"
            v = list(img.pixels[:])[2]
            bpy.data.images.remove(img)
            if isinstance(v, float):
                if v != v:
                    v = "NaN"
                v = round(v, 9)
            results["cases"].append({"label": label, "op": op, "a": a, "b": b, "value": v})
            print(f"  {label:26s} op={op:14s} -> {v}", flush=True)
            bpy.data.node_groups.remove(tree)
        except Exception as e:
            results["cases"].append({"label": label, "error": str(e)[:200]})
            print(f"  {label:26s} ERROR {str(e)[:160]}", flush=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    tree = bpy.data.node_groups.new("MixProbe", "CompositorNodeTree")
    mix_ok = {}
    for dt in ("FLOAT", "VECTOR", "RGBA"):
        try:
            m = tree.nodes.new("ShaderNodeMix")
            m.data_type = dt
            mix_ok[dt] = "ok"
        except Exception as e:
            mix_ok[dt] = str(e)[:80]
    results["mix_data_type_build"] = mix_ok

    path = OUT_DIR / f"probe4_{runtime}_{dev}.json"
    path.write_text(json.dumps(results, indent=2))
    print("JSON:", path)
    print("PROBE4_DONE")


if __name__ == "__main__":
    main()