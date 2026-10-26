"""PROBE 1 - XYZ equivalence: Blender's CompositorNodeConvertColorSpace vs the
OpenDRT v1.1.0 spec input matrices, measured on a real headless compositor
render (Cycles CPU, 1 sample, 4x3px float32 EXR round-trip).

Runs under BOTH runtimes (engine: Cycles CPU, per steering):
  pixi run python tools/probes/probe_xyz_equiv.py
  env BLENDER_BIN=/home/zicklag/bin/blender $BLENDER_BIN -b -P tools/probes/probe_xyz_equiv.py

What it answers:
  1. Which from_color_space/to_color_space strings the node accepts
     (by assigning an invalid string and parsing the RNA enum error list).
  2. Are the rendered XYZ values numerically identical to float32 application
     of spec/opendrt-v1.1.0.json input matrices (matrix_*_to_xyz)?
     Controls: (a) Raw-image -> Composite passthrough (catches any non-identity
     pipeline leg), (b) identity conversion (from == to).
  3. Verdict per colorspace: identical / diverges, with max-abs + max-rel.

JSON results: /tmp/opendrt_probes/probe1_<runtime>.json
"""
import ast
import json
import sys
from pathlib import Path

import numpy as np
import bpy

PROBES_DIR = Path(__file__).resolve().parent
ROOT = PROBES_DIR.parent.parent
SPEC = ROOT / "spec" / "opendrt-v1.1.0.json"
OUT_DIR = Path("/tmp/opendrt_probes")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 12 test values (linear RGB), one per pixel of the 4x3 image.
TEST_VALUES = [
    (1.0, 1.0, 1.0),      # white
    (0.18, 0.18, 0.18),   # 0.18 gray
    (0.5, 0.5, 0.5),      # mid gray
    (2.0, 2.0, 2.0),      # HDR > 1
    (10.0, 10.0, 10.0),   # deep HDR > 1
    (0.0, 0.0, 0.0),      # black
    (1.0, 0.0, 0.0),      # R primary
    (0.0, 1.0, 0.0),      # G primary
    (0.0, 0.0, 1.0),      # B primary
    (1.0, 1.0, 0.0),      # yellow
    (0.0, 1.0, 1.0),      # cyan
    (0.35, 0.08, 0.22),   # saturated mixed chroma
]
W, H = 4, 3
CANDIDATES = [
    "Linear Rec.709", "Linear Rec.2020", "ACEScg", "Linear ACEScg",
    "ACES2065-1", "Linear ACES2065-1", "Linear CIE-XYZ D65", "CIE-XYZ D65",
    "Linear CIE-XYZ", "XYZ", "CIE XYZ",
]
MAP_CS_TO_MATRIX = {
    "Linear Rec.709": "matrix_rec709_to_xyz",
    "Linear Rec.2020": "matrix_rec2020_to_xyz",
    "ACEScg": "matrix_ap1_to_xyz",
    "ACES2065-1": "matrix_ap0_to_xyz",
}


def parse_enum_error(exc) -> list[str]:
    """Extract the accepted-enum list from an RNA assignment error message."""
    msg = str(exc)
    tail = msg.split("not found in (", 1)[1]
    for strip in range(len(tail)):
        try:
            return list(ast.literal_eval("(" + tail[:-strip] + ")"))
        except (SyntaxError, ValueError):
            continue
    raise ValueError("could not parse enum list from: " + msg[:200])


def make_test_image(name):
    img = bpy.data.images.new(name, W, H, float_buffer=True)
    img.colorspace_settings.name = "Non-Color"  # identity: raw values reach the compositor
    rgba = []
    for (r, g, b) in TEST_VALUES:
        rgba += [r, g, b, 1.0]
    img.pixels.foreach_set(rgba)
    return img


def build_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 1
    sc.cycles.use_denoising = False
    sc.cycles.use_adaptive_sampling = False
    sc.render.resolution_x = W
    sc.render.resolution_y = H
    sc.render.resolution_percentage = 100
    # Force CPU full-float compositor (property name discovered, see PROBE_REPORT)
    sc.render.compositor_device = "CPU"
    sc.render.compositor_precision = "FULL"
    sc.render.use_compositing = True
    sc.view_settings.view_transform = "Standard"  # identity view transform
    sc.view_settings.look = "None"
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
    return sc


def build_tree(sc, img, from_cs, to_cs, with_convert=True):
    ng = bpy.data.node_groups.new("ProbeTree", "CompositorNodeTree")
    tree = ng
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    tree.nodes.clear()
    n_img = tree.nodes.new("CompositorNodeImage")
    n_img.image = img
    prev = n_img.outputs[0]
    if with_convert:
        conv = tree.nodes.new("CompositorNodeConvertColorSpace")
        conv.from_color_space = from_cs
        conv.to_color_space = to_cs
        tree.links.new(prev, conv.inputs[0])
        prev = conv.outputs[0]
    comp = tree.nodes.new("NodeGroupOutput")
    tree.links.new(prev, comp.inputs[0])
    sc.compositing_node_group = ng
    return tree


def render_and_read(sc, exr_path):
    sc.render.filepath = str(exr_path)
    bpy.ops.render.render(write_still=True)
    out_img = bpy.data.images.load(str(exr_path))
    out_img.colorspace_settings.name = "Non-Color"  # raw float readback
    vals = list(out_img.pixels[:])
    bpy.data.images.remove(out_img)
    return vals


def compare(rendered, ref32, ref64):
    xyz_rendered = np.array(rendered, dtype=np.float64).reshape(-1, 4)[:H * W, :3]
    diffs32 = np.abs(xyz_rendered - ref32)
    diffs64 = np.abs(xyz_rendered - ref64)
    denom = np.maximum(np.abs(ref32), 1e-9)
    rel = diffs32 / denom
    rel_sig_mask = np.abs(ref32) >= 1e-3
    i = np.unravel_index(np.argmax(diffs32), diffs32.shape)
    return {
        "max_abs_vs_f32": float(diffs32.max()),
        "max_rel_vs_f32": float(rel.max()),
        "max_rel_sig_vs_f32": float(rel[rel_sig_mask].max()) if rel_sig_mask.any() else 0.0,
        "max_abs_vs_f64": float(diffs64.max()),
        "bitwise_equal_f32": bool(np.array_equal(xyz_rendered, ref32)),
        "argmax_pixel": int(i[0] // 3),
        "argmax_channel": int(i[1]),
        "argmax_values": {
            "rendered": xyz_rendered[i[0]].tolist(),
            "ref_f32": ref32[i[0]].tolist(),
            "ref_f64": ref64[i[0]].tolist(),
        },
    }


def main():
    results = {"runtime": None, "accepted_enums": {}, "from_tests": [], "identity_test": {}, "passthrough_control": {}, "verdicts": {}}
    results["runtime"] = f"bpy-module wheel {bpy.app.version_string}" if "--wheel" in sys.argv else "real binary"
    results["bpy_version"] = bpy.app.version_string

    spec = json.loads(SPEC.read_text())
    consts = spec["constants"]
    nodes_enum = {}
    bpy.ops.wm.read_factory_settings(use_empty=True)
    probe_ng = bpy.data.node_groups.new("ProbeEnum", "CompositorNodeTree")
    probe_node = probe_ng.nodes.new("CompositorNodeConvertColorSpace")
    for side in ("from_color_space", "to_color_space"):
        try:
            setattr(probe_node, side, "NOT_A_REAL_COLORSPACE")
        except Exception as e:
            opts = parse_enum_error(e)
            nodes_enum[side] = opts
            nodes_enum[side + "_err_len"] = len(str(e))
        accepted = [c for c in CANDIDATES if _accepts(probe_node, side, c)]
        nodes_enum[side + "_accepted_candidates"] = accepted
    full_from = [it.identifier for it in probe_node.bl_rna.properties["from_color_space"].enum_items]
    full_to = [it.identifier for it in probe_node.bl_rna.properties["to_color_space"].enum_items]
    results["accepted_enums"] = {
        "options_count": len(full_from),
        "options": full_from,
        "to_options": full_to,
        "from_accepted_try": nodes_enum.get("from_color_space_accepted_candidates", []),
        "to_accepted_try": nodes_enum.get("to_color_space_accepted_candidates", []),
        "error_msg_len": nodes_enum.get("from_color_space_err_len"),
    }
    bpy.data.node_groups.remove(probe_ng)

    to_acc = nodes_enum.get("to_color_space_accepted_candidates", [])
    to_cs = "Linear CIE-XYZ D65" if "Linear CIE-XYZ D65" in to_acc else "XYZ"
    results["to_name_used"] = to_cs

    rgb32 = np.array(TEST_VALUES, dtype=np.float32)[:H * W, :3]  # stored float32 inputs

    build_scene()
    img = make_test_image("ProbeImage")
    # Control 1: image -> composite with NO convert node (pipeline passthrough)
    build_tree(bpy.context.scene, img, None, None, with_convert=False)
    ctrl = render_and_read(bpy.context.scene, OUT_DIR / "probe1_control.exr")
    raw = np.array(ctrl, dtype=np.float64).reshape(-1, 4)[:H * W, :3]
    ctrl32 = raw - rgb32.astype(np.float64)
    results["passthrough_control"] = {
        "max_abs": float(np.abs(ctrl32).max()),
        "bitwise_equal": bool(np.array_equal(raw, rgb32.astype(np.float64))),
    }
    if results["passthrough_control"]["max_abs"] > 1e-6:
        print(f"PROBE1 WARN: passthrough control shows pipeline delta {results['passthrough_control']['max_abs']:.3e}; "
              "equiv numbers below include that baseline.", flush=True)

    # Control 2: identity conversion (from == to) on one space
    build_tree(bpy.context.scene, img, "Linear Rec.709", "Linear Rec.709", with_convert=True)
    idt = render_and_read(bpy.context.scene, OUT_DIR / "probe1_identity.exr")
    raw = np.array(idt, dtype=np.float64).reshape(-1, 4)[:H * W, :3]
    results["identity_test"] = {
        "space": "Linear Rec.709",
        "max_abs": float(np.abs(raw - rgb32.astype(np.float64)).max()),
    }

    # Main tests per colorspace
    for from_cs, const_name in MAP_CS_TO_MATRIX.items():
        rows = consts[const_name]["rows"]
        mat32 = np.array(rows, dtype=np.float32)
        mat64 = np.array(rows, dtype=np.float64)
        ref32 = rgb32 @ mat32.T
        ref64 = rgb32.astype(np.float64) @ mat64.T
        build_tree(bpy.context.scene, img, from_cs, to_cs, with_convert=True)
        rendered = render_and_read(bpy.context.scene, OUT_DIR / f"probe1_{from_cs}.exr")
        entry = compare(rendered, ref32, ref64)
        entry["matrix_const"] = const_name
        xyz_arr = np.array(rendered, dtype=np.float64).reshape(-1, 4)[:H * W, :3]
        M_bl = np.stack([xyz_arr[6], xyz_arr[7], xyz_arr[8]], axis=1)  # linear map from basis pixels
        entry["blender_matrix"] = M_bl.tolist()
        entry["spec_matrix"] = mat64.tolist()
        entry["matrix_diff_max"] = float(np.max(np.abs(M_bl - mat64)))
        ok = entry["max_abs_vs_f32"] <= 5e-6
        results["verdicts"][from_cs] = "identical" if ok else "DIVERGES"
        results["from_tests"].append({"from": from_cs, **entry})

    out_path = OUT_DIR / f"probe1_{'wheel' if '--wheel' in sys.argv else 'binary'}.json"
    out_path.write_text(json.dumps(results, indent=2))

    print("== PROBE1 XYZ EQUIVALENCE ==")
    print("runtime:", results["runtime"])
    print("accepted name check (try-assign):")
    print("from/to enum option count:", results["accepted_enums"]["options_count"])
    for cs, v in results["verdicts"].items():
        t = next(x for x in results["from_tests"] if x["from"] == cs)
        print(f"VERDICT {cs:16s} matrix={t['matrix_const']:24s} max_abs={t['max_abs_vs_f32']:.3e} "
              f"max_rel={t['max_rel_vs_f32']:.3e} rel_sig={t['max_rel_sig_vs_f32']:.3e} "
              f"bitwise={t['bitwise_equal_f32']} matrix_diff_max={t['matrix_diff_max']:.3e} -> {v}")
    print("passthrough control: max_abs=%.3e bitwise=%s" % (
        results["passthrough_control"]["max_abs"], results["passthrough_control"]["bitwise_equal"]))
    print("identity control:    max_abs=%.3e" % results["identity_test"]["max_abs"])

    print("JSON:", out_path)
    print("PROBE1_DONE")


def _accepts(node, side, value):
    try:
        setattr(node, side, value)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    main()