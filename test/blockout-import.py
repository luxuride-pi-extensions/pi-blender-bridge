#!/usr/bin/env python3
"""
Exercise the generative blockout import path against real Blender.

**What this probe proves.** A provider's output is dense, open and unwrapped, and
the one failure worth preventing is a worker shipping it. So the probe builds a
GLB that is exactly that: a subdivided shell with one face removed, no UVs, 10:1
tall. It writes the file into a temp dir rather than committing one, because a
fixture binary in git is a fixture nobody can read the geometry of.

Then it imports through `handle_blockout_import` and asserts the four things the
gate depends on:

  * the geometry lands in the `Blockout` collection and nowhere else;
  * every imported object carries `pi_blender_blockout`;
  * the bounds are normalised to the requested box, upright, centred and resting
    on Z=0, with the transform baked rather than left as object scale;
  * `validate()` refuses the result for `character` -- which is the point, because
    a gate nobody measured is a comment.

Run: blender --background --factory-startup --python test/blockout-import.py
Exit code 0 means every assertion held.
"""

import os
import sys
import tempfile

# pi-lens-ignore: ast-grep:unresolved-import-python (bpy is provided by Blender)
import bmesh  # pyright: ignore[reportMissingImports]
import bpy  # pyright: ignore[reportMissingImports]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blender.import_asset import DENSE_TRIANGLES, handle_blockout_import  # noqa: E402
from blender.validation import validate  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
    print(f"  {'ok  ' if condition else 'FAIL'}: {message}")
    sys.stdout.flush()


def dense_shell(subdivisions=24, height=5.0):
    """A subdivided cube with one face missing: dense, open and unwrapped.

    Open because a boundary edge is the cheapest honest way to be non-manifold,
    and because that is what a generator's answer usually is. Tall because the
    normaliser has to stand a model up, and a 10:1 shell makes "which axis is
    longest" a fact about the fixture rather than a coincidence.
    """
    mesh = bpy.data.meshes.new("DenseBlockout")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=subdivisions, use_grid_fill=True)
    for vertex in bm.verts:
        vertex.co.z *= height
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[bm.faces[0]], context="FACES")
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("DenseBlockout", mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def main():
    workdir = tempfile.mkdtemp(prefix="pi-blockout-")
    glb_path = os.path.join(workdir, "generated.glb")
    blend_path = os.path.join(workdir, "blockout.blend")

    bpy.ops.wm.read_factory_settings(use_empty=True)
    source = dense_shell()
    source_triangles = sum(len(polygon.vertices) - 2 for polygon in source.data.polygons)
    bpy.ops.export_scene.gltf(filepath=glb_path, export_format="GLB")
    check(os.path.exists(glb_path), "the fixture GLB was written into the temp dir")
    check(source_triangles > 1000, f"the fixture is dense ({source_triangles} triangles)")
    check(len(source.data.uv_layers) == 0, "the fixture carries no UV map")

    # The import path must survive a scene that has nothing in common with the
    # module it will be rebuilt into: no collection, no camera, no ownership.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    result = handle_blockout_import(
        {
            "sourcePath": glb_path,
            "blendPath": blend_path,
            "provider": "local",
            "license": "CC0-1.0",
            "prompt": "a probe fixture, not a generated asset",
            "seed": 7,
            "id": "blockout-probe",
            "targetBounds": {"x": 1.0, "y": 1.0, "z": 2.0},
        }
    )

    check(result.get("success") is True, f"the GLB imported: {result.get('error') or 'ok'}")
    if not result.get("success"):
        raise SystemExit(1)

    objects = [bpy.data.objects[name] for name in result["objects"]]
    meshes = [obj for obj in objects if obj.type == "MESH"]
    check(len(meshes) == 1, f"one mesh came back ({len(meshes)} found)")
    imported = meshes[0]

    collection = bpy.data.collections.get("Blockout")
    check(collection is not None, "a dedicated Blockout collection exists")
    check(
        collection is not None
        and {obj.name for obj in collection.objects} == {obj.name for obj in objects},
        "every imported object is in the Blockout collection",
    )
    check(
        [c.name for c in imported.users_collection] == ["Blockout"],
        f"the mesh is in no other collection ({[c.name for c in imported.users_collection]})",
    )
    check(bool(imported.get("pi_blender_blockout")), "the mesh is tagged as a blockout")

    bounds = result["provenance"]["bounds"]
    width = bounds["dimensions"][0]
    depth = bounds["dimensions"][1]
    tall = bounds["dimensions"][2]
    check(
        abs(max(bounds["dimensions"]) - 2.0) < 0.02,
        f"the longest axis fills the box (max {max(bounds['dimensions']):.3f} against 2.0)",
    )
    check(
        tall > width and tall > depth,
        f"the longest axis is upright (dimensions {bounds['dimensions']})",
    )
    check(abs(bounds["min"][2]) < 0.01, f"the base sits on Z=0 (min z {bounds['min'][2]:.4f})")
    check(
        abs(bounds["min"][0] + bounds["max"][0]) < 0.01 and abs(bounds["min"][1] + bounds["max"][1]) < 0.01,
        "the model is centred on X and Y",
    )
    check(
        [round(float(s), 4) for s in imported.scale] == [1.0, 1.0, 1.0],
        f"the transform was baked, not left as object scale ({[round(float(s), 4) for s in imported.scale]})",
    )

    provenance = result["provenance"]
    for key in ("id", "path", "provider", "license", "prompt", "seed", "bounds", "triangles"):
        check(key in provenance, f"provenance carries {key}")
    check(provenance["blockout"] is True, "provenance says blockout")
    check(provenance["id"] == "blockout-probe", "the requested id is kept")
    check(provenance["license"] == "CC0-1.0", "the licence travels with the blockout")
    check(
        provenance["triangles"] == source_triangles,
        f"the triangle count is measured ({provenance['triangles']} against {source_triangles})",
    )
    check(provenance["triangles"] <= DENSE_TRIANGLES, "the fixture is small enough not to trip the density warning")

    check(
        any("scaled by" in note for note in result["notes"]),
        f"the scale change is reported ({result['notes']!r})",
    )
    warnings = " ".join(result["warnings"])
    check("UV" in warnings, f"the missing UV map is warned about ({warnings!r})")
    check(os.path.exists(blend_path), "the blockout blend was saved where it was asked for")
    check(
        os.path.abspath(bpy.data.filepath) == os.path.abspath(blend_path),
        "the saved blend is the blockout blend, not the module's",
    )

    verdict = validate(bpy, [imported.name], profile="character")
    check(
        verdict["checks"]["blockout"]["status"] == "block",
        f"preflight refuses the blockout for character (status {verdict['checks']['blockout']['status']})",
    )
    check("blockout" in verdict["blockers"], "the blockout verdict is a blocker, not a warning")
    prop = validate(bpy, [imported.name], profile="prop")
    check(
        prop["checks"]["blockout"]["status"] == "warn" and "blockout" not in prop["blockers"],
        "a prop is warned about the blockout rather than blocked by it",
    )

    # The tag is the only signal the gate has, so a rebuild that clears it has to
    # pass: otherwise the gate refuses real work forever.
    del imported["pi_blender_blockout"]
    rebuilt = validate(bpy, [imported.name], profile="character")
    check(rebuilt["checks"]["blockout"]["status"] == "ok", "an untagged mesh passes the gate")

    for name in failures:
        print(f"FAIL: {name}")
    if failures:
        raise SystemExit(1)
    print("PASS: blockout import")


if __name__ == "__main__":
    main()
