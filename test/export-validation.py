import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "blender"))

# pi-lens-ignore: ast-grep:unresolved-import-python
import bpy  # pyright: ignore[reportMissingImports]  # noqa: E402, I001
# Frozen snapshot of the module under test, imported by path.
# pi-lens-ignore: ast-grep:unresolved-import-python
import export_asset  # pyright: ignore[reportMissingImports]  # noqa: E402, I001

checks = {}


def cube(name, location=(0, 0, 0), scale=(1, 1, 1)):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    return obj


bpy.ops.wm.read_factory_settings(use_empty=True)
cube("Cube")
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "cube.glb")
    export_asset._run_export("glb", path, False, False)
    objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]
    materials = [slot.material for obj in objects for slot in obj.material_slots if slot.material]
    gltf = export_asset._validate_export("gltf", "glb", path, objects, materials)
    checks["gltf_pass"] = gltf is not None and gltf["ok"] and any(c["name"] == "nodes" for c in gltf["checks"])
    print_ok = export_asset._validate_export("print", "stl", path, objects, materials)
    checks["closed_cube_print_pass"] = print_ok["ok"]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_plane_add()
    plane = bpy.context.object
    open_ = export_asset._validate_export("print", "stl", "unused", [plane], [])
    checks["open_plane_print_fails"] = not open_["ok"] and any(
        c["name"] == "manifold" and c["status"] == "fail" for c in open_["checks"]
    )

    bpy.ops.wm.read_factory_settings(use_empty=True)
    cube("A")
    cube("B", location=(0.5, 0, 0))
    overlap = export_asset._validate_export("print", "stl", "unused", list(bpy.data.objects), [])
    checks["overlap_print_fails"] = not overlap["ok"] and any(
        c["name"] == "selfIntersection" and c["status"] == "fail" for c in overlap["checks"]
    )

    bpy.ops.wm.read_factory_settings(use_empty=True)
    first = cube("A")
    cube("B", location=(0.5, 0, 0))
    bpy.ops.object.select_all(action="SELECT")
    bpy.context.view_layer.objects.active = first
    bpy.ops.object.join()
    joined = export_asset._validate_export("print", "stl", "unused", [bpy.context.object], [])
    checks["joined_overlap_fails"] = not joined["ok"] and any(
        c["name"] == "selfIntersection" and c["status"] == "fail" for c in joined["checks"]
    )

    bpy.ops.wm.read_factory_settings(use_empty=True)
    cube("Scaled", scale=(1, 1, 2))
    scaled = export_asset._validate_export("print", "stl", "unused", [bpy.context.object], [])
    checks["unapplied_scale_warns"] = any(
        c["name"] == "scale" and c["status"] == "warn" for c in scaled["checks"]
    ) and scaled["ok"]

    engine = export_asset._validate_export("engine", "stl", "unused", [bpy.context.object], [])
    checks["engine_reports_checks"] = engine is not None and any(
        c["name"] == "textures" for c in engine["checks"]
    )
    checks["none_is_empty"] = export_asset._validate_export("none", "stl", "unused", [], []) is None

    bpy.ops.wm.read_factory_settings(use_empty=True)
    cube("Cube")
    with tempfile.TemporaryDirectory() as tmp:
        material = bpy.data.materials.new("Checked")
        material.use_nodes = True
        source = os.path.join(tmp, "source.png")
        image = bpy.data.images.new("Source", 8, 8)
        image.filepath_raw = source
        image.file_format = "PNG"
        image.save()
        texture = material.node_tree.nodes.new("ShaderNodeTexImage")
        texture.image = image
        principled = next(
            node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
        )
        material.node_tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])
        bpy.context.object.data.materials.append(material)
        sidecar = os.path.join(tmp, "sidecar.gltf")
        export_asset._run_export("gltf", sidecar, False, False)
        document = export_asset._read_gltf_json(sidecar)
        uris = [image.get("uri") for image in document.get("images", []) if image.get("uri")]
        checks["gltf_has_sidecar"] = bool(uris)
        if uris:
            os.remove(os.path.join(tmp, uris[0]))
            missing = export_asset._validate_export("gltf", "gltf", sidecar, [bpy.context.object], [])
            checks["missing_sidecar_fails"] = any(
                c["name"] == f"image:{uris[0]}" and c["status"] == "fail" for c in missing["checks"]
            )

        bpy.ops.wm.read_factory_settings(use_empty=True)
        cube("Cube")
        default_stl = export_asset.handle_export(
            {"format": "stl", "outputDir": tmp, "name": "default"}, lambda request: None
        )
        checks["stl_default_validates"] = (
            default_stl["validation"] is not None
            and default_stl["validation"]["profile"] == "print"
            and default_stl["validation"]["ok"]
        )

    # A glTF with zero textures is valid and flat, which is exactly the case a
    # `pass` on `0 texture(s)` used to hide: the file is correct, the asset
    # arrives untextured, and nothing in the report said so.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    cube("Bare")
    with tempfile.TemporaryDirectory() as tmp:
        bare_path = os.path.join(tmp, "bare.glb")
        export_asset._run_export("glb", bare_path, False, False)
        objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]
        bare = export_asset._validate_export(
            "gltf", "glb", bare_path, objects, [], {"images": 0, "textures": 0, "verified": None}
        )
        checks["untextured_glb_warns"] = bare["ok"] and any(
            c["name"] == "textures" and c["status"] == "warn" for c in bare["checks"]
        )
        reported = export_asset.handle_export(
            {"format": "glb", "outputDir": tmp, "name": "bare"}, lambda request: None
        )
        checks["untextured_glb_reports_a_warning"] = (
            reported["textures"] == 0
            and any("0 textures" in warning for warning in reported["warnings"])
        )

    # An image the exporter does not walk is an image the file cannot carry: the
    # glTF stays valid, `_verify_textures` rightly does not demand it, and the
    # material still claims it. Emission Strength is that case -- the exporter
    # writes no texture for it, so the numbers arrive at their default and the
    # asset is flat. The warning has to name the material, because "0 textures"
    # on its own reads as "no textures were needed".
    bpy.ops.wm.read_factory_settings(use_empty=True)
    cube("Lit")
    lit = bpy.data.materials.new("LitByImage")
    lit.use_nodes = True
    source = bpy.data.images.new("LitSource", 4, 4)
    sampled = lit.node_tree.nodes.new("ShaderNodeTexImage")
    sampled.image = source
    scale = lit.node_tree.nodes.new("ShaderNodeMath")
    scale.operation = "MULTIPLY"
    principled = next(
        node for node in lit.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
    )
    lit.node_tree.links.new(sampled.outputs["Color"], scale.inputs[0])
    lit.node_tree.links.new(scale.outputs["Value"], principled.inputs["Emission Strength"])
    bpy.context.object.data.materials.append(lit)
    with tempfile.TemporaryDirectory() as tmp:
        lit_path = os.path.join(tmp, "lit.glb")
        export_asset._run_export("glb", lit_path, False, False)
        objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]
        materials = [slot.material for obj in objects for slot in obj.material_slots if slot.material]
        report = export_asset._verify_textures("glb", lit_path, materials)
        checks["a map outside the texture slots is dropped without raising"] = (
            report["textures"] == 0 and report["verified"] is None
        )
        lit_result = export_asset._validate_export("gltf", "glb", lit_path, objects, materials, report)
        checks["a dropped map warns and names the material"] = lit_result["ok"] and any(
            c["name"] == "textures" and c["status"] == "warn" and "LitByImage" in c["detail"]
            for c in lit_result["checks"]
        )

    bad = False
    try:
        export_asset._validate_export("nonsense", "stl", "unused", [], [])
    except RuntimeError:
        bad = True
    checks["unknown_profile_rejected"] = bad

print(json.dumps({"module": "export-validation", "checks": checks}, indent=2))
if not all(checks.values()):
    raise SystemExit(1)
print("PASS: export validation profiles report glTF, print and engine facts")
