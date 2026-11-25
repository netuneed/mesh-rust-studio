"""Prepare GPU tables for a static, triangulated solid and convex cutter islands."""

from contextlib import ExitStack
from pathlib import Path

import bpy

import numpy as np

from . import (CUTTER_NODE, GROUP_MARKER, RENDER_SEAM_GROUP, SEAM_GROUP, BooleanGraph, cutter_of,
               find_modifier, input_control, operation_of)


_CAP = "r11_live_private_cap"
_OPERAND = ".boolean_plus_visual_operand"
_CLOSED = ".boolean_plus_visual_closed"
_INSTANCES = ".boolean_plus_visual_instances"
_MAX_TRIANGLES = 120_000
_MAX_CUTTER_ISLANDS = 1024
_STATIC_CAP_NODES = {
    "FunctionNodeCompare", "GeometryNodeAttributeStatistic", "GeometryNodeFillCurve",
    "GeometryNodeInputMeshEdgeNeighbors", "GeometryNodeInputMeshVertexNeighbors", "GeometryNodeInputNamedAttribute",
    "GeometryNodeInputPosition", "GeometryNodeJoinGeometry", "GeometryNodeMergeByDistance", "GeometryNodeMeshToCurve",
    "GeometryNodeSetPosition", "GeometryNodeStoreNamedAttribute", "GeometryNodeSwitch", "ShaderNodeCombineXYZ",
    "ShaderNodeMath", "ShaderNodeSeparateXYZ",
}


def _node(group, label):
    matches = [node for node in group.nodes if node.label == label]
    if len(matches) != 1:
        raise ValueError(f"Missing distinctive R11 preview node {label!r}.")
    return matches[0]


def _source(socket):
    if len(socket.links) != 1:
        raise ValueError("The visual preview requires one source per operand.")
    return socket.links[0].from_socket


def _face_attribute(group, geometry, name, data_type, value):
    node = group.nodes.new("GeometryNodeStoreNamedAttribute")
    node.data_type = data_type
    node.domain = "FACE"
    group.links.new(geometry, node.inputs["Geometry"])
    node.inputs["Name"].default_value = name
    if isinstance(value, bpy.types.NodeSocket):
        group.links.new(value, node.inputs["Value"])
    else:
        node.inputs["Value"].default_value = value
    return node.outputs["Geometry"]


def _check_fixed_cap(*outputs):
    pending = list(outputs)
    seen = set()
    while pending:
        socket = pending.pop()
        if socket.as_pointer() in seen:
            continue
        seen.add(socket.as_pointer())
        node = socket.node
        if node.bl_idname == "NodeGroupInput" and socket.name == "Geometry":
            continue
        if node.bl_idname not in _STATIC_CAP_NODES:
            raise ValueError("The visual preview requires a cap independent of the cutter's position.")
        pending.extend(link.from_socket for input_socket in node.inputs for link in input_socket.links)


def _check_exact_path(group, base, cutter, closure, output):
    fallback = _node(group, "Fallback keeps the chosen volume semantics")
    difference = _node(group, "Exact on closed volumes with overlapping cutter shells")
    if (fallback.bl_idname != "GeometryNodeSwitch" or fallback.input_type != "GEOMETRY" or fallback.mute or
            _source(output) != fallback.outputs[0] or _source(fallback.inputs["Switch"]) != closure or
            difference.bl_idname != "GeometryNodeMeshBoolean" or difference.mute or
            difference.operation != "DIFFERENCE" or difference.solver != "EXACT" or
            _source(difference.inputs["Mesh 1"]) != base):
        raise ValueError("The R11 Exact preview requires the guarded closed-volume output.")
    linked = _source(fallback.inputs["True"])
    for attribute in ("r11_live_private_weld", _CAP):
        remove = linked.node
        if (remove.bl_idname != "GeometryNodeRemoveAttribute" or remove.mute or
                remove.inputs["Name"].is_linked or remove.inputs["Name"].default_value != attribute):
            raise ValueError("The R11 Exact preview requires the original temporary cap cleanup.")
        linked = _source(remove.inputs["Geometry"])
    strip = linked.node
    if (strip.bl_idname != "GeometryNodeDeleteGeometry" or strip.domain != "FACE" or strip.mute or
            _source(strip.inputs["Geometry"]) != difference.outputs["Mesh"]):
        raise ValueError("The R11 Exact preview requires the original temporary cap cleanup.")
    cap = _source(strip.inputs["Selection"]).node
    if (cap.bl_idname != "GeometryNodeInputNamedAttribute" or cap.data_type != "BOOLEAN" or cap.mute or
            cap.inputs["Name"].is_linked or cap.inputs["Name"].default_value != _CAP):
        raise ValueError("The R11 Exact preview requires the marked temporary cap.")
    union = _source(difference.inputs["Mesh 2"]).node
    if (union.bl_idname != "GeometryNodeMeshBoolean" or union.operation != "UNION" or
            union.solver != "EXACT" or union.mute):
        raise ValueError("The R11 Exact preview requires the union of cutter shells.")
    split = _source(union.inputs["Mesh 2"]).node
    if (split.bl_idname != "GeometryNodeSplitToInstances" or split.domain != "FACE" or split.mute or
            _source(split.inputs["Geometry"]) != cutter or
            split.inputs["Selection"].is_linked or not split.inputs["Selection"].default_value):
        raise ValueError("The R11 Exact preview requires all connected shells from the current cutter.")
    island = _source(split.inputs["Group ID"])
    if island.node.bl_idname != "GeometryNodeInputMeshIsland" or island.name != "Island Index" or island.node.mute:
        raise ValueError("The R11 Exact preview requires the original cutter island split.")


def _mark_inputs(group, expected_cutter, *, exact=False):
    operation = _node(group, "MANIFOLD difference")
    if operation.bl_idname != "GeometryNodeMeshBoolean" or operation.operation != "DIFFERENCE" or operation.solver != "MANIFOLD":
        raise ValueError("The visual preview requires the R11 Manifold Difference inputs.")
    base = _source(operation.inputs["Mesh 1"])
    cutter = _source(operation.inputs["Mesh 2"])
    guard = _node(group, "Only use the closed planar path")
    object_info = cutter.node
    instance = object_info.inputs.get("As Instance")
    if (object_info.bl_idname != "GeometryNodeObjectInfo" or object_info.transform_space != "RELATIVE" or
            object_info.mute or cutter.name != "Geometry" or instance is None or
            instance.is_linked or instance.default_value):
        raise ValueError("The visual preview requires the evaluated cutter directly in target-local space.")
    object_socket = object_info.inputs["Object"]
    if object_socket.is_linked:
        source = _source(object_socket)
        if source.node.bl_idname != "NodeGroupInput" or source.name != "Object":
            raise ValueError("The visual preview requires the direct cutter input.")
    elif object_socket.default_value != expected_cutter:
        raise ValueError("The visual preview cutter has changed.")
    result = _node(group, "Live cut with original open seam").inputs["Geometry"]
    if exact:
        _check_exact_path(group, base, cutter, guard.outputs[0], result)
    _check_fixed_cap(base, guard.outputs[0])
    base = _face_attribute(group, base, _CLOSED, "BOOLEAN", guard.outputs[0])
    _join_probe_inputs(group, base, cutter, result)


def _mark_solid_inputs(group, expected_cutter):
    from .visual_reuse import _tree_values

    # Compare with the authored graph in the isolated worker. A hand-edited
    # Difference can change the relationship between its operands and result.
    reference = BooleanGraph()
    try:
        canonical = reference.create(expected_cutter, group[GROUP_MARKER])
        if not group.is_modifier or _tree_values(group)[1:] != _tree_values(canonical)[1:]:
            raise ValueError("The visual preview needs the standard Boolean Plus Difference graph.")
    finally:
        reference.discard()
    base = _node(group, "Boolean Plus inputs").outputs["Geometry"]
    cutter = group.nodes[CUTTER_NODE].outputs["Geometry"]
    output = _node(group, "Live Boolean result").inputs["Geometry"]
    _join_probe_inputs(group, base, cutter, output)


def _join_probe_inputs(group, base, cutter, result):
    base = _face_attribute(group, base, _OPERAND, "BOOLEAN", False)

    # The native solver can consume instances as independent operands. The GPU
    # snapshot uses only a realized mesh. Reject configurations with instances
    # instead of silently dropping any of the cutters.
    size = group.nodes.new("GeometryNodeAttributeDomainSize")
    size.component = "INSTANCES"
    group.links.new(cutter, size.inputs["Geometry"])
    cutter = _face_attribute(group, cutter, _INSTANCES, "INT", size.outputs["Instance Count"])
    cutter = _face_attribute(group, cutter, _OPERAND, "BOOLEAN", True)
    join = group.nodes.new("GeometryNodeJoinGeometry")
    group.links.new(base, join.inputs["Geometry"])
    group.links.new(cutter, join.inputs["Geometry"])
    for link in tuple(result.links):
        group.links.remove(link)
    group.links.new(join.outputs["Geometry"], result)


def _face_values(mesh, name, dtype):
    attribute = mesh.attributes.get(name)
    if attribute is None or attribute.domain != "FACE":
        raise ValueError(f"Missing face data for the visual preview: {name}.")
    result = np.empty(len(mesh.polygons), dtype=dtype)
    attribute.data.foreach_get("value", result)
    return result


def _compact(positions, triangles):
    indices, remapped = np.unique(triangles, return_inverse=True)
    return positions[indices], remapped.reshape((-1, 3)).astype("u4")


def _check_vertex_fans(mesh):
    import bmesh

    topology = bmesh.new()
    try:
        topology.from_mesh(mesh)
        if any(not vertex.is_manifold for vertex in topology.verts):
            raise ValueError("The visual preview requires closed inputs with manifold vertices.")
    finally:
        topology.free()


def _extract(mesh, *, planar=True):
    count = len(mesh.loop_triangles)
    if not 0 < count <= _MAX_TRIANGLES:
        raise ValueError("The two visual preview meshes exceed the triangle budget.")
    if not planar:
        _check_vertex_fans(mesh)
    positions = np.empty(len(mesh.vertices) * 3, dtype="f4")
    triangles = np.empty(count * 3, dtype="i4")
    faces = np.empty(count, dtype="i4")
    mesh.vertices.foreach_get("co", positions)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    mesh.loop_triangles.foreach_get("polygon_index", faces)
    positions = positions.reshape((-1, 3))
    triangles = triangles.reshape((-1, 3))
    if not np.all(np.isfinite(positions)):
        raise ValueError("The visual preview has non-finite coordinates.")
    cutter = _face_values(mesh, _OPERAND, "?")
    if not np.any(cutter) or np.all(cutter):
        raise ValueError("The visual preview requires both meshes.")
    if np.any(_face_values(mesh, _INSTANCES, "i4")[cutter]):
        raise ValueError("The visual preview requires realized cutters.")
    cap = np.zeros(count, dtype=bool)
    if planar:
        if not np.all(_face_values(mesh, _CLOSED, "?")[~cutter]):
            raise ValueError("The R11 planar closure did not qualify.")
        cap = _face_values(mesh, _CAP, "?")[faces]
    cutter = cutter[faces]
    if planar and (not np.any(cap & ~cutter) or not np.any(~cap & ~cutter)):
        raise ValueError("The R11 visual preview requires a separately marked cap.")
    target_positions, target_triangles = _compact(positions, triangles[~cutter])
    cutter_positions, cutter_triangles = _compact(positions, triangles[cutter])
    return target_positions, target_triangles, ~cap[~cutter], cutter_positions, cutter_triangles


def _rigid_array_modifiers(cutter):
    expected = {"Shape": "Line", "Count Method": "Count", "Transform Reference": "Inputs",
                "Realize Instances": True, "Randomize": False, "Merge": False}
    enabled = tuple(modifier for modifier in cutter.modifiers if modifier.show_viewport)
    for modifier in enabled:
        group = modifier.node_group if modifier.type == "NODES" else None
        # PreviewWorker has already authenticated this linked Essentials library.
        if (group is None or group.name != "Array" or group.library is None or
                Path(group.library.filepath).name != "geometry_nodes_essentials.blend"):
            raise ValueError("The visual preview requires the bundled Array cutter modifiers.")
        values = {}
        for item in group.interface.items_tree:
            if item.item_type != "SOCKET" or item.in_out != "INPUT":
                continue
            if item.name not in expected and item.socket_type != "NodeSocketObject":
                continue
            prop = getattr(modifier.properties.inputs, item.identifier)
            if getattr(prop, "use_attribute", False):
                raise ValueError("The visual preview requires fixed controls for the Array mode.")
            value = prop.value
            if item.socket_type == "NodeSocketObject" and value is not None:
                raise ValueError("The visual preview cannot move an Array that depends on another object.")
            values[item.name] = value
        if any(values.get(key) != value for key, value in expected.items()):
            raise ValueError("The visual preview requires realized linear Arrays with fixed input transforms.")
    return enabled


def _validate_rigid_stacks(target, cutter, boolean):
    enabled = [modifier for modifier in target.modifiers if modifier.show_viewport]
    if not enabled or enabled[-1] != boolean or any(modifier.type != "SUBSURF" for modifier in enabled[:-1]):
        raise ValueError("The visual preview needs Subdivision as the only modifier before Boolean Plus.")
    return _rigid_array_modifiers(cutter)


def requires_separated_cutters(modifier):
    return SEAM_GROUP not in modifier.node_group


def _capture(target, cutter, scene, settings):
    modifier = find_modifier(target)
    if (modifier is None or not modifier.show_viewport or operation_of(modifier) != "DIFFERENCE" or
            cutter_of(modifier) != cutter):
        raise ValueError("The visual preview requires an enabled Boolean Plus Difference.")
    _validate_rigid_stacks(target, cutter, modifier)
    group = modifier.node_group
    solver = input_control(modifier, "Solver")
    exact = solver is not None and solver.value == "Exact"
    planar = not requires_separated_cutters(modifier)
    seam = group.get(RENDER_SEAM_GROUP if exact else SEAM_GROUP) if planar else None
    if planar and (seam is None or group.get(RENDER_SEAM_GROUP) is None):
        raise ValueError("The visual preview requires the R11 closed-volume seam graphs.")

    layer = bpy.context.view_layer
    hidden = target.hide_get(view_layer=layer)
    with ExitStack() as cleanup:
        cleanup.callback(target.hide_set, hidden, view_layer=layer)
        probe_group = (seam if planar else group).copy()
        cleanup.callback(bpy.data.node_groups.remove, probe_group)
        if planar:
            _mark_inputs(probe_group, cutter, exact=exact)
        else:
            _mark_solid_inputs(probe_group, cutter)
        probe = target.copy()
        cleanup.callback(bpy.data.objects.remove, probe, do_unlink=True)
        probe_modifier = probe.modifiers[modifier.name]
        probe_modifier.node_group = probe_group
        for item in probe_group.interface.items_tree:
            if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == "Object":
                getattr(probe_modifier.properties.inputs, item.identifier).value = cutter
        scene.collection.objects.link(probe)
        # The private worker links both operands at the root. Hiding the source
        # still evaluates its Boolean, which the operand probe does not need.
        scene.collection.objects.unlink(target)
        cleanup.callback(scene.collection.objects.link, target)
        scene.frame_set(settings["frame"], subframe=settings["subframe"])
        graph = bpy.context.evaluated_depsgraph_get()
        evaluated = probe.evaluated_get(graph)
        world = evaluated.matrix_world.copy()
        origin = cutter.location.copy()
        if min(world.to_3x3().determinant(), cutter.evaluated_get(graph).matrix_world.to_3x3().determinant()) <= 0:
            raise ValueError("The visual preview requires positive non-singular transforms.")
        geometry = _extract(evaluated.data, planar=planar)
    return (*geometry, np.asarray(world, dtype="f4"), np.asarray(origin, dtype="f4"))


def write_snapshot(directory, target, cutter, scene, settings):
    """Capture the reduced viewport operands without modifying the snapshot target."""
    base, faces, visible, tools, tool_faces, matrix, origin = _capture(target, cutter, scene, settings)
    solid = requires_separated_cutters(find_modifier(target))
    target_bvh, target_triangles = build_bvh(base, faces, validate_solid=solid)
    pending = directory / "visual.pending"
    with pending.open("wb") as output:
        np.savez(
            output, target_positions=base, target_triangles=faces[visible], target_bvh=target_bvh,
            target_bvh_triangles=target_triangles, target_matrix_world=matrix,
            **_cutter_tables(tools, tool_faces, origin, len(cutter.data.vertices), separated=solid),
        )
    pending.replace(directory / "visual.npz")


def _cutter_tables(coords, corners, origin, source_vertices, *, separated=False):
    grid = cutter_grid(coords, corners, source_vertices, separated=separated)
    return {
        "cutter_positions": coords, "cutter_triangles": corners, "cutter_membership": grid["membership"],
        "cutter_grid": grid["grid"], "cutter_bounds": grid["bounds"], "cutter_planes": grid["planes"],
        "grid_origin": np.asarray(grid["origin"], dtype="f4"), "grid_inverse": np.asarray(grid["inverse"], dtype="f4"),
        "grid_side": np.uint32(grid["side"]), "epsilon": np.float32(grid["epsilon"]),
        "max_plane_adjustment": np.float32(grid["max_plane_adjustment"]), "initial_cutter_location": origin,
    }


def evaluated_cutter_tables(target, cutter, graph):
    """Read only the updated cutter from the viewport's current evaluation."""
    modifier = find_modifier(target)
    _validate_rigid_stacks(target, cutter, modifier)
    target_world = target.evaluated_get(graph).matrix_world
    evaluated = cutter.evaluated_get(graph)
    if min(target_world.to_3x3().determinant(), evaluated.matrix_world.to_3x3().determinant()) <= 0:
        raise ValueError("The visual preview requires positive non-singular transforms.")
    if not 0 < len(evaluated.data.loop_triangles) <= _MAX_TRIANGLES:
        raise ValueError("The visual cutter exceeds the triangle budget.")
    mesh = evaluated.data.copy()
    try:
        # Object Info transforms the mesh before tessellation, which can change
        # the diagonals of quads and n-gons.
        mesh.transform(target_world.inverted() @ evaluated.matrix_world)
        return cutter_mesh_tables(mesh, cutter.location, len(cutter.data.vertices),
                                  separated=requires_separated_cutters(modifier))
    finally:
        bpy.data.meshes.remove(mesh)


def _cutter_mesh_arrays(mesh):
    count = len(mesh.loop_triangles)
    if not 0 < count <= _MAX_TRIANGLES:
        raise ValueError("The visual cutter exceeds the triangle budget.")
    positions = np.empty(len(mesh.vertices) * 3, dtype="f4")
    triangles = np.empty(count * 3, dtype="i4")
    mesh.vertices.foreach_get("co", positions)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    coords, corners = _compact(positions.reshape((-1, 3)), triangles.reshape((-1, 3)))
    if not np.all(np.isfinite(coords)):
        raise ValueError("The visual cutter has non-finite coordinates.")
    return coords, corners


def cutter_mesh_tables(mesh, origin, source_vertices, *, separated=False):
    """Read an already transformed target-local cutter without creating data blocks."""
    coords, corners = _cutter_mesh_arrays(mesh)
    return _cutter_tables(coords, corners, np.asarray(origin, dtype="f4"), source_vertices, separated=separated)


def packed(values):
    coords = np.asarray(values, dtype="f4").reshape((-1, 3))
    result = np.zeros((len(coords), 4), dtype="f4")
    result[:, :3] = coords
    return result


def check_winding(triangles):
    edges = np.concatenate((triangles[:, :2], triangles[:, 1:], triangles[:, (2, 0)]))
    forward = edges[:, 0] < edges[:, 1]
    lo = np.minimum(edges[:, 0], edges[:, 1]).astype("u8")
    hi = np.maximum(edges[:, 0], edges[:, 1]).astype("u8")
    keys = (lo << 32) | hi
    _, groups, sizes = np.unique(keys, return_inverse=True, return_counts=True)
    if np.any(sizes != 2) or np.any(np.bincount(groups, weights=forward) != 1):
        raise ValueError("The visual preview requires consistently oriented, closed inputs.")


def _check_coplanar_faces(faces, normals, tolerance):
    # The native BVH overlap test skips coplanar triangles. Sweep boxes and
    # use separating axes for the surviving coplanar pairs. Contact at a shared
    # edge or vertex has no area and is allowed.
    lows, highs = faces.min(axis=1), faces.max(axis=1)
    axis = int(np.argmax(highs.max(axis=0) - lows.min(axis=0)))
    order = np.argsort(lows[:, axis], kind="stable")
    lows, highs, faces, normals = lows[order], highs[order], faces[order], normals[order]
    endpoints = np.searchsorted(lows[:, axis], highs[:, axis] + tolerance, side="right")
    if int(np.maximum(endpoints - np.arange(len(endpoints)) - 1, 0).sum()) > 3_000_000:
        raise ValueError("The closed preview exceeds its surface validation budget.")
    for index, end in enumerate(endpoints):
        following = slice(index + 1, end)
        box_test = np.all((lows[following] <= highs[index] + tolerance) &
                          (highs[following] >= lows[index] - tolerance), axis=1)
        candidates = faces[following][box_test]
        if not len(candidates):
            continue
        normal = normals[index]
        offset = candidates - faces[index, 0]
        coplanar = candidates[np.all(np.abs(offset @ normal) <= tolerance, axis=1)]
        if not len(coplanar):
            continue
        keep = np.arange(3) != np.argmax(np.abs(normal))
        element, others = faces[index][:, keep], coplanar[:, :, keep]
        first = np.broadcast_to(element, others.shape)
        edges = np.concatenate((np.roll(first, 1, axis=1) - first, np.roll(others, 1, axis=1) - others), axis=1)
        axes = edges[:, :, (1, 0)] * (-1, 1)
        lengths = np.linalg.norm(axes, axis=2)
        if np.any(lengths == 0):
            raise ValueError("The closed preview cannot resolve a degenerate planar target face.")
        axes /= lengths[:, :, None]
        a, b = first @ axes.swapaxes(1, 2), others @ axes.swapaxes(1, 2)
        overlap = np.minimum(a.max(axis=1), b.max(axis=1)) - np.maximum(a.min(axis=1), b.min(axis=1))
        if np.any(np.all(overlap > tolerance, axis=1)):
            raise ValueError("The closed preview cannot use overlapping coplanar target faces.")


def _validate_closed_target(coords, corners):
    from mathutils.bvhtree import BVHTree

    components = _island_ids(corners, len(coords))
    if np.any(components != components[0]) or len(np.unique(coords, axis=0)) != len(coords):
        raise ValueError("The closed preview requires one connected shell without duplicate vertices.")
    points = coords.astype("f8")
    points -= points.mean(axis=0)
    faces = points[corners]
    normals = np.cross(faces[:, 1] - faces[:, 0], faces[:, 2] - faces[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    if np.any(lengths == 0) or np.einsum("ij,ij->", faces[:, 0], normals) <= 0:
        raise ValueError("The closed preview requires nondegenerate faces and outward winding.")
    normals /= lengths[:, None]
    tree = BVHTree.FromPolygons(coords.tolist(), corners.tolist(), all_triangles=True)
    if tree.overlap(tree):
        raise ValueError("The closed preview cannot use a self-intersecting target.")
    tolerance = max(1e-10, float(np.ptp(points, axis=0).max()) * 1e-8)
    _check_coplanar_faces(faces, normals, tolerance)


def build_bvh(coords, corners, *, validate_solid=False):
    check_winding(corners)
    if validate_solid:
        _validate_closed_target(coords, corners)
    triangles = coords[corners]
    lows, highs = triangles.min(axis=1), triangles.max(axis=1)
    centers = (lows + highs) / 2
    nodes = []
    order = []

    def visit(ids, depth):
        slot = len(nodes) // 2
        low = lows[ids].min(axis=0)
        high = highs[ids].max(axis=0)
        nodes.extend([None, None])
        if depth >= 28:
            raise ValueError("The preview ray stack would exceed its capacity.")
        if len(ids) <= 8:
            nodes[slot * 2] = (*low, len(order))
            nodes[slot * 2 + 1] = (*high, len(ids))
            order.extend(ids)
            return slot
        axis = np.argmax(high - low)
        ids = ids[np.argsort(centers[ids, axis], kind="stable")]
        middle = len(ids) // 2
        left = visit(ids[:middle], depth + 1)
        right = visit(ids[middle:], depth + 1)
        nodes[slot * 2] = (*low, left)
        nodes[slot * 2 + 1] = (*high, -right)
        return slot

    visit(np.arange(len(corners)), 0)
    return np.asarray(nodes, dtype="f4"), packed(triangles[order])


def _island_ids(corners, vertex_count):
    roots = list(range(vertex_count))

    def root(vertex):
        while roots[vertex] != vertex:
            roots[vertex] = roots[roots[vertex]]
            vertex = roots[vertex]
        return vertex

    for a, b, c in corners.tolist():
        base = root(a)
        roots[root(b)] = base
        roots[root(c)] = base
    return np.asarray([root(index) for index in corners[:, 0]])


def _repeated_topology(corners, width, count):
    triangles, remainder = divmod(len(corners), count)
    if remainder or not 0 < triangles <= 512:
        return None
    local = corners.reshape(count, triangles, 3).astype("i4")
    local -= width * np.arange(count, dtype="i4")[:, None, None]
    template = local[0]
    if (template.min() < 0 or template.max() >= width or np.unique(template).size != width or
            not np.all(local == template)):
        return None
    ids = _island_ids(template, width)
    return template if np.all(ids == ids[0]) else None


def _repeated_planes(positions, template, epsilon):
    center = positions.mean(axis=1)[:, None, :]
    triangles = positions[:, template] - center[:, None, :, :]
    points = triangles[:, :, 0, :]
    normals = np.cross(triangles[:, :, 1, :] - points, triangles[:, :, 2, :] - points)
    lengths = np.linalg.norm(normals, axis=-1)
    if np.any(lengths < epsilon * epsilon):
        raise ValueError("Degenerate triangles in the cutter.")
    normals /= lengths[:, :, None]
    offsets = np.sum(normals * points, axis=-1)
    violations = ((positions - center) @ normals.swapaxes(-1, -2) - offsets[:, None, :]).max(axis=1)
    support_error = max(0.0, float(violations.max()))
    if support_error > 0.001:
        raise ValueError(f"Nonconvex or reversed cutter island: {support_error:.6g}")
    absolute_offsets = np.sum(normals * (points + center), axis=-1) + violations
    original = np.concatenate((normals, absolute_offsets[:, :, None]), axis=-1)
    quantized = np.round(np.concatenate((normals, offsets[:, :, None]), axis=-1), 5)
    order = np.lexsort(quantized.transpose((2, 0, 1))[::-1])
    sorted_planes = np.take_along_axis(quantized, order[:, :, None], axis=1)
    first = np.ones((len(positions), 1), dtype=bool)
    distinct = np.concatenate((first, (sorted_planes[:, 1:] != sorted_planes[:, :-1]).any(axis=-1)), axis=1)
    counts = distinct.sum(axis=1)
    if np.any(counts > 256):
        raise ValueError("Too many planes on one cutter island.")
    return np.take_along_axis(original, order[:, :, None], axis=1)[distinct], counts, support_error


def _repeated_cutters(coords, corners, width, epsilon):
    """Batch plane construction only for verified contiguous copies of one shell."""
    if not 0 < width <= 256:
        return None
    count, remainder = divmod(len(coords), width)
    if remainder or not 1 < count <= _MAX_CUTTER_ISLANDS:
        return None
    template = _repeated_topology(corners, width, count)
    if template is None:
        return None
    check_winding(template)
    # Limit the two float64 support matrices used to validate each batch.
    chunk_size = max(1, (4 * 1024 * 1024) // (width * len(template) * 8 * 2))
    positions = coords.reshape(count, width, 3).astype("f8")
    plane_chunks = []
    count_chunks = []
    support_error = 0.0
    for first in range(0, count, chunk_size):
        planes, counts, error = _repeated_planes(positions[first:first + chunk_size], template, epsilon)
        plane_chunks.append(planes)
        count_chunks.append(counts)
        support_error = max(support_error, error)
    lows = positions.min(axis=1) - epsilon
    highs = positions.max(axis=1) + epsilon
    counts = np.concatenate(count_chunks)
    membership = np.repeat(np.arange(count, dtype="f4"), width)
    metadata = np.empty((count * 2, 4), dtype="f8")
    metadata[::2, :3] = lows
    metadata[1::2, :3] = highs
    metadata[::2, 3] = np.cumsum(counts) - counts
    metadata[1::2, 3] = counts
    bounds = np.stack((lows, highs), axis=1)
    return membership, bounds, metadata, np.concatenate(plane_chunks), support_error


def cutter_grid(coords, corners, source_vertices=0, *, separated=False):
    epsilon = max(0.000003, float(np.ptp(coords, axis=0).max()) * 1.0e-6)
    repeated = _repeated_cutters(coords, corners, source_vertices, epsilon)
    if repeated is not None:
        membership, bounds, metadata, planes, support_error = repeated
        return _assemble_cutter_grid(membership, bounds, metadata, planes, epsilon, support_error, separated=separated)

    check_winding(corners)
    ids = _island_ids(corners, len(coords))
    bounds = []
    metadata = []
    planes = []
    membership = np.full(len(coords), -1.0, dtype="f4")
    support_error = 0.0
    components = np.unique(ids)
    if len(components) > _MAX_CUTTER_ISLANDS:
        raise ValueError("The visual preview has too many convex cutter islands.")
    for component in components:
        indices = corners[ids == component]
        vertices = np.unique(indices)
        if len(vertices) > 256 or len(indices) > 512:
            raise ValueError("A cutter island exceeds the visual preview complexity budget.")
        positions = coords[vertices].astype("f8")
        membership[vertices] = len(bounds)
        center = positions.mean(axis=0)
        triangles = coords[indices].astype("f8") - center
        points = triangles[:, 0, :]
        normals = np.cross(triangles[:, 1, :] - points, triangles[:, 2, :] - points)
        lengths = np.linalg.norm(normals, axis=1)
        if np.any(lengths < epsilon * epsilon):
            raise ValueError("Degenerate triangles in the cutter.")
        normals /= lengths[:, None]
        offsets = np.sum(normals * points, axis=1)
        violations = np.max((positions - center) @ normals.T - offsets, axis=0)
        support_error = max(support_error, float(violations.max()))
        if support_error > 0.001:
            raise ValueError(f"Nonconvex or reversed cutter island: {support_error:.6g}")
        d = np.sum(normals * (points + center), axis=1) + violations
        original = np.column_stack((normals, d))
        quantized = np.round(np.column_stack((normals, offsets)), 5)
        order = np.lexsort(quantized.T[::-1])
        sorted_planes = quantized[order]
        distinct = np.r_[True, (sorted_planes[1:] != sorted_planes[:-1]).any(axis=1)]
        unique = order[distinct]
        if len(unique) > 256:
            raise ValueError("Too many planes on one cutter island.")
        low = positions.min(axis=0) - epsilon
        high = positions.max(axis=0) + epsilon
        bounds.append((low, high))
        metadata.extend([(*low, len(planes)), (*high, len(unique))])
        planes.extend(original[unique])

    return _assemble_cutter_grid(membership, bounds, metadata, planes, epsilon, support_error, separated=separated)


def _require_separated(bounds):
    order = np.argsort(bounds[:, 0, 0], kind="stable")
    lows, highs = bounds[order, 0], bounds[order, 1]
    endpoints = np.searchsorted(lows[:, 0], highs[:, 0], side="right")
    for index, end in enumerate(endpoints):
        other = slice(index + 1, end)
        if np.any(np.all((lows[other, 1:] <= highs[index, 1:]) & (highs[other, 1:] >= lows[index, 1:]), axis=1)):
            raise ValueError("The closed preview requires separated cutter islands.")


def _assemble_cutter_grid(membership, bounds, metadata, planes, epsilon, support_error, *, separated):
    bounds = np.asarray(bounds, dtype="f8")
    if separated:
        _require_separated(bounds)
    low, high = bounds[:, 0].min(axis=0), bounds[:, 1].max(axis=0)
    side = 24
    cells = side ** 3
    inv_step = side / (high - low)
    starts = np.maximum(np.floor((bounds[:, 0] - low) * inv_step).astype("i4"), 0)
    ends = np.minimum(np.floor((bounds[:, 1] - low) * inv_step).astype("i4"), side - 1)
    widths = (ends - starts + 1).astype("i4")
    references = np.prod(widths, axis=1, dtype="i4")
    reference_count = int(references.sum())
    if reference_count > 250_000:
        raise ValueError("The cutter grid exceeds the visual preview footprint budget.")

    islands = np.repeat(np.arange(len(bounds), dtype="i4"), references)
    offsets = (references.cumsum(dtype="i4") - references)[islands]
    local = np.arange(reference_count, dtype="i4") - offsets
    width_x, width_y = widths[islands, :2].T
    x = starts[islands, 0] + local % width_x
    y = starts[islands, 1] + local // width_x % width_y
    z = starts[islands, 2] + local // (width_x * width_y)
    cell_ids = (z * side + y) * side + x
    sizes = np.bincount(cell_ids, minlength=cells).astype("i4")
    if sizes.max() > 64:
        raise ValueError("Too many cutters share one visual preview grid cell.")

    grid_table = np.zeros((cells + reference_count, 4), dtype="f4")
    grid_table[:cells, 0] = cells + np.cumsum(sizes) - sizes
    grid_table[:cells, 1] = sizes
    grid_table[cells:, 0] = islands[np.argsort(cell_ids, kind="stable")]
    return {
        "grid": grid_table,
        "bounds": np.asarray(metadata, dtype="f4"),
        "planes": np.asarray(planes, dtype="f4"),
        "origin": tuple(low), "inverse": tuple(inv_step), "side": side, "epsilon": epsilon,
        "islands": len(bounds), "max_plane_adjustment": support_error, "membership": membership,
    }
