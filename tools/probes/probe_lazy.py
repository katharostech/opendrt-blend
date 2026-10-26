"""PROBE 2 - Lazy output-branch benchmark for compositor node groups (Blender 5.x).

One heavy compositor node group ("Heavy": 9 output sockets, each heading a
serial 400-node Mix RGBA chain = 3600 eval nodes), placed in a real compositor
tree. Render timing (time.perf_counter around scene render, Cycles CPU
1-sample 64x64, CPU full-float compositor):

  (a) 0 outputs linked   - group node present, no links (input still linked)
  (b) 1 output linked
  (c) 9 outputs linked   (each branch consumed once)
  (d) 18 links           (same group, each output consumed twice)

3 timed renders per config + 1 untimed-in-median "first" render whose extra
time exposes compositor-compile overhead. Reports medians + verdict.

Runs under BOTH runtimes:
  pixi run python tools/probes/probe_lazy.py
  env BLENDER_BIN=/home/zicklag/bin/blender $BLENDER_BIN -b -P tools/probes/probe_lazy.py

JSON: /tmp/opendrt_probes/probe2_<runtime>.json
"""
import json
import time
import sys

import bpy

BRANCH_LEN = 400
N_OUT = 9
RES = 64
LINK_CONFIGS = [0, 1, 9, 18]
RUNS = 3
OUT_DIR = "/tmp/opendrt_probes"


def make_input_image():
    img = bpy.data.images.new("LazyIn", RES, RES, float_buffer=True)
    img.colorspace_settings.name = "Non-Color"
    px = []
    for y in range(RES):
        for x in range(RES):
            px += [0.5, 0.6, 0.7, 1.0]
    img.pixels.foreach_set(px)
    return img


def build_heavy_group(name="Heavy"):
    ng = bpy.data.node_groups.new(name, "CompositorNodeTree")
    ng.interface.new_socket("Image", in_out="INPUT", socket_type="NodeSocketColor")
    for j in range(N_OUT):
        ng.interface.new_socket(f"Out{j + 1}", in_out="OUTPUT", socket_type="NodeSocketColor")
    gin = ng.nodes.new("NodeGroupInput")
    gout = ng.nodes.new("NodeGroupOutput")
    src = gin.outputs["Image"]
    for j in range(N_OUT):
        prev = src
        for i in range(BRANCH_LEN):
            mix = ng.nodes.new("ShaderNodeMix")
            mix.data_type = "RGBA"
            mix.inputs["Factor"].default_value = 0.5
            ng.links.new(prev, mix.inputs["A"])
            prev = mix.outputs["Result"]
        ng.links.new(prev, gout.inputs[f"Out{j + 1}"])
    return ng


def build_outer_tree(heavy, img, links):
    tree = bpy.data.node_groups.new(f"LazyOuter{links}", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    bpy.context.scene.compositing_node_group = tree
    n_img = tree.nodes.new("CompositorNodeImage")
    n_img.image = img
    gnode = tree.nodes.new("CompositorNodeGroup")
    gnode.node_tree = heavy
    tree.links.new(n_img.outputs[0], gnode.inputs["Image"])
    gout = tree.nodes.new("NodeGroupOutput")
    # combiner chain: link k mixes group output round-robin into the running color
    prev = None
    for k in range(links):
        c = tree.nodes.new("ShaderNodeMix")
        c.data_type = "RGBA"
        c.inputs["Factor"].default_value = 0.5
        c.inputs["B"].default_value = (0.0, 0.0, 0.0, 1.0)
        if prev is None:
            tree.links.new(gnode.outputs[k % N_OUT], c.inputs["A"])
            prev = c
        else:
            tree.links.new(gnode.outputs[k % N_OUT], c.inputs["B"])
            last = tree.nodes.new("ShaderNodeMix")
            last.data_type = "RGBA"
            last.inputs["Factor"].default_value = 0.5
            last.inputs["B"].default_value = (0.0, 0.0, 0.0, 1.0)
            tree.links.new(prev.outputs["Result"], last.inputs["A"])
            tree.links.new(c.outputs["Result"], last.inputs["B"])
            prev = last
    # (a) fall-through: composite gets the image directly
    if links == 0:
        tree.links.new(n_img.outputs[0], gout.inputs[0])
    else:
        tree.links.new(prev.outputs["Result"], gout.inputs[0])
    return tree


def timed_render(sc):
    t0 = time.perf_counter()
    bpy.ops.render.render(write_still=False)
    return time.perf_counter() - t0


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 1
    sc.cycles.use_denoising = False
    sc.cycles.use_adaptive_sampling = False
    sc.render.resolution_x = RES
    sc.render.resolution_y = RES
    sc.render.resolution_percentage = 100
    sc.render.compositor_device = "CPU"
    sc.render.compositor_precision = "FULL"
    sc.render.use_compositing = True
    world = bpy.data.worlds.new("W")
    sc.world = world
    cam_data = bpy.data.cameras.new("C")
    cam = bpy.data.objects.new("C", cam_data)
    sc.collection.objects.link(cam)
    sc.camera = cam
    sc.frame_start = sc.frame_end = 1

    results = {
        "bpy_version": bpy.app.version_string,
        "engine": "CYCLES CPU 1smp 64x64",
        "compositor": {"device": sc.render.compositor_device, "precision": sc.render.compositor_precision,
                       "force_cpu_property": "scene.render.compositor_device"},
        "node_count_per_branch": BRANCH_LEN,
        "configs": {},
    }

    img = make_input_image()
    heavy = build_heavy_group("Heavy")
    print(f"heavy group built: nodes={len(heavy.nodes)} links={len(heavy.links)}", flush=True)

    for links in LINK_CONFIGS:
        tree = build_outer_tree(heavy, img, links)
        sc.compositing_node_group = tree
        first = timed_render(sc)
        runs = [timed_render(sc) for _ in range(RUNS)]
        med = sorted(runs)[len(runs) // 2]
        results["configs"][str(links)] = {
            "first_s": round(first, 4),
            "runs_s": [round(r, 4) for r in runs],
            "median_s": round(med, 4),
            "outer_nodes": len(tree.nodes),
            "outer_links": len(tree.links),
        }
        print(f"links={links:2d} outer_nodes={len(tree.nodes):5d} first={first:8.3f}s "
              f"runs={[round(r, 3) for r in runs]} median={med:.3f}s", flush=True)
        bpy.data.node_groups.remove(tree)

    a = results["configs"]["0"]["median_s"]
    b = results["configs"]["1"]["median_s"]
    c = results["configs"]["9"]["median_s"]
    d = results["configs"]["18"]["median_s"]
    branch_cost = (c - a) / N_OUT if N_OUT else None
    results["derived"] = {
        "base_median_s": a,
        "one_branch_median_s": b,
        "nine_branch_median_s": c,
        "eighteen_link_median_s": d,
        "inferred_branch_cost_s": round(branch_cost, 6),
        "unlinked_branches_cost_c_minus_a": round(c - a - 9 * branch_cost, 6),
    }
    print("== PROBE2 LAZY OUTPUT-BRANCH BENCHMARK ==")
    print("verdict inputs:", json.dumps(results["derived"]))
    print("LAZY VERDICT: unlinked cost = %.4fs (base), 1 branch = %+.4fs, 9 branches = %+.4fs, 18 links = %+.4fs"
          % (a, b - a, c - a, d - a))
    path = f"{OUT_DIR}/probe2_{'wheel' if '--wheel' in sys.argv else 'binary'}.json"
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    print("JSON:", path)
    print("PROBE2_DONE")


if __name__ == "__main__":
    main()