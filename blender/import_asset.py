"""
Import an external asset into the open workspace blend.

Supports obj, stl, gltf/glb, and probes fbx capability.
Provides checkpoint-safe ingress, path traversal guards, asset name sanitization,
and post-import reporting of non-uniform scale and principal axes.
"""

import math
import os
import re

# Provided by Blender's own runtime, not by anything installed here.
# pi-lens-ignore: ast-grep:unresolved-import-python
import bpy  # pyright: ignore[reportMissingImports]

try:
    # pi-lens-ignore: ast-grep:unresolved-import-python
    from mathutils import Matrix, Vector  # pyright: ignore[reportMissingImports]
except ImportError:
    Matrix = None
    Vector = None


SUPPORTED_FORMATS = ("obj", "stl", "gltf", "glb", "fbx")


def _probe_format_and_addon(fmt):
    """Probe whether the requested format and its operator/add-on are available.

    Returns (is_available, status_string, error_message).
    """
    if fmt not in SUPPORTED_FORMATS:
        return (
            False,
            "unsupported_format",
            f'Unsupported import format "{fmt}". Supported: {", ".join(SUPPORTED_FORMATS)}.',
        )

    if fmt == "fbx":
        has_fbx = hasattr(bpy.ops, "import_scene") and hasattr(bpy.ops.import_scene, "fbx")
        if not has_fbx:
            return (
                False,
                "missing_addon",
                "FBX import operator is not available in this Blender installation (io_scene_fbx add-on is missing or disabled).",
            )
        return (True, None, None)

    if fmt == "obj":
        has_obj = hasattr(bpy.ops.wm, "obj_import") or (
            hasattr(bpy.ops, "import_scene") and hasattr(bpy.ops.import_scene, "obj")
        )
        if not has_obj:
            return (
                False,
                "missing_addon",
                "OBJ import operator is not available in this Blender installation.",
            )
        return (True, None, None)

    if fmt == "stl":
        has_stl = hasattr(bpy.ops.wm, "stl_import") or (
            hasattr(bpy.ops, "import_mesh") and hasattr(bpy.ops.import_mesh, "stl")
        )
        if not has_stl:
            return (
                False,
                "missing_addon",
                "STL import operator is not available in this Blender installation.",
            )
        return (True, None, None)

    if fmt in ("gltf", "glb"):
        has_gltf = hasattr(bpy.ops, "import_scene") and hasattr(bpy.ops.import_scene, "gltf")
        if not has_gltf:
            return (
                False,
                "missing_addon",
                "glTF/GLB import operator (bpy.ops.import_scene.gltf) is not available in this Blender installation.",
            )
        return (True, None, None)

    return (True, None, None)


def _run_import(fmt, filepath):
    """Dispatch to whichever operator this format needs."""
    if fmt in ("glb", "gltf"):
        return bpy.ops.import_scene.gltf(filepath=filepath)

    if fmt == "obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            return bpy.ops.wm.obj_import(filepath=filepath)
        return bpy.ops.import_scene.obj(filepath=filepath)

    if fmt == "stl":
        if hasattr(bpy.ops.wm, "stl_import"):
            return bpy.ops.wm.stl_import(filepath=filepath)
        return bpy.ops.import_mesh.stl(filepath=filepath)

    if fmt == "fbx":
        return bpy.ops.import_scene.fbx(filepath=filepath)

    raise ValueError(f"Unknown format {fmt}")


def compute_principal_axes(obj):
    """Compute principal axes from vertex coordinates of a mesh object."""
    if obj.type != "MESH" or not hasattr(obj.data, "vertices") or len(obj.data.vertices) < 3 or Vector is None:
        return {
            "available": False,
            "reason": "Object is not a mesh or has fewer than 3 vertices",
        }

    vertices = obj.data.vertices
    count = len(vertices)
    limit = 2000
    stride = max(1, count // limit)
    points = [obj.matrix_world @ vertices[index].co for index in range(0, count, stride)]
    if len(points) < 3:
        return {"available": False, "reason": "Fewer than 3 sampled vertices"}

    n = len(points)
    center = Vector((
        sum(p.x for p in points) / n,
        sum(p.y for p in points) / n,
        sum(p.z for p in points) / n,
    ))
    centered = [p - center for p in points]

    cov = [
        [sum(p[i] * p[j] for p in centered) / n for j in range(3)]
        for i in range(3)
    ]

    v1 = Vector((1.0, 0.0, 0.0))
    for _ in range(64):
        pv = Vector((
            cov[0][0] * v1.x + cov[0][1] * v1.y + cov[0][2] * v1.z,
            cov[1][0] * v1.x + cov[1][1] * v1.y + cov[1][2] * v1.z,
            cov[2][0] * v1.x + cov[2][1] * v1.y + cov[2][2] * v1.z,
        ))
        if pv.length < 1e-12:
            pv = Vector((0.0, 1.0, 0.0))
        v1 = pv.normalized()

    temp = Vector((0.0, 1.0, 0.0)) if abs(v1.dot(Vector((0.0, 1.0, 0.0)))) < 0.9 else Vector((0.0, 0.0, 1.0))
    v2_proj = temp - (temp.dot(v1) * v1)
    v2 = v2_proj.normalized()

    for _ in range(32):
        pv = Vector((
            cov[0][0] * v2.x + cov[0][1] * v2.y + cov[0][2] * v2.z,
            cov[1][0] * v2.x + cov[1][1] * v2.y + cov[1][2] * v2.z,
            cov[2][0] * v2.x + cov[2][1] * v2.y + cov[2][2] * v2.z,
        ))
        pv = pv - (pv.dot(v1) * v1)
        if pv.length < 1e-12:
            break
        v2 = pv.normalized()

    v3 = v1.cross(v2).normalized()

    def variance(axis):
        p_axis = Vector((
            cov[0][0] * axis.x + cov[0][1] * axis.y + cov[0][2] * axis.z,
            cov[1][0] * axis.x + cov[1][1] * axis.y + cov[1][2] * axis.z,
            cov[2][0] * axis.x + cov[2][1] * axis.y + cov[2][2] * axis.z,
        ))
        return max(0.0, float(p_axis.dot(axis)))

    axes_data = sorted([
        ("primary", v1, variance(v1)),
        ("secondary", v2, variance(v2)),
        ("tertiary", v3, variance(v3)),
    ], key=lambda x: x[2], reverse=True)

    return {
        "available": True,
        "center": [round(float(c), 4) for c in center],
        "primaryAxis": [round(float(c), 4) for c in axes_data[0][1]],
        "secondaryAxis": [round(float(c), 4) for c in axes_data[1][1]],
        "tertiaryAxis": [round(float(c), 4) for c in axes_data[2][1]],
        "variances": [round(float(x[2]), 6) for x in axes_data],
        "extents": [round(float(math.sqrt(x[2])), 4) for x in axes_data],
    }


def _build_post_import_report(imported_objects):
    """Generate report on scale uniformity and principal axes for imported objects."""
    objects_report = {}
    non_uniform_objects = []

    for obj in imported_objects:
        matrix = obj.matrix_world
        basis = matrix.to_3x3()
        world_col_scales = [basis.col[i].length for i in range(3)]
        local_scale = [float(s) for s in obj.scale]

        # Non-uniform if either local or world scale shows variation across axes
        local_diff = max(local_scale) - min(local_scale)
        world_diff = max(world_col_scales) - min(world_col_scales)
        is_non_uniform = (local_diff > 1e-4) or (world_diff > 1e-4)

        if is_non_uniform:
            non_uniform_objects.append(obj.name)

        dimensions = [round(float(d), 4) for d in obj.dimensions]
        axes_info = compute_principal_axes(obj)

        objects_report[obj.name] = {
            "name": obj.name,
            "scale": [round(float(s), 4) for s in local_scale],
            "worldScale": [round(float(s), 4) for s in world_col_scales],
            "isNonUniform": is_non_uniform,
            "dimensions": dimensions,
            "principalAxes": axes_info,
        }

    return {
        "hasNonUniformScale": len(non_uniform_objects) > 0,
        "nonUniformScaleObjects": non_uniform_objects,
        "objects": objects_report,
    }


def handle_import(request, open_blend):
    """Import an external 3D asset into the workspace blend.

    Saves the mutated blend back to disk upon successful import.
    Reports unsupported format or missing add-ons as explicit result objects.
    """
    open_blend(request)

    source_path = request.get("sourcePath")
    if not source_path or not os.path.exists(source_path):
        return {
            "success": False,
            "status": "file_not_found",
            "error": f"Source asset does not exist: {source_path}",
            "importedObjects": [],
        }

    fmt = str(request.get("format") or os.path.splitext(source_path)[1].lstrip(".")).lower()

    # Asset name sanitization
    raw_name = request.get("name") or os.path.splitext(os.path.basename(source_path))[0]
    asset_name = re.sub(r"^[./\\]+", "", str(raw_name))
    asset_name = re.sub(r"[./\\]+", "_", asset_name)
    asset_name = re.sub(r"[^A-Za-z0-9_-]", "_", asset_name)
    asset_name = re.sub(r"_{2,}", "_", asset_name).strip("._-") or "asset"

    # Capability probing
    supported, status, err = _probe_format_and_addon(fmt)
    if not supported:
        return {
            "success": False,
            "status": status,
            "format": fmt,
            "assetName": asset_name,
            "error": err,
            "importedObjects": [],
        }

    # Snapshot existing objects so we know which ones were added
    existing_names = set(bpy.data.objects.keys())

    try:
        _run_import(fmt, source_path)
    except Exception as error:
        return {
            "success": False,
            "status": "import_failed",
            "format": fmt,
            "assetName": asset_name,
            "error": f"Blender {fmt} import failed: {error}",
            "importedObjects": [],
        }

    new_objects = [obj for name, obj in bpy.data.objects.items() if name not in existing_names]
    if not new_objects:
        new_objects = list(bpy.context.selected_objects)

    imported_names = [obj.name for obj in new_objects]
    report = _build_post_import_report(new_objects)

    # Save mutated blend to disk
    bpy.ops.wm.save_mainfile()

    return {
        "success": True,
        "status": "imported",
        "format": fmt,
        "sourcePath": source_path,
        "assetName": asset_name,
        "blendPath": bpy.data.filepath or None,
        "importedObjects": imported_names,
        "report": report,
    }


# --- Generative blockout ingress -------------------------------------------
#
# A blockout is a provider's starting point, not a module output: the topology
# is dense, usually non-manifold and often unwrapped. It is imported into its
# own collection in its own blend, tagged, and measured, because the one thing
# that must not happen is a worker treating it as finished geometry.

# The tag is owned by validation, which is what reads it back: one spelling for
# the writer and the gate, or the gate silently passes what the importer wrote.
try:
    from validation import BLOCKOUT_TAG  # noqa: E402
except ImportError:
    # Imported as `blender.import_asset` (which is how the probes reach it)
    # rather than as a sibling module on the bridge's own sys.path.
    from blender.validation import BLOCKOUT_TAG  # noqa: E402

BLOCKOUT_COLLECTION = "Blockout"

# glTF fixes its unit to metres. Every other format carries whatever the author
# used, and nothing in the file says which, so the scale is a stated assumption
# rather than a measurement.
UNIT_KNOWN_FORMATS = ("gltf", "glb")

# Above this the surface is a scan, not a blockout: retopology at that density is
# a rewrite, and the manager should see the number before it plans the work.
DENSE_TRIANGLES = 50000

# A blockout is upright enough when its longest axis is this close to +Z.
UPRIGHT_TOLERANCE_DEGREES = 10.0


def _world_bounds(objects):
    """The world-space box around the given objects, or None for no geometry.

    Mesh vertices rather than `obj.bound_box`: the box is a depsgraph result, and
    the normaliser rewrites mesh data between passes, so a cached box would
    report the size the import arrived at instead of the size it was just made.
    """
    low = [math.inf, math.inf, math.inf]
    high = [-math.inf, -math.inf, -math.inf]
    for obj in objects:
        if obj.type == "MESH" and obj.data is not None:
            points = (vertex.co for vertex in obj.data.vertices)
        else:
            points = (Vector(corner) for corner in obj.bound_box)
        for co in points:
            point = obj.matrix_world @ co
            for axis in range(3):
                low[axis] = min(low[axis], point[axis])
                high[axis] = max(high[axis], point[axis])
    if low[0] == math.inf:
        return None
    return {
        "min": [round(float(value), 5) for value in low],
        "max": [round(float(value), 5) for value in high],
        "dimensions": [round(float(high[axis] - low[axis]), 5) for axis in range(3)],
    }


def _apply_matrix(objects, matrix):
    """Bake `matrix` into the mesh data and leave every object transform identity.

    Baking rather than storing an object-level scale is what makes the result
    usable downstream: a leftover 0.01 object scale is exactly the unappliedScale
    defect preflight blocks on, and the worker is meant to rebuild from this
    geometry, not to scale it again.
    """
    for obj in objects:
        world = matrix @ obj.matrix_world
        # A glTF importer parents the scene's meshes under a root empty; keeping
        # the parent would re-apply its transform after the bake.
        obj.parent = None
        if obj.type != "MESH" or obj.data is None:
            obj.matrix_world = world
            continue
        mesh = obj.data
        # One mesh datablock can be instanced across several objects, and moving
        # a shared datablock once per object would move it as many times.
        if mesh.users > 1:
            obj.data = mesh.copy()
            mesh = obj.data
        mesh.transform(world)
        obj.matrix_world = Matrix.Identity(4)


def _normalise_blockout(objects, target_bounds):
    """Stand the import upright and fit it inside the requested box.

    A generator answers in its own pose and its own scale: nothing in a GLB
    promises the model's up axis is +Z, and providers train at whatever size
    their dataset used. Both are resolved here, so the caller reads one bounds
    triple that is already in metres, already centred, and already resting on
    Z=0. Returns the final bounds and the notes worth showing the caller.
    """
    _apply_matrix(objects, Matrix.Identity(4))
    meshes = [obj for obj in objects if obj.type == "MESH" and obj.data is not None]
    if not meshes:
        return None, ["the import contains no mesh objects"]
    notes = []

    reference = max(meshes, key=lambda obj: len(obj.data.vertices))
    axes = compute_principal_axes(reference)
    if not axes.get("available"):
        notes.append(
            f"orientation was not normalised: {axes.get('reason') or 'no principal axis'}"
        )
    else:
        primary = Vector(axes["primaryAxis"])
        if primary.length > 1e-6:
            # A model lying upside down is a flip rather than a tilt, so pick the
            # end of the axis that points up before measuring the angle.
            if primary.z < 0:
                primary = -primary
            angle = primary.angle(Vector((0.0, 0.0, 1.0)))
            if angle > math.radians(UPRIGHT_TOLERANCE_DEGREES):
                box = _world_bounds(objects)
                centre = Vector([(box["min"][axis] + box["max"][axis]) / 2.0 for axis in range(3)])
                rotation = primary.rotation_difference(Vector((0.0, 0.0, 1.0))).to_matrix().to_4x4()
                _apply_matrix(
                    objects,
                    Matrix.Translation(centre) @ rotation @ Matrix.Translation(-centre),
                )
                notes.append(f"rotated {math.degrees(angle):.1f} degrees so the longest axis is +Z")

    box = _world_bounds(objects)
    if target_bounds:
        factors = [
            target_bounds[axis] / box["dimensions"][axis]
            for axis in range(3)
            if target_bounds[axis] > 0 and box["dimensions"][axis] > 1e-9
        ]
        # The smallest factor is the one that binds: the model fits inside the
        # box on every axis rather than filling it on one and overflowing the
        # others, because a blockout that overflows is exactly the export the
        # bounds check rejects later.
        factor = min(factors) if factors else 1.0
        if abs(factor - 1.0) > 1e-6:
            _apply_matrix(objects, Matrix.Scale(factor, 4))
            notes.append(f"scaled by {factor:.4f} to fit the target bounds")
        box = _world_bounds(objects)

    offset = Vector((
        -(box["min"][0] + box["max"][0]) / 2.0,
        -(box["min"][1] + box["max"][1]) / 2.0,
        -box["min"][2],
    ))
    _apply_matrix(objects, Matrix.Translation(offset))
    return _world_bounds(objects), notes


def _triangle_count(meshes):
    total = 0
    for obj in meshes:
        if obj.type != "MESH" or obj.data is None:
            continue
        obj.data.calc_loop_triangles()
        total += len(obj.data.loop_triangles)
    return total


def _missing_uv(meshes):
    missing = []
    for obj in meshes:
        if obj.type != "MESH" or obj.data is None:
            continue
        layers = getattr(obj.data, "uv_layers", None)
        if not layers or len(layers) == 0:
            missing.append(obj.name)
    return sorted(missing)


def _blockout_collection():
    """The one collection a blockout lives in, created on first use.

    Separate from the module's own collection because the module's collection is
    what `branch_apply` merges into model.blend. A blockout in it would be a
    blockout in the shipped mesh.
    """
    collection = bpy.data.collections.get(BLOCKOUT_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(BLOCKOUT_COLLECTION)
    if collection.name not in {child.name for child in bpy.context.scene.collection.children}:
        bpy.context.scene.collection.children.link(collection)
    return collection


def _target_bounds(raw):
    """The requested fit box in metres, or (None, None) when unrestricted."""
    if raw is None:
        return None, None
    values = [raw.get(key) for key in ("x", "y", "z")] if isinstance(raw, dict) else list(raw)
    if len(values) != 3:
        return None, "targetBounds must be { x, y, z } in metres."
    try:
        numbers = [float(value) for value in values]
    except (TypeError, ValueError):
        return None, "targetBounds must be { x, y, z } in metres."
    if any(number <= 0 for number in numbers):
        return None, "targetBounds must be positive metres on every axis."
    return numbers, None


def handle_blockout_import(request, open_blend=None):
    """Import a generated blockout into its own blend, tagged and measured.

    Returns the provenance the caller writes into the reference manifest and the
    warnings a worker needs before it plans a rebuild. Nothing is merged into
    model.blend: the caller decides what to rebuild from this, which is the whole
    difference between a blockout and an asset.
    """
    if open_blend is not None:
        open_blend(request)

    source_path = request.get("sourcePath")
    if not source_path or not os.path.exists(source_path):
        return {
            "success": False,
            "status": "file_not_found",
            "error": f"Blockout source does not exist: {source_path}",
        }

    fmt = str(request.get("format") or os.path.splitext(source_path)[1].lstrip(".")).lower()
    if fmt not in ("glb", "gltf", "obj"):
        return {
            "success": False,
            "status": "unsupported_format",
            "format": fmt,
            "error": f'Blockout import accepts glb, gltf or obj; got "{fmt}".',
        }

    supported, status, error = _probe_format_and_addon(fmt)
    if not supported:
        return {"success": False, "status": status, "format": fmt, "error": error}

    target_bounds, bounds_error = _target_bounds(request.get("targetBounds"))
    if bounds_error:
        return {"success": False, "status": "invalid_target_bounds", "format": fmt, "error": bounds_error}

    existing_names = set(bpy.data.objects.keys())
    try:
        _run_import(fmt, source_path)
    except Exception as failure:
        return {
            "success": False,
            "status": "import_failed",
            "format": fmt,
            "error": f"Blender {fmt} import failed: {failure}",
        }

    imported = [obj for name, obj in bpy.data.objects.items() if name not in existing_names]
    if not imported:
        imported = list(bpy.context.selected_objects)
    if not imported:
        return {
            "success": False,
            "status": "empty_import",
            "format": fmt,
            "error": "The source imported no objects.",
        }

    collection = _blockout_collection()
    for obj in imported:
        for other in list(obj.users_collection):
            other.objects.unlink(obj)
        collection.objects.link(obj)
        obj[BLOCKOUT_TAG] = True

    bounds, notes = _normalise_blockout(imported, target_bounds)
    triangles = _triangle_count(imported)

    warnings = []
    if fmt not in UNIT_KNOWN_FORMATS:
        warnings.append(
            f"{fmt} states no unit: the source scale was read as metres, so correct it if it was authored in anything else"
        )
    if triangles > DENSE_TRIANGLES:
        warnings.append(
            f"{triangles} triangles is scan-grade, not blockout-grade: decimate or retopologise before comparing it to a render"
        )
    missing_uv = _missing_uv(imported)
    if missing_uv:
        warnings.append(
            f"no UV map on {', '.join(missing_uv)}: the blockout cannot be painted until it is unwrapped"
        )

    blend_path = request.get("blendPath")
    if blend_path:
        blend_path = os.path.abspath(blend_path)
        directory = os.path.dirname(blend_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    else:
        bpy.ops.wm.save_mainfile()

    provider = str(request.get("provider") or "local")
    raw_name = request.get("name") or os.path.splitext(os.path.basename(source_path))[0]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(raw_name)).strip("-").lower() or "blockout"
    return {
        "success": True,
        "status": "imported",
        "format": fmt,
        "sourcePath": os.path.abspath(source_path),
        "blendPath": bpy.data.filepath or None,
        "collection": collection.name,
        "objects": [obj.name for obj in imported],
        "notes": notes,
        "provenance": {
            "id": request.get("id") or f"{provider}-{slug}",
            "path": os.path.abspath(source_path),
            "provider": provider,
            "license": request.get("license") or "unknown",
            "prompt": request.get("prompt"),
            "seed": request.get("seed"),
            "bounds": bounds,
            "triangles": triangles,
            "blockout": True,
        },
        "warnings": warnings,
    }
