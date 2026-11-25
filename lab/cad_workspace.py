"""Optional CAD workspace and a reusable Blender 5.3 Geometry Nodes modifier."""

from math import radians
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from mathutils import Euler

from cad_plate import add_plate


WORKSPACE = "CAD"
WORKSPACE_LIBRARY = Path(__file__).with_name("cad-workspace.blend")
HOLE_GROUP = "CAD - Отверстие"
HOLE_PARAMETERS = (
    ("Радиус", 0.004, "Радиус цилиндрического резака"),
    ("Глубина", 0.030, "Полная высота резака вдоль локальной оси Z"),
    ("Центр X", 0.0, "Координата центра в системе объекта"),
    ("Центр Y", 0.0, "Координата центра в системе объекта"),
    ("Центр Z", 0.0, "Середина резака по локальной оси Z"),
)
CYLINDER_VERTICES = 128
_HOLE_GROUP_TAG = "blender_rust_cad_hole"


def prepare_workspace():
    """Customize the Modeling tab in the library's factory scene."""
    workspace = bpy.data.workspaces['Modeling']
    workspace.name = WORKSPACE
    workspace.object_mode = 'OBJECT'
    workspace.use_pin_scene = False
    for screen in workspace.screens:
        for area in screen.areas:
            if area.type == 'PROPERTIES':
                area.spaces.active.context = 'MODIFIER'
            if area.type == 'VIEW_3D':
                view = area.spaces.active
                view.clip_start = 0.001
                view.overlay.show_overlays = False
                view.region_3d.view_location = (0, 0, 0)
                view.region_3d.view_distance = 0.17
                view.region_3d.view_rotation = Euler((radians(32), 0, radians(20))).to_quaternion()
                view.region_3d.view_perspective = 'ORTHO'
    return workspace


def write_workspace_library(path):
    """Export the workspace from a fresh factory profile for the Dev package."""
    workspace = prepare_workspace()
    for screen in workspace.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                overlay = area.spaces.active.overlay
                overlay.show_overlays = True
                overlay.show_extras = False
                overlay.show_floor = False
                overlay.show_axis_x = overlay.show_axis_y = False
    bpy.data.libraries.write(str(path), {workspace}, compress=True, fake_user=True)


def ensure_workspace():
    """Append CAD to the current file while keeping its active workspace and scene."""
    existing = bpy.data.workspaces.get(WORKSPACE)
    if existing is not None:
        return existing

    screen = bpy.context.screen
    source_view = next(
        (area.spaces.active for area in screen.areas if area.type == 'VIEW_3D'),
        None,
    ) if screen else None
    with bpy.data.libraries.load(str(WORKSPACE_LIBRARY), link=False) as (available, appended):
        if WORKSPACE not in available.workspaces:
            raise RuntimeError(f"Missing {WORKSPACE} workspace in {WORKSPACE_LIBRARY}")
        appended.workspaces = [WORKSPACE]
    workspace = appended.workspaces[0]
    if workspace is None:
        raise RuntimeError(f"Could not append {WORKSPACE} from {WORKSPACE_LIBRARY}")

    if source_view:
        for workspace_screen in workspace.screens:
            for area in workspace_screen.areas:
                if area.type == 'VIEW_3D':
                    target = area.spaces.active.region_3d
                    source = source_view.region_3d
                    target.view_location = source.view_location
                    target.view_distance = source.view_distance
                    target.view_rotation = source.view_rotation
                    if source.view_perspective in {'ORTHO', 'PERSP'}:
                        target.view_perspective = source.view_perspective
    return workspace


def create_hole_group():
    tree = bpy.data.node_groups.new(HOLE_GROUP, 'GeometryNodeTree')
    tree[_HOLE_GROUP_TAG] = True
    tree.is_modifier = True
    tree.asset_mark()
    tree.asset_data.description = "Цилиндрическое отверстие в замкнутом меше вдоль локальной Z"
    for direction in ('INPUT', 'OUTPUT'):
        tree.interface.new_socket(name='Geometry', in_out=direction, socket_type='NodeSocketGeometry')
    for index, (name, default, description) in enumerate(HOLE_PARAMETERS):
        socket = tree.interface.new_socket(name=name, in_out='INPUT', socket_type='NodeSocketFloat',
                                           description=description)
        socket.subtype = 'DISTANCE'
        socket.default_value = default
        socket.force_non_field = True
        if index < 2:
            socket.min_value = 0

    def node(type_name, label, x, y):
        item = tree.nodes.new(type_name)
        item.label = label
        item.location = x, y
        return item

    def is_positive(source, x, y):
        compare = node('ShaderNodeMath', source.name, x, y)
        compare.operation = 'GREATER_THAN'
        tree.links.new(source, compare.inputs[0])
        compare.inputs[1].default_value = 0.0
        return compare.outputs[0]

    inputs = node('NodeGroupInput', 'Параметры слоя', -840, 220).outputs
    output = node('NodeGroupOutput', 'Обработанная деталь', 660, 220)
    cylinder = node('GeometryNodeMeshCylinder', 'Резак', -580, 0)
    cylinder.inputs['Vertices'].default_value = CYLINDER_VERTICES
    tree.links.new(inputs['Радиус'], cylinder.inputs['Radius'])
    tree.links.new(inputs['Глубина'], cylinder.inputs['Depth'])

    center = node('ShaderNodeCombineXYZ', 'Центр резака', -580, -400)
    for axis in 'XYZ':
        tree.links.new(inputs[f'Центр {axis}'], center.inputs[axis])
    place = node('GeometryNodeTransform', 'Позиция отверстия', -320, 0)
    tree.links.new(center.outputs['Vector'], place.inputs['Translation'])
    tree.links.new(cylinder.outputs['Mesh'], place.inputs['Geometry'])

    boolean = node('GeometryNodeMeshBoolean', 'Вычитание', -60, 220)
    boolean.operation = 'DIFFERENCE'
    boolean.solver = 'EXACT'
    tree.links.new(inputs['Geometry'], boolean.inputs['Mesh 1'])
    tree.links.new(place.outputs['Geometry'], boolean.inputs['Mesh 2'])

    positive_size = node('FunctionNodeBooleanMath', 'Резак включён', -60, -240)
    positive_size.operation = 'AND'
    tree.links.new(is_positive(inputs['Радиус'], -320, -400), positive_size.inputs[0])
    tree.links.new(is_positive(inputs['Глубина'], -320, -580), positive_size.inputs[1])
    cut = node('GeometryNodeSwitch', 'Геометрия', 190, 220)
    cut.input_type = 'GEOMETRY'
    tree.links.new(positive_size.outputs[0], cut.inputs['Switch'])
    tree.links.new(inputs['Geometry'], cut.inputs['False'])
    tree.links.new(boolean.outputs['Mesh'], cut.inputs['True'])
    tree.links.new(cut.outputs[0], output.inputs['Geometry'])
    return tree


def show_modifier_result(context):
    if context.screen:
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.spaces.active.context = 'MODIFIER'
    if context.area and context.area.type == 'VIEW_3D':
        region = next(region for region in context.area.regions if region.type == 'WINDOW')
        with context.temp_override(region=region):
            bpy.ops.view3d.view_selected()


class VIEW3D_OT_rust_cad_add_plate(bpy.types.Operator):
    bl_idname = "view3d.rust_cad_add_plate"
    bl_label = "Добавить пластину"
    bl_description = "Создать параметрическую пластину в позиции 3D-курсора"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        collection = context.collection
        return context.mode == 'OBJECT' and collection is not None and collection.is_editable

    def execute(self, context):
        add_plate(context)
        show_modifier_result(context)
        return {'FINISHED'}


class VIEW3D_OT_rust_cad_add_hole(bpy.types.Operator):
    bl_idname = "view3d.rust_cad_add_hole"
    bl_label = "Добавить отверстие"
    bl_description = "Добавить вырез в позиции 3D-курсора вдоль локальной оси Z меша"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return context.mode == 'OBJECT' and obj is not None and obj.type == 'MESH' and obj.is_editable

    def execute(self, context):
        group = next(
            (tree for tree in bpy.data.node_groups if tree.get(_HOLE_GROUP_TAG) and tree.is_editable),
            None,
        )
        if group is None:
            group = create_hole_group()
        obj = context.active_object
        modifier = obj.modifiers.new(HOLE_GROUP, 'NODES')
        modifier.node_group = group
        center = obj.matrix_world.inverted_safe() @ context.scene.cursor.location
        values = dict(zip(("Центр X", "Центр Y", "Центр Z"), center, strict=True))
        for socket in group.interface.items_tree:
            if socket.item_type == 'SOCKET' and socket.in_out == 'INPUT' and socket.name in values:
                getattr(modifier.properties.inputs, socket.identifier).value = values[socket.name]
        show_modifier_result(context)
        return {'FINISHED'}


class VIEW3D_MT_rust_cad(bpy.types.Menu):
    bl_label = "CAD"

    def draw(self, context):
        self.layout.operator(VIEW3D_OT_rust_cad_add_plate.bl_idname, icon='MESH_CUBE')
        self.layout.operator(VIEW3D_OT_rust_cad_add_hole.bl_idname, icon='MOD_BOOLEAN')


def draw_cad_menu(self, context):
    if context.workspace and context.workspace.name == WORKSPACE:
        self.layout.menu(VIEW3D_MT_rust_cad.__name__)


@persistent
def _on_load(_filepath):
    ensure_workspace()


def _install_workspace():
    ensure_workspace()
    return None


_CLASSES = (VIEW3D_OT_rust_cad_add_plate, VIEW3D_OT_rust_cad_add_hole, VIEW3D_MT_rust_cad)


def register():
    if _on_load in bpy.app.handlers.load_post:
        return
    for item in _CLASSES:
        bpy.utils.register_class(item)
    bpy.types.VIEW3D_MT_editor_menus.append(draw_cad_menu)
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.timers.register(_install_workspace, first_interval=0)


def unregister():
    if _on_load not in bpy.app.handlers.load_post:
        return
    if bpy.app.timers.is_registered(_install_workspace):
        bpy.app.timers.unregister(_install_workspace)
    bpy.app.handlers.load_post.remove(_on_load)
    bpy.types.VIEW3D_MT_editor_menus.remove(draw_cad_menu)
    for item in reversed(_CLASSES):
        bpy.utils.unregister_class(item)
