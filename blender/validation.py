"""Deterministic structural checks for a module's geometry.

The vision critic is asked to judge a render, and a render can hide an open
surface, an interpenetration or a wall thinner than the material it imitates.
These checks answer those questions from geometry instead, before a critic
context is spent.

`bpy` is passed in rather than imported at module scope so the thresholds and
the verdict can be tested outside Blender. Every number states its method and
its sample, because a confident number from an unexplained sample is worse than
no number: the caller has to be able to decide whether to trust it.
"""

import math
import re

# Profiles are the same names the camera plans use: one declared use case should
# not have to be repeated in two vocabularies.
PROFILES = (
    "prop",
    "character",
    "hard_surface",
    "interior",
    "print",
    "paint",
    "asset_pack",
)

MIN_WALL_METRES = {
    "print": 0.0008,
    "character": 0.002,
}

# A check is critical only where the profile makes the defect disqualifying. A
# one-sided prop back is a style choice; the same face on a print is a hole.
CRITICAL = {
    "prop": {"empty", "bounds"},
    "character": {"empty", "blockout", "unappliedScale", "bounds", "nonManifold", "wallThickness", "duplicateObjects"},
    "hard_surface": {"empty", "blockout", "unappliedScale", "bounds", "selfIntersections", "duplicateObjects"},
    "interior": {"empty", "bounds"},
    "print": {"empty", "blockout", "unappliedScale", "bounds", "nonManifold", "selfIntersections", "wallThickness", "duplicateObjects"},
    # A paint job is judged on the material it applied: a mesh with no material,
    # or one still on Blender's default surface, has nothing for a critic to see.
    "paint": {"uv", "missingMaterial", "defaultMaterial"},
    "asset_pack": {"empty", "bounds"},
}

SELF_INTERSECTION_FACE_LIMIT = 60000

# Blender's own Principled defaults: a Base Color of 0.8 grey, Metallic 0.0,
# Roughness 0.5 and no texture. A material still sitting on those numbers is the
# surface a mesh gets when nobody assigned one, and it renders as untextured
# grey plastic -- a modelling failure to fix, not a look to judge. The tolerance
# is per channel on 0..1, so it absorbs a value nudged in the UI without giving
# up on "this is still the default grey".
DEFAULT_BASE_COLOR = (0.8, 0.8, 0.8)
DEFAULT_METALLIC = 0.0
DEFAULT_ROUGHNESS = 0.5
DEFAULT_TOLERANCE = 1e-3

# The sockets a paint job writes to. A link into any of them is texture or
# procedural work, which the exporter carries as a material of its own; an
# explicit value on them is a decision somebody made, even a grey one.
PAINTED_SOCKETS = ("Base Color", "Metallic", "Roughness", "Normal", "Emission Color")

# Blender's own disambiguation suffix: `Hero` becomes `Hero.001` when a second
# datablock claims the same name. Three digits is Blender's width; matching more
# keeps a deliberate `Part.2024` from reading as a copy of `Part`.
DUPLICATE_SUFFIX = re.compile(r"^(?P<base>.+)\.(?P<index>\d{3,})$")


def _round(value, digits=5):
    return round(float(value), digits) if value is not None else None


def _check(status, value=None, method=None, limit=None, sampled=False, reason=None):
    result = {"status": status}
    if value is not None:
        result["value"] = value
    if method is not None:
        result["method"] = method
    if limit is not None:
        result["limit"] = limit
    if sampled:
        result["sampled"] = True
    if reason is not None:
        result["reason"] = reason
    return result


def _duplicate_objects(bpy, candidates, owned_objects, objects):
    """Copies this run made of a datablock that already existed under the base name.

    Neither `bpy.data.objects.new("Hero", ...)` nor a primitive operator replaces
    an existing datablock: Blender renames the NEW one to `Hero.001`. The copy is
    a distinct datablock, so the content diff reports it as an addition and the
    apply carries BOTH into the model, leaving two objects stacked in place. The
    self-intersection scan only measures the stack, and it is critical for
    hard-surface and print alone, so a prop or paint run passed preflight with
    the twin in it.

    Only names in `candidates` are considered, so a deliberate `Bolt.001` that
    was already in the file is not this script's mistake. The base has to be
    something this module owns or the file already holds; a `.001` whose base is
    nowhere is a naming choice, not a duplicate.
    """
    bases = set(owned_objects or ()) | set(objects or ())
    bases.update(obj.name for obj in bpy.data.objects)
    copies_by_base = {}
    for name in candidates or ():
        match = DUPLICATE_SUFFIX.match(name)
        if match is None or match.group("base") not in bases:
            continue
        copies_by_base.setdefault(match.group("base"), []).append(name)
    return [{"base": base, "copies": sorted(names)} for base, names in sorted(copies_by_base.items())]

BLOCKOUT_TAG = "pi_blender_blockout"


def _blockouts(bpy, objects):
    """Objects still carrying a generative blockout tag.

    A blockout is a starting point, not a module output. The provider answers in
    its own topology -- dense, usually non-manifold, often with no UVs -- and the
    tag is what lets this verdict tell it apart from geometry a worker actually
    built. The check reads the tag rather than the geometry, because a blockout
    that happens to be manifold is still unbuilt: nothing has decided what its
    front, its shell or its material split should be.
    """
    found = []
    for name in objects or ():
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.get(BLOCKOUT_TAG):
            found.append(obj.name)
    return sorted(found)


def _mesh_objects(bpy, objects):
    found = []
    for name in objects:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "MESH" and obj.data is not None:
            found.append(obj)
    return found


def _evaluated(obj, depsgraph):
    """The mesh Blender actually evaluates, with modifiers and shape keys applied.

    Reading obj.data measures the base mesh, which is not what the user sees or
    exports: a Solidify wall or a Mirror seam lives in the modifier stack.
    """
    return obj.evaluated_get(depsgraph).to_mesh()


def _bounds(bpy, meshes, depsgraph):
    low = [math.inf, math.inf, math.inf]
    high = [-math.inf, -math.inf, -math.inf]
    measured = False
    for obj in meshes:
        mesh = _evaluated(obj, depsgraph)
        try:
            matrix = obj.matrix_world
            for vertex in mesh.vertices:
                world = matrix @ vertex.co
                for axis in range(3):
                    low[axis] = min(low[axis], world[axis])
                    high[axis] = max(high[axis], world[axis])
                measured = True
        finally:
            obj.evaluated_get(depsgraph).to_mesh_clear()
    dimensions = [high[axis] - low[axis] for axis in range(3)] if measured else [0.0, 0.0, 0.0]
    return {
        "min": [_round(value, 4) for value in low] if measured else None,
        "max": [_round(value, 4) for value in high] if measured else None,
        "dimensions": [_round(value, 4) for value in dimensions],
        "unitScale": _round(getattr(bpy.context.scene.unit_settings, "scale_length", 1.0), 4),
    }


def _unapplied_scale(meshes):
    offenders = []
    for obj in meshes:
        if any(abs(float(value) - 1.0) > 1e-4 for value in obj.scale):
            offenders.append({"object": obj.name, "scale": [_round(value, 4) for value in obj.scale]})
    return offenders


def _topology(bpy, meshes, depsgraph):
    # pi-lens-ignore: ast-grep:unresolved-import-python
    import bmesh  # pyright: ignore[reportMissingImports]

    non_manifold = 0
    boundary = 0
    faces = 0
    for obj in meshes:
        bm = bmesh.new()
        try:
            bm.from_mesh(_evaluated(obj, depsgraph))
            faces += len(bm.faces)
            non_manifold += sum(1 for edge in bm.edges if not edge.is_manifold)
            boundary += sum(1 for edge in bm.edges if len(edge.link_faces) == 1)
        finally:
            bm.free()
            obj.evaluated_get(depsgraph).to_mesh_clear()
    return {"nonManifoldEdges": non_manifold, "boundaryEdges": boundary, "faces": faces}


def _self_intersections(bpy, meshes, faces, depsgraph):
    if faces > SELF_INTERSECTION_FACE_LIMIT:
        return None, f"skipped: {faces} faces exceeds the {SELF_INTERSECTION_FACE_LIMIT}-face scan limit"
    from mathutils import Vector  # pyright: ignore[reportMissingImports]
    from mathutils.bvhtree import BVHTree  # pyright: ignore[reportMissingImports]

    vertices = []
    triangles = []
    owners = []
    corners_world = []
    for ordinal, obj in enumerate(meshes):
        mesh = _evaluated(obj, depsgraph)
        try:
            offset = len(vertices)
            matrix = obj.matrix_world
            vertices.extend(matrix @ vertex.co for vertex in mesh.vertices)
            for polygon in mesh.polygons:
                corners = [offset + index for index in polygon.vertices]
                for index in range(1, len(corners) - 1):
                    triangle = [corners[0], corners[index], corners[index + 1]]
                    triangles.append(triangle)
                    owners.append((ordinal, polygon.index))
                    corners_world.append(tuple(vertices[corner].copy() for corner in triangle))
        finally:
            obj.evaluated_get(depsgraph).to_mesh_clear()
    if not triangles:
        return {"pairs": 0, "adjacentPairs": 0}, None

    # Connectivity, not object identity, is what distinguishes a fold from two
    # shells that merely touch. A joined model is one object with several shells;
    # a blanket "same object" rule counted every abutting face pair as a fold.
    parent = list(range(len(triangles)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first, second):
        first_root, second_root = root(first), root(second)
        if first_root != second_root:
            parent[second_root] = first_root

    first_triangle_at_vertex = {}
    for index, triangle in enumerate(triangles):
        for vertex in triangle:
            seen = first_triangle_at_vertex.get(vertex)
            if seen is None:
                first_triangle_at_vertex[vertex] = index
            else:
                union(seen, index)
    components = {}
    for index in range(len(triangles)):
        components.setdefault(root(index), []).append(index)
    component_tree = {}
    component_extent = {}
    for component, indices in components.items():
        component_tree[component] = BVHTree.FromPolygons(
            vertices, [triangles[index] for index in indices], all_triangles=True, epsilon=1e-6
        )
        component_points = [vertex for index in indices for vertex in corners_world[index]]
        if component_points:
            minimum = Vector(min(point[i] for point in component_points) for i in range(3))
            maximum = Vector(max(point[i] for point in component_points) for i in range(3))
            component_extent[component] = max((maximum - minimum).length, 1e-6)
        else:
            component_extent[component] = 1.0

    tree = BVHTree.FromPolygons(vertices, triangles, all_triangles=True, epsilon=1e-6)
    pairs = 0
    adjacent_checked = 0
    tested = 0
    truncated = False

    def inside(bvh, point, extent):
        """True when a point is inside by a three-axis parity vote.

        A first hit whose normal points along the ray means the ray left the
        solid; a first hit facing the ray means it entered. Two of three axes
        agreeing tolerates a ray grazing an edge without turning the result into
        a coin flip.
        """
        votes = 0
        for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)):
            direction = Vector(axis)
            location, normal, _, _ = bvh.ray_cast(point, direction, max(extent * 4.0, 1.0))
            if location is None or normal is None:
                return False
            if normal.dot(direction) > 0:
                votes += 1
        return votes >= 2

    def penetrates(triangle, other_tree, other_extent):
        first, second, third = triangle
        normal = (second - first).cross(third - first)
        if normal.length < 1e-12:
            return False
        normal.normalize()
        centre = (first + second + third) / 3.0
        # Only the side the triangle's own solid occupies. Testing +normal as
        # well turns two solids that merely share a face into an interpenetration,
        # because one solid's outside is the other's inside.
        epsilon = max(other_extent * 1e-4, 1e-6)
        if inside(other_tree, centre - normal * epsilon, other_extent):
            return True
        return any(inside(other_tree, point - normal * epsilon, other_extent) for point in triangle)

    for first, second in tree.overlap(tree):
        if first >= second:
            continue
        adjacent_checked += 1
        if set(triangles[first]) & set(triangles[second]):
            continue
        first_component, second_component = root(first), root(second)
        if first_component == second_component:
            # A non-adjacent pair inside one shell is a fold.
            pairs += 1
            continue
        tested += 1
        if tested > 50000:
            truncated = True
            break
        if penetrates(corners_world[first], component_tree[second_component], component_extent[second_component]) or penetrates(
            corners_world[second], component_tree[first_component], component_extent[first_component]
        ):
            pairs += 1
    return {"pairs": pairs, "adjacentPairs": adjacent_checked, "testedPairs": tested, "truncated": truncated}, None

def _wall_thickness(bpy, meshes, depsgraph):
    # pi-lens-ignore: ast-grep:unresolved-import-python
    import bmesh  # pyright: ignore[reportMissingImports]
    from mathutils.bvhtree import BVHTree  # pyright: ignore[reportMissingImports]

    thinnest = None
    sampled_faces = 0
    open_faces = 0
    other_shell_faces = 0
    for obj in meshes:
        bm = bmesh.new()
        try:
            mesh = _evaluated(obj, depsgraph)
            bm.from_mesh(mesh)
            bm.transform(obj.matrix_world)
            bm.normal_update()
            bm.faces.ensure_lookup_table()
            if not bm.faces:
                continue
            tree = BVHTree.FromBMesh(bm)
            extent = max((obj.matrix_world @ vertex.co).length for vertex in mesh.vertices) if mesh.vertices else 1.0
            parent = list(range(len(bm.faces)))

            def root(index, parent=parent):
                while parent[index] != index:
                    parent[index] = parent[parent[index]]
                    index = parent[index]
                return index

            for edge in bm.edges:
                faces = [face.index for face in edge.link_faces]
                for other in faces[1:]:
                    first_root, other_root = root(faces[0]), root(other)
                    if first_root != other_root:
                        parent[other_root] = first_root
            face_component = [root(index) for index in range(len(bm.faces))]
            step = max(1, len(bm.faces) // 300)
            for index, face in enumerate(bm.faces):
                if index % step:
                    continue
                if any(len(edge.link_faces) == 1 for edge in face.edges):
                    open_faces += 1
                    continue
                sampled_faces += 1
                origin = face.calc_center_median() - face.normal * 1e-5
                hit = tree.ray_cast(origin, -face.normal, max(extent * 2.0, 1.0))
                if hit[0] is None or hit[3] is None:
                    open_faces += 1
                    continue
                # A joined model has several shells. Hitting another shell says
                # nothing about this wall, and a crossing curve would otherwise
                # measure ~0 and block the character profile.
                if hit[2] is not None and face_component[hit[2]] != face_component[index]:
                    other_shell_faces += 1
                    continue
                distance = hit[3]
                if distance > 1e-7:
                    thinnest = distance if thinnest is None else min(thinnest, distance)
        finally:
            bm.free()
            obj.evaluated_get(depsgraph).to_mesh_clear()
    unit_scale = float(getattr(bpy.context.scene.unit_settings, "scale_length", 1.0) or 1.0)
    return {
        "distance": _round(thinnest * unit_scale) if thinnest is not None else None,
        "unitScale": _round(unit_scale, 4),
        "sampledFaces": sampled_faces,
        "openFaces": open_faces,
        "otherShellFaces": other_shell_faces,
    }, None


def _symmetry(bpy, meshes, axis_name, depsgraph):
    # pi-lens-ignore: ast-grep:unresolved-import-python
    import bmesh  # pyright: ignore[reportMissingImports]
    from mathutils.kdtree import KDTree  # pyright: ignore[reportMissingImports]

    axis = {"x": 0, "y": 1, "z": 2}.get(axis_name, 0)
    total = 0
    for obj in meshes:
        total += len(_evaluated(obj, depsgraph).vertices)
        obj.evaluated_get(depsgraph).to_mesh_clear()
    if total == 0:
        return {"rms": None, "max": None, "sampled": 0, "vertices": 0}, None
    # A KD-tree over a multi-million-vertex scene is itself the cost this guard
    # exists to bound. Sample while walking instead of materialising every
    # world-space vertex first.
    step = max(1, total // 20000)
    verts = []
    seen = 0
    for obj in meshes:
        bm = bmesh.new()
        try:
            bm.from_mesh(_evaluated(obj, depsgraph))
            matrix = obj.matrix_world
            for vertex in bm.verts:
                if seen % step == 0:
                    verts.append(matrix @ vertex.co)
                seen += 1
        finally:
            bm.free()
            obj.evaluated_get(depsgraph).to_mesh_clear()
    if not verts:
        return {"rms": None, "max": None, "sampled": 0, "vertices": 0}, None
    plane = (min(co[axis] for co in verts) + max(co[axis] for co in verts)) * 0.5
    tree = KDTree(len(verts))
    for index, co in enumerate(verts):
        tree.insert(co, index)
    tree.balance()
    stride = max(1, len(verts) // 3000)
    total_error = 0.0
    worst = 0.0
    measured = 0
    for index in range(0, len(verts), stride):
        co = verts[index].copy()
        co[axis] = 2.0 * plane - co[axis]
        _, _, distance = tree.find(co)
        if distance is None:
            continue
        total_error += distance * distance
        worst = max(worst, distance)
        measured += 1
    return {"rms": _round(math.sqrt(total_error / measured) if measured else 0.0), "max": _round(worst), "sampled": measured, "vertices": len(verts)}, None

def _uv(meshes, depsgraph):
    """UV data lives on the mesh, not the modifier result.

    An evaluated mesh can carry a generated UV layer the base data does not have,
    so measuring the modifier result here would report a UV-less asset as ready
    to paint.
    """
    missing = []
    for obj in meshes:
        layers = getattr(obj.data, "uv_layers", None)
        if not layers or len(layers) == 0:
            missing.append(obj.name)
    return missing


def _materials(bpy, meshes):
    procedural = []
    seen = set()
    for obj in meshes:
        for slot in obj.material_slots:
            material = slot.material
            if material is None or material.name in seen:
                continue
            seen.add(material.name)
            nodes = getattr(material, "node_tree", None)
            if nodes is None:
                continue
            if any(node.type not in {"OUTPUT_MATERIAL", "BSDF_PRINCIPLED", "TEX_IMAGE", "NORMAL_MAP", "UVMAP", "MAPPING"} for node in nodes.nodes):
                procedural.append(material.name)
    return procedural


def _surface_shader(nodes):
    """The Principled node that actually drives the surface, or None.

    Read through the Material Output link rather than by node type: a tree can
    keep a Principled node that a Mix Shader bypasses, and taking the first one
    of that type would call a painted surface default. A surface built from
    anything else is not a default material by this definition.
    """
    for node in nodes.nodes:
        if node.type != "OUTPUT_MATERIAL":
            continue
        socket = node.inputs.get("Surface")
        if socket is None or not socket.is_linked:
            return None
        source = socket.links[0].from_node
        return source if source.type == "BSDF_PRINCIPLED" else None
    return None


def _matches(component, expected, tolerance=DEFAULT_TOLERANCE):
    return abs(float(component) - expected) <= tolerance


def _is_default_material(material):
    """True when the material is still Blender's untouched Principled surface.

    Any custom Base Color, non-default Metallic or Roughness, or a link into one
    of PAINTED_SOCKETS means somebody painted it, so a hand-picked grey of 0.4
    reports as painted while the untouched 0.8 does not. The tolerance is per
    channel on the same 0..1 scale the socket holds.
    """
    nodes = getattr(material, "node_tree", None)
    if nodes is None:
        return False
    surface = _surface_shader(nodes)
    if surface is None:
        return False
    for name in PAINTED_SOCKETS:
        socket = surface.inputs.get(name)
        if socket is not None and socket.is_linked:
            return False
    base = surface.inputs.get("Base Color")
    if base is None or len(base.default_value) < 3:
        return False
    if not all(_matches(base.default_value[index], DEFAULT_BASE_COLOR[index]) for index in range(3)):
        return False
    metallic = surface.inputs.get("Metallic")
    if metallic is not None and not _matches(metallic.default_value, DEFAULT_METALLIC):
        return False
    roughness = surface.inputs.get("Roughness")
    if roughness is not None and not _matches(roughness.default_value, DEFAULT_ROUGHNESS):
        return False
    return True


def _material_presence(meshes):
    """Meshes with no material at all, and materials left on the default surface.

    Both are decided from the material data rather than from the render: a
    picture cannot separate a default grey from a grey somebody chose, and by
    the time a critic has looked at one the answer has already cost a look.
    A mesh with no slot, or whose every slot is empty, is reported once and its
    material is not also called default; the remaining materials are
    deduplicated by name, because a default shared by five meshes is one
    mistake, not five.
    """
    missing = []
    default = []
    seen = set()
    for obj in meshes:
        slots = list(getattr(obj, "material_slots", []) or [])
        if not slots or all(slot.material is None for slot in slots):
            missing.append(obj.name)
            continue
        for slot in slots:
            material = slot.material
            if material is None or material.name in seen:
                continue
            seen.add(material.name)
            if _is_default_material(material):
                default.append(material.name)
    return {"missing": missing, "default": default}


def validate(bpy, objects, profile="prop", symmetry_axis="x", added_objects=None, owned_objects=None):
    """Return the preflight verdict for the objects a module wrote.

    `objects` are datablock names, because that is what ownership carries and
    what survives a reload. The caller decides whether a blocking verdict stops
    the critic; this function only measures and applies profile thresholds.

    `added_objects` is what the run itself created, when the caller knows it;
    a duplicate is only this script's mistake if the copy was made now. Without
    it, `objects` is used as the candidate set — a numbered name already there
    is then reported as well, which is the wider, noisier reading of the same
    rule.
    """
    profile = str(profile or "prop")
    if profile not in PROFILES:
        raise ValueError(f"unknown preflight profile {profile!r}; expected one of {', '.join(PROFILES)}")
    critical = CRITICAL[profile]
    meshes = _mesh_objects(bpy, objects or [])
    depsgraph = bpy.context.evaluated_depsgraph_get()
    checks = {}
    blockers = []
    warnings = []

    def record(check_id, result, is_critical):
        checks[check_id] = result
        if result["status"] == "block":
            if is_critical:
                blockers.append(check_id)
            else:
                warnings.append(check_id)
        elif result["status"] in {"warn", "skipped"}:
            warnings.append(check_id)

    blockouts = _blockouts(bpy, objects)
    blockout_critical = "blockout" in critical
    record(
        "blockout",
        _check(
            "block" if blockouts and blockout_critical else "warn" if blockouts else "ok",
            {"objects": blockouts} if blockouts else None,
            method='obj["pi_blender_blockout"] on the datablocks owned by the module',
            reason=(
                "a generative blockout is a starting point; rebuild or retopologise it before the module ships it"
                if blockouts
                else None
            ),
        ),
        blockout_critical,
    )

    bounds = _bounds(bpy, meshes, depsgraph)
    flat = bool(meshes) and any(value <= 1e-6 for value in bounds["dimensions"])
    tiny = not meshes or all(value <= 1e-6 for value in bounds["dimensions"])
    record("bounds", _check("block" if tiny else "warn" if flat else "ok", bounds, "evaluated object bounds; unit scale reported", reason="no measurable extent" if tiny else "one dimension is flat" if flat else None), "bounds" in critical)
    record("empty", _check("block" if not meshes else "ok", {"objects": len(meshes)}, "mesh datablocks named by ownership"), "empty" in critical)

    candidates = objects if added_objects is None else added_objects
    duplicates = _duplicate_objects(bpy, candidates, owned_objects, objects)
    duplicate_critical = "duplicateObjects" in critical
    record(
        "duplicateObjects",
        _check(
            "block" if duplicates and duplicate_critical else "warn" if duplicates else "ok",
            {"duplicates": duplicates},
            "<base>.<NNN> names among the datablocks added by this run, where <base> is owned by the module or present in the file",
            reason=(
                "Blender renames a re-created datablock instead of replacing it, so the apply would merge both copies"
                if duplicates
                else None
            ),
        ),
        duplicate_critical,
    )

    scales = _unapplied_scale(meshes)
    record("unappliedScale", _check("block" if scales else "ok", scales or None, "object.scale compared with 1.0, tolerance 1e-4"), "unappliedScale" in critical)

    topology = _topology(bpy, meshes, depsgraph)
    non_manifold_status = "block" if topology["nonManifoldEdges"] else "ok"
    record("nonManifold", _check(non_manifold_status, topology, "BMEdge.is_manifold and boundary edge count"), "nonManifold" in critical or "boundary" in critical)
    if topology["boundaryEdges"] and "boundary" in critical:
        checks["nonManifold"]["status"] = "block"
        if "nonManifold" not in blockers:
            blockers.append("nonManifold")

    pairs, skipped = _self_intersections(bpy, meshes, topology["faces"], depsgraph)
    if skipped or pairs is None:
        record("selfIntersections", _check("warn", None, "BVHTree.overlap plus a three-axis inside vote for cross-object pairs", reason=skipped or "not measured"), False)
    else:
        status = "block" if pairs["pairs"] else "warn" if pairs["truncated"] else "ok"
        record("selfIntersections", _check(status, pairs, "BVHTree.overlap plus a three-axis inside vote for cross-object pairs; testedPairs is capped at 50000"), "selfIntersections" in critical)

    wall_limit = MIN_WALL_METRES.get(profile)
    if wall_limit is None:
        record("wallThickness", _check("notApplicable", None, "no profile limit; the scan was skipped"), False)
    else:
        wall, _ = _wall_thickness(bpy, meshes, depsgraph)
        # Distance is converted to metres above; the thresholds are metres.
        wall_status = "warn" if wall["distance"] is None else "block" if wall["distance"] < wall_limit else "ok"
        record("wallThickness", _check(wall_status, wall, "ray from sampled face centres along the inverted normal, ignoring other shells", limit=wall_limit), "wallThickness" in critical)

    symmetry, _ = _symmetry(bpy, meshes, symmetry_axis, depsgraph)
    record("symmetry", _check("warn" if symmetry["rms"] and symmetry["rms"] > 1e-3 else "ok", symmetry, "KD-tree nearest mirrored vertex about the bounds centre"), False)

    missing_uv = _uv(meshes, depsgraph)
    record("uv", _check("block" if missing_uv else "ok", {"missing": missing_uv} if missing_uv else {"missing": []}, "mesh.uv_layers on the named meshes"), "uv" in critical)

    procedural = _materials(bpy, meshes)
    record("materials", _check("warn" if procedural else "ok", {"procedural": procedural} if procedural else {"procedural": []}, "node types outside the glTF-safe set"), "materials" in critical)

    # Reported after `materials` because the two answer different questions:
    # that one asks whether the paint will survive export, these ask whether
    # there is any paint to export at all. Both are warnings everywhere except
    # paint, where a surface nobody authored is disqualifying rather than worth
    # a look.
    presence = _material_presence(meshes)
    record(
        "missingMaterial",
        _check(
            "block" if presence["missing"] and "missingMaterial" in critical else "warn" if presence["missing"] else "ok",
            {"objects": presence["missing"]},
            "material_slots on the named meshes; no slot, or every slot empty, counts as missing",
            reason=(
                "an unassigned mesh renders as the default surface, so nothing about its material was authored"
                if presence["missing"]
                else None
            ),
        ),
        "missingMaterial" in critical,
    )
    record(
        "defaultMaterial",
        _check(
            "block" if presence["default"] and "defaultMaterial" in critical else "warn" if presence["default"] else "ok",
            {"materials": presence["default"]},
            "the Principled surface linked to Material Output, with no links into " + ", ".join(PAINTED_SOCKETS),
            limit={"baseColor": list(DEFAULT_BASE_COLOR), "metallic": DEFAULT_METALLIC, "roughness": DEFAULT_ROUGHNESS, "tolerance": DEFAULT_TOLERANCE},
            reason=(
                "the material still has Blender's default Base Color, Metallic and Roughness, so it renders as an unpainted surface"
                if presence["default"]
                else None
            ),
        ),
        "defaultMaterial" in critical,
    )

    return {
        "profile": profile,
        "blocking": bool(blockers),
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
    }
