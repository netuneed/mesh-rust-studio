"""Identify unchanged GPU preview operands between rigid cutter movements."""

from array import array
from dataclasses import dataclass
from hashlib import blake2b

import bpy

from . import CONTROLS_MARKER, GROUP_MARKER, RENDER_SEAM_GROUP, SEAM_GROUP, cutter_of, find_modifier, operation_of


_NODE_SETTINGS = (
    "operation", "data_type", "domain", "input_type", "mode", "transform_space", "solver",
    "integer", "boolean", "use_clamp", "mapping", "axis", "component", "space", "is_active_output",
)
_MAX_BASE_CORNERS = 360_000
_MESH_ATTRIBUTES = {
    "FLOAT": ("value", "f", 1), "INT": ("value", "i", 1), "INT8": ("value", "i", 1),
    "INT32_2D": ("value", "i", 2), "BOOLEAN": ("value", "b", 1), "FLOAT2": ("vector", "f", 2),
    "FLOAT_VECTOR": ("vector", "f", 3), "FLOAT_COLOR": ("color", "f", 4), "BYTE_COLOR": ("color", "f", 4),
    "QUATERNION": ("value", "f", 4), "FLOAT4X4": ("value", "f", 16),
}


@dataclass(frozen=True)
class OperandInputs:
    settings: tuple
    mesh: tuple
    watched: frozenset

    def changed_mesh(self, mesh):
        uid, signature = self.mesh
        return uid == mesh.session_uid and signature != _mesh_values(mesh)[1]


@dataclass(frozen=True)
class VisualInputs:
    surface: OperandInputs
    cutter: OperandInputs

    @property
    def watched(self):
        return self.surface.watched | self.cutter.watched

    def changed_mesh(self, mesh):
        return self.surface.changed_mesh(mesh) or self.cutter.changed_mesh(mesh)


def _value(item):
    if isinstance(item, bpy.types.ID):
        return item.session_uid
    if item is None or isinstance(item, (bool, str, int, float)):
        return item
    return tuple(map(_value, item))


def modifier_values(obj):
    values = []
    for modifier in obj.modifiers:
        entry = [modifier.name, modifier.type, modifier.show_viewport]
        if modifier.type == "NODES" and modifier.node_group is not None:
            entry.append(modifier.node_group.session_uid)
            for socket in modifier.node_group.interface.items_tree:
                if socket.item_type == "SOCKET" and socket.in_out == "INPUT":
                    prop = getattr(modifier.properties.inputs, socket.identifier, None)
                    if prop is not None and hasattr(prop, "value"):
                        entry.append((socket.identifier, _value(prop.value),
                                      getattr(prop, "attribute_name", None), getattr(prop, "use_attribute", None)))
        elif modifier.type == "SUBSURF":
            entry.extend((modifier.levels, modifier.subdivision_type, modifier.quality, modifier.use_limit_surface,
                          modifier.use_creases, modifier.boundary_smooth))
        values.append(tuple(entry))
    return tuple(values)


def _mesh_values(mesh):
    if mesh.shape_keys is not None or len(mesh.loops) > _MAX_BASE_CORNERS:
        raise ValueError("GPU cache needs bounded base meshes without shape keys.")
    data = blake2b(digest_size=32)

    def add(collection, prop, kind, width=1):
        buffer = array(kind, [0]) * (len(collection) * width)
        if buffer:
            collection.foreach_get(prop, buffer)
        data.update(len(buffer).to_bytes(8, "little"))
        data.update(buffer)

    add(mesh.vertices, "co", "f", 3)
    add(mesh.edges, "vertices", "i", 2)
    add(mesh.loops, "vertex_index", "i")
    add(mesh.polygons, "loop_total", "i")
    for attribute in mesh.attributes:
        if attribute.name in {"position", ".edge_verts", ".corner_vert", ".corner_edge"}:
            continue
        if attribute.name.startswith(("r11_live_private_", ".boolean_plus_visual_")):
            raise ValueError("The source mesh already contains a reserved preview attribute.")
        field = _MESH_ATTRIBUTES.get(attribute.data_type)
        if field is None:
            raise ValueError("GPU cache cannot compare this mesh attribute type.")
        data.update(f"{attribute.name}\0{attribute.domain}\0{attribute.data_type}\0".encode())
        add(attribute.data, *field)
    for material in mesh.materials:
        data.update((material.session_uid if material else 0).to_bytes(8, "little"))
    return mesh.session_uid, data.digest()


def _trees(obj):
    pending = [modifier.node_group for modifier in obj.modifiers
               if modifier.show_viewport and modifier.type == "NODES" and modifier.node_group is not None]
    seen = {}
    while pending:
        group = pending.pop()
        if group.session_uid in seen:
            continue
        seen[group.session_uid] = group
        if group.library is None:
            pending.extend(node.node_tree for node in group.nodes
                           if node.type == "GROUP" and node.node_tree is not None)
            for marker in (SEAM_GROUP, RENDER_SEAM_GROUP):
                if isinstance(seam := group.get(marker), bpy.types.NodeTree):
                    pending.append(seam)
    return tuple(seen[uid] for uid in sorted(seen))


def _tree_values(tree):
    if tree.library is not None:
        return tree.session_uid, str(tree.library.filepath)
    nodes = tuple((node.name, node.label, node.bl_idname, node.mute,
                   tuple(_value(getattr(node, prop, None)) for prop in _NODE_SETTINGS),
                   tuple(item.name for item in getattr(node, "enum_items", ())),
                   tuple((socket.identifier, _value(socket.default_value)) for socket in node.inputs
                         if not socket.is_linked and hasattr(socket, "default_value")),
                   tuple((socket.identifier, _value(socket.default_value)) for socket in node.outputs
                         if hasattr(socket, "default_value"))) for node in tree.nodes)
    links = tuple((link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier,
                   link.is_muted, link.multi_input_sort_id) for link in tree.links)
    markers = (tree.get(GROUP_MARKER), tuple(sorted(tree.get(CONTROLS_MARKER, {}).items())),
               _value(tree.get(SEAM_GROUP)), _value(tree.get(RENDER_SEAM_GROUP)))
    return tree.session_uid, nodes, links, markers


def visual_inputs(target, cutter, scene, level):
    """Keep the fixed shape and linear cutter transform, omitting only its translation."""
    if (scene.render.use_simplify or target.parent is not None or target.constraints or
            cutter.parent is not None or cutter.constraints or cutter.animation_data is not None):
        raise ValueError("GPU cache requires the static R11 operand stack.")
    modifier = find_modifier(target)
    if modifier is None or not modifier.show_viewport or cutter_of(modifier) != cutter:
        raise ValueError("GPU cache requires an enabled Boolean Plus modifier with this cutter.")
    surface_trees = _trees(target)
    cutter_trees = _trees(cutter)
    animation = target.animation_data
    if animation is not None and (animation.drivers or animation.nla_tracks):
        raise ValueError("GPU cache does not reuse driven or NLA operands.")
    surface_watched = {tree.session_uid for tree in surface_trees}
    if animation is not None and animation.action is not None:
        surface_watched.add(animation.action.session_uid)
    surface = (target.session_uid, cutter.session_uid, scene.session_uid, scene.frame_current, scene.frame_subframe,
               scene.render.fps, scene.render.fps_base, level, modifier.name, operation_of(modifier),
               animation.action_slot_handle if animation else None,
               tuple(tuple(row) for row in target.matrix_world), modifier_values(target),
               tuple(_tree_values(tree) for tree in surface_trees))
    tool = (tuple(tuple(row) for row in cutter.matrix_world.to_3x3()), tuple(cutter.delta_location),
            modifier_values(cutter), tuple(_tree_values(tree) for tree in cutter_trees))
    return VisualInputs(OperandInputs(surface, _mesh_values(target.data), frozenset(surface_watched)),
                        OperandInputs(tool, _mesh_values(cutter.data),
                                      frozenset(tree.session_uid for tree in cutter_trees)))
