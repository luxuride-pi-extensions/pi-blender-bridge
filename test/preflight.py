"""Real-Blender fixtures for the deterministic preflight checks.

Run by test/blender-probes.test.mjs (needs `import bpy` and a PASS line).
"""

import os
import sys
import time

# pi-lens-ignore: ast-grep:unresolved-import-python (bpy is provided by Blender)
import bmesh  # pyright: ignore[reportMissingImports]
import bpy  # pyright: ignore[reportMissingImports]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blender.validation import validate  # noqa: E402


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def cube(name="Cube", size=2.0, location=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=size, location=location)
    obj = bpy.context.object
    obj.name = name
    return obj


def open_cube():
    reset()
    obj = cube()
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[bm.faces[0]], context="FACES")
    bm.to_mesh(obj.data)
    bm.free()
    return obj


def material(name="Paint", base_color=None, metallic=None, roughness=None):
    """A Principled material, untouched except for what the caller sets.

    An untouched one is exactly Blender's default surface, which is what the
    default-material check has to recognise.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    principled = next(
        node for node in mat.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
    )
    if base_color is not None:
        principled.inputs["Base Color"].default_value = (*base_color, 1.0)
    if metallic is not None:
        principled.inputs["Metallic"].default_value = metallic
    if roughness is not None:
        principled.inputs["Roughness"].default_value = roughness
    return mat


def main():
    checks = {}

    obj = open_cube()
    verdict = validate(bpy, [obj.name], profile="print")
    checks["open boundary is blocking"] = (
        verdict["checks"]["nonManifold"]["value"]["boundaryEdges"] > 0
        and "nonManifold" in verdict["blockers"]
    )

    reset()
    first = cube("First")
    second = cube("Second", location=(0.6, 0, 0))
    verdict = validate(bpy, [first.name, second.name], profile="print")
    checks["interpenetration is counted"] = (
        verdict["checks"]["selfIntersections"]["value"]["pairs"] > 0
        and "selfIntersections" in verdict["blockers"]
    )

    reset()
    first = cube("First")
    second = cube("Second", location=(0.6, 0, 0))
    verdict = validate(bpy, [first.name, second.name], profile="hard_surface")
    checks["hard surface blocks interpenetration"] = "selfIntersections" in verdict["blockers"]

    reset()
    first = cube("First")
    second = cube("Second")
    verdict = validate(bpy, [first.name, second.name], profile="print")
    checks["coincident duplicates are counted"] = (
        verdict["checks"]["selfIntersections"]["value"]["pairs"] > 0
    )

    reset()
    first = cube("First")
    second = cube("Second", location=(2.0, 0, 0))
    bpy.ops.object.select_all(action="SELECT")
    bpy.context.view_layer.objects.active = first
    bpy.ops.object.join()
    verdict = validate(bpy, [first.name], profile="print")
    checks["a joined pair of touching shells is not a fold"] = (
        verdict["checks"]["selfIntersections"]["value"]["pairs"] == 0
    )
    reset()
    first = cube("First")
    second = cube("Second", location=(0.6, 0, 0))
    bpy.ops.object.select_all(action="SELECT")
    bpy.context.view_layer.objects.active = first
    bpy.ops.object.join()
    verdict = validate(bpy, [first.name], profile="character")
    wall = verdict["checks"]["wallThickness"]["value"]
    checks["a joined interpenetration is a fold"] = (
        verdict["checks"]["selfIntersections"]["value"]["pairs"] > 0
        and wall["otherShellFaces"] > 0
        and wall["distance"] is not None
        and wall["distance"] > 0.01
        and "wallThickness" not in verdict["blockers"]
    )

    reset()
    obj = cube()
    obj.scale = (0.5, 0.5, 0.5)
    verdict = validate(bpy, [obj.name], profile="prop")
    checks["unapplied scale is reported"] = (
        verdict["checks"]["unappliedScale"]["value"][0]["scale"] == [0.5, 0.5, 0.5]
        and "unappliedScale" not in verdict["blockers"]
        and "unappliedScale" in verdict["warnings"]
    )

    reset()
    first = cube("First")
    second = cube("Second", location=(2.0, 0, 0))
    verdict = validate(bpy, [first.name, second.name], profile="print")
    checks["touching faces are not an interpenetration"] = (
        verdict["checks"]["selfIntersections"]["value"]["pairs"] == 0
    )

    reset()
    obj = cube()
    obj.scale = (0.2, 0.2, 0.0002)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    verdict = validate(bpy, [obj.name], profile="print")
    thickness = verdict["checks"]["wallThickness"]["value"]["distance"]
    checks["thin wall is measured"] = (
        thickness is not None and 0.0003 < thickness < 0.0005 and "wallThickness" in verdict["blockers"]
    )

    reset()
    obj = cube()
    obj.scale = (1.0, 1.0, 0.2)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    bpy.context.scene.unit_settings.scale_length = 0.001
    verdict = validate(bpy, [obj.name], profile="print")
    thickness = verdict["checks"]["wallThickness"]["value"]["distance"]
    checks["millimetre scenes compare against metres"] = (
        thickness is not None and 0.0003 < thickness < 0.0005 and "wallThickness" in verdict["blockers"]
    )
    bpy.context.scene.unit_settings.scale_length = 1.0

    reset()
    bpy.ops.mesh.primitive_plane_add(size=2)
    panel = bpy.context.object
    panel.data.materials.append(material("Panel", base_color=(0.2, 0.2, 0.2)))
    verdict = validate(bpy, [panel.name], profile="paint")
    checks["a flat subject is not an empty scene"] = verdict["blocking"] is False
    printed = validate(bpy, [panel.name], profile="print")
    checks["an unmeasurable wall is a warning, not a pass"] = (
        printed["checks"]["wallThickness"]["status"] == "warn"
    )

    reset()
    bpy.ops.mesh.primitive_plane_add(size=2)
    shell = bpy.context.object
    solidify = shell.modifiers.new("Wall", "SOLIDIFY")
    solidify.thickness = 0.0005
    verdict = validate(bpy, [shell.name], profile="print")
    checks["a modifier wall is measured"] = (
        verdict["checks"]["wallThickness"]["value"]["distance"] is not None
        and verdict["checks"]["wallThickness"]["value"]["distance"] < 0.0008
    )

    reset()
    obj = cube()
    while obj.data.uv_layers:
        obj.data.uv_layers.remove(obj.data.uv_layers[0])
    obj.data.materials.append(material("Unwrapped", base_color=(0.3, 0.3, 0.3)))
    verdict = validate(bpy, [obj.name], profile="paint")
    checks["missing UV is blocking for paint"] = "uv" in verdict["blockers"]

    # A material nobody painted is a modelling failure, not a taste question: it
    # renders as grey plastic whatever the lighting does. The check reads the
    # material because a picture cannot separate it from a grey somebody chose;
    # paint blocks, every other profile only warns so it reaches the reviewer
    # before a critic is spent.
    reset()
    obj = cube("Default")
    obj.data.materials.append(material("Untouched"))
    verdict = validate(bpy, [obj.name], profile="prop")
    checks["a default material warns prop"] = (
        verdict["checks"]["defaultMaterial"]["status"] == "warn"
        and verdict["checks"]["defaultMaterial"]["value"]["materials"] == ["Untouched"]
        and "defaultMaterial" in verdict["warnings"]
        and "defaultMaterial" not in verdict["blockers"]
        and verdict["blocking"] is False
    )
    checks["an assigned material is not a missing one"] = (
        verdict["checks"]["missingMaterial"]["status"] == "ok"
        and verdict["checks"]["missingMaterial"]["value"]["objects"] == []
    )
    painted = validate(bpy, [obj.name], profile="paint")
    checks["a default material blocks paint"] = (
        painted["checks"]["defaultMaterial"]["status"] == "block"
        and "defaultMaterial" in painted["blockers"]
        and painted["blocking"] is True
    )

    reset()
    obj = cube("Painted")
    obj.data.materials.append(material("Red", base_color=(0.7, 0.05, 0.05)))
    verdict = validate(bpy, [obj.name], profile="paint")
    checks["a custom base colour is a painted material"] = (
        verdict["checks"]["defaultMaterial"]["status"] == "ok"
        and verdict["blocking"] is False
    )

    # Roughness and metallic are paint decisions too: deliberately glossy grey is
    # authored work, and reporting it as default would send a manager to redo a
    # material somebody chose.
    reset()
    obj = cube("Glossy")
    obj.data.materials.append(material("Glossy", roughness=0.15, metallic=0.6))
    verdict = validate(bpy, [obj.name], profile="prop")
    checks["non-default metallic and roughness are painted"] = (
        verdict["checks"]["defaultMaterial"]["status"] == "ok"
    )

    reset()
    obj = cube("Mapped")
    textured = material("Mapped")
    image = bpy.data.images.new("Map", 4, 4)
    texture_node = textured.node_tree.nodes.new("ShaderNodeTexImage")
    texture_node.image = image
    principled = next(
        node for node in textured.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
    )
    textured.node_tree.links.new(texture_node.outputs["Color"], principled.inputs["Base Color"])
    obj.data.materials.append(textured)
    verdict = validate(bpy, [obj.name], profile="prop")
    checks["a texture link is a painted material"] = (
        verdict["checks"]["defaultMaterial"]["status"] == "ok"
        and verdict["checks"]["materials"]["status"] == "ok"
    )

    reset()
    obj = cube("Bare")
    verdict = validate(bpy, [obj.name], profile="prop")
    checks["a mesh with no material slot is reported"] = (
        verdict["checks"]["missingMaterial"]["status"] == "warn"
        and verdict["checks"]["missingMaterial"]["value"]["objects"] == ["Bare"]
        and "missingMaterial" in verdict["warnings"]
        and "defaultMaterial" not in verdict["warnings"]
    )
    bare_paint = validate(bpy, [obj.name], profile="paint")
    checks["a mesh with no material blocks paint"] = (
        "missingMaterial" in bare_paint["blockers"] and bare_paint["blocking"] is True
    )

    reset()
    obj = cube("EmptySlot")
    obj.data.materials.append(None)
    verdict = validate(bpy, [obj.name], profile="prop")
    checks["an empty slot counts as no material"] = (
        verdict["checks"]["missingMaterial"]["value"]["objects"] == ["EmptySlot"]
    )

    reset()
    obj = cube()
    verdict = validate(bpy, [obj.name], profile="prop")
    checks["a clean cube is not blocking"] = verdict["blocking"] is False
    checks["digest subject can be the clean cube"] = verdict["checks"]["empty"]["status"] == "ok"

    # A heavy grid must stay bounded: the scan is skipped, not attempted.
    reset()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=320, y_subdivisions=320, size=2)
    heavy = bpy.context.object
    started = time.time()
    verdict = validate(bpy, [heavy.name], profile="prop")
    elapsed = time.time() - started
    checks["a heavy mesh is bounded"] = (
        verdict["checks"]["selfIntersections"]["status"] == "warn"
        and "exceeds" in str(verdict["checks"]["selfIntersections"].get("reason"))
        and elapsed < 20
    )

    # Blender renames a datablock that claims a taken name, so a script that
    # recreates its own object leaves `Hero` and `Hero.001` stacked. A copy made
    # by this run is a duplicate on every profile; it is disqualifying only where
    # a stacked twin cannot be judged away.
    reset()
    base = cube("Hero")
    copy = cube("Hero")
    checks["blender renamed the re-created object"] = copy.name == "Hero.001"
    for profile in ("character", "hard_surface", "print"):
        verdict = validate(
            bpy, [base.name, copy.name], profile=profile,
            added_objects=[copy.name], owned_objects=["Hero"],
        )
        checks[f"an owned duplicate blocks {profile}"] = (
            verdict["checks"]["duplicateObjects"]["value"]["duplicates"]
            == [{"base": "Hero", "copies": ["Hero.001"]}]
            and "duplicateObjects" in verdict["blockers"]
            and verdict["blocking"] is True
        )
    for profile in ("prop", "paint"):
        verdict = validate(
            bpy, [base.name, copy.name], profile=profile,
            added_objects=[copy.name], owned_objects=["Hero"],
        )
        checks[f"an owned duplicate warns {profile}"] = (
            verdict["checks"]["duplicateObjects"]["status"] == "warn"
            and "duplicateObjects" in verdict["warnings"]
            and "duplicateObjects" not in verdict["blockers"]
        )

    # A `.NNN` name whose base is nowhere is a naming choice, not a copy, and a
    # pre-existing numbered datablock is not this run's addition.
    reset()
    numbered = cube()
    numbered.name = "Bolt.001"
    verdict = validate(bpy, [numbered.name], profile="character", added_objects=[numbered.name])
    checks["an unrelated .001 name is not a duplicate"] = (
        verdict["checks"]["duplicateObjects"]["status"] == "ok"
        and verdict["checks"]["duplicateObjects"]["value"]["duplicates"] == []
    )
    reset()
    pre_existing = cube("Bolt")
    numbered = cube("Bolt")
    verdict = validate(
        bpy, [pre_existing.name, numbered.name], profile="character",
        added_objects=[], owned_objects=["Bolt"],
    )
    checks["a pre-existing numbered object is not this run's duplicate"] = (
        verdict["checks"]["duplicateObjects"]["status"] == "ok"
    )

    # A generative blockout is a starting point, so the profiles that ship a
    # hand-modelled surface refuse it outright and prop merely says so. The tag
    # is what the importer writes; nothing here depends on the topology being
    # bad, because a manifold blockout is still unbuilt.
    for profile in ("character", "hard_surface", "print"):
        reset()
        obj = cube("BlockoutBody")
        obj["pi_blender_blockout"] = True
        verdict = validate(bpy, [obj.name], profile=profile)
        checks[f"a blockout blocks {profile}"] = (
            verdict["checks"]["blockout"]["status"] == "block"
            and "blockout" in verdict["blockers"]
            and verdict["blocking"] is True
            and "rebuild" in verdict["checks"]["blockout"]["reason"]
        )
    for profile in ("prop", "interior", "asset_pack"):
        reset()
        obj = cube("BlockoutBody")
        obj["pi_blender_blockout"] = True
        verdict = validate(bpy, [obj.name], profile=profile)
        checks[f"a blockout warns {profile}"] = (
            verdict["checks"]["blockout"]["status"] == "warn"
            and "blockout" in verdict["warnings"]
            and "blockout" not in verdict["blockers"]
            and verdict["blocking"] is False
        )

    # Rebuilt geometry must stop tripping the gate, or the check is a dead end:
    # the tag is the whole signal, so clearing it is what finishes the rebuild.
    reset()
    obj = cube("Rebuilt")
    verdict = validate(bpy, [obj.name], profile="character")
    checks["an untagged mesh passes the blockout gate"] = (
        verdict["checks"]["blockout"]["status"] == "ok" and "blockout" not in verdict["blockers"]
    )

    for name, passed in checks.items():
        print(f"{'ok' if passed else 'FAIL'}: {name}")
    if not all(checks.values()):
        raise SystemExit(1)
    print("PASS: deterministic preflight")


if __name__ == "__main__":
    main()
