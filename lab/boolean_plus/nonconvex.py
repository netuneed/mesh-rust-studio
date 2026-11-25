"""Build bounded per-island BVHs for staged nonconvex cuts."""

import numpy as np

from .visual_geometry import (_MAX_CUTTER_ISLANDS, _MAX_TRIANGLES, _assemble_cutter_grid,
                              _island_ids, _repeated_topology, build_bvh, packed)


_MAX_UNSHARED_ISLANDS = 16


def _check_vertex_fans(corners, width):
    fans = [{} for _ in range(width)]
    for a, b, c in corners.tolist():
        fans[a][b] = c
        fans[b][c] = a
        fans[c][a] = b
    for fan in fans:
        first = next(iter(fan))
        following = first
        for count in range(1, len(fan) + 1):
            following = fan[following]
            if following == first:
                break
        if count != len(fan) or following != first:
            raise ValueError("The nonconvex preview requires connected vertex fans.")


def _validated_tree(points, corners):
    tree = build_bvh(points, corners, validate_solid=True)
    _check_vertex_fans(corners, len(points))
    return tree


def _shared_translations(coords, corners, width, epsilon):
    if not 0 < width <= 256:
        return None
    count, remainder = divmod(len(coords), width)
    if remainder or not 1 < count <= _MAX_CUTTER_ISLANDS:
        return None
    template = _repeated_topology(corners, width, count)
    if template is None:
        return None
    positions = coords.reshape(count, width, 3).astype("f8")
    offsets = positions[:, 0].copy()
    local = positions - offsets[:, None]
    # Only reuse an identical translated shell to within the shader's
    # boundary tolerance. This excludes Array rotations and deformations.
    residual = local - local[0]
    if np.max(np.einsum("ijk,ijk->ij", residual, residual)) > (epsilon * 0.25) ** 2:
        return None
    tree, triangles = _validated_tree(local[0].astype("f4"), template)
    bounds = np.stack((positions.min(axis=1), positions.max(axis=1)), axis=1)
    return (bounds, np.repeat(np.arange(count, dtype="f4"), width), packed(offsets),
            np.zeros(count, dtype="f4"), tree, triangles)


def _individual_trees(coords, corners):
    ids = _island_ids(corners, len(coords))
    components = np.unique(ids)
    if not 0 < len(components) <= _MAX_UNSHARED_ISLANDS:
        raise ValueError("The nonconvex preview exceeds its independent cutter budget.")

    bounds, offsets, roots, nodes, triangle_chunks = [], [], [], [], []
    membership = np.full(len(coords), -1.0, dtype="f4")
    node_offset = triangle_offset = 0
    for index, component in enumerate(components):
        faces = corners[ids == component]
        vertices, local_faces = np.unique(faces, return_inverse=True)
        if len(vertices) > 256 or len(faces) > 512:
            raise ValueError("A cutter island exceeds the nonconvex preview complexity budget.")
        points = coords[vertices].astype("f8")
        offset = points[0]
        local = (points - offset).astype("f4")
        tree, triangles = _validated_tree(local, local_faces.reshape((-1, 3)))
        membership[vertices] = index
        bounds.append((points.min(axis=0), points.max(axis=0)))
        offsets.append(offset)
        roots.append(node_offset)
        if node_offset:
            leaves = tree[1::2, 3] > 0
            tree[::2, 3] += np.where(leaves, triangle_offset, node_offset)
            tree[1::2, 3] -= np.where(leaves, 0, node_offset)
        nodes.append(tree)
        triangle_chunks.append(triangles)
        node_offset += len(tree) // 2
        triangle_offset += len(triangles) // 3

    return (np.asarray(bounds, dtype="f8"), membership, packed(offsets), np.asarray(roots, dtype="f4"),
            np.concatenate(nodes), np.concatenate(triangle_chunks))


def nonconvex_cutter_tables(coords, corners, origin, source_vertices, *, separated=False):
    """Pack closed cutter shells and one spatial grid, with a BVH for each distinct shell."""
    if not 0 < len(corners) <= _MAX_TRIANGLES or not np.all(np.isfinite(coords)):
        raise ValueError("The nonconvex cutter has unsupported geometry.")
    epsilon = max(3.0e-6, float(np.ptp(coords.astype("f8"), axis=0).max()) * 1.0e-6)
    trees = _shared_translations(coords, corners, source_vertices, epsilon)
    if trees is None:
        trees = _individual_trees(coords, corners)
    bounds, membership, offsets, roots, nodes, triangles = trees
    bounds[:, 0] -= epsilon
    bounds[:, 1] += epsilon
    metadata = np.empty((len(bounds) * 2, 4), dtype="f4")
    metadata[::2, :3], metadata[1::2, :3] = bounds[:, 0], bounds[:, 1]
    metadata[::2, 3], metadata[1::2, 3] = roots, -1
    planes = np.zeros((1, 4), dtype="f4")  # Retain a valid sampler for the shared shader.
    grid = _assemble_cutter_grid(membership, bounds, metadata, planes, epsilon, 0.0, separated=separated)
    return {
        "cutter_positions": coords, "cutter_triangles": corners, "cutter_membership": grid["membership"],
        "cutter_grid": grid["grid"], "cutter_bounds": grid["bounds"], "cutter_planes": grid["planes"],
        "grid_origin": np.asarray(grid["origin"], dtype="f4"), "grid_inverse": np.asarray(grid["inverse"], dtype="f4"),
        "grid_side": np.uint32(grid["side"]), "epsilon": np.float32(epsilon),
        "initial_cutter_location": np.asarray(origin, dtype="f4"),
        "cutter_offsets": offsets, "cutter_bvh": nodes, "cutter_ray_triangles": triangles,
    }
