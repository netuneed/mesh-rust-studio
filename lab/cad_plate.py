"""Generate a millimetre-sized test part from stock Blender 5.3 nodes."""

import math

import bpy
from mathutils import Euler


MODIFIER = "CAD-пластина"
PARAMETERS = (
    ("Длина", 0.080, 0.002, 1.0),
    ("Ширина", 0.050, 0.002, 1.0),
    ("Толщина", 0.008, 0.002, 1.0),
    ("Радиус", 0.008, 0.0001, 0.5),
    ("Фаска", 0.001, 0.0, 0.25),
)
DESCRIPTIONS = {"Радиус": "Радиус центрального сквозного отверстия",
                "Фаска": "Отступ фаски от наружного ребра"}
LIMIT_WARNINGS = ("Длина ограничена", "Ширина ограничена", "Толщина ограничена", "Радиус ограничен", "Фаска ограничена")
CYLINDER_VERTICES = 128
MIN_WALL = 0.0002


def make_plate_nodes():
    tree = bpy.data.node_groups.new(MODIFIER, 'GeometryNodeTree')
    tree.is_modifier = True
    out = tree.interface.new_socket(name="Пластина", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    for name, default, low, high in PARAMETERS:
        item = tree.interface.new_socket(name=name, in_out='INPUT', socket_type='NodeSocketFloat',
                                         description=DESCRIPTIONS.get(name, "Размер в миллиметрах"))
        item.subtype = 'DISTANCE'
        item.default_value, item.min_value, item.max_value = default, low, high
        item.force_non_field = True

    def node(type_name, label, x, y):
        item = tree.nodes.new(type_name)
        item.label = label
        item.location = (x, y)
        return item

    def assign(value, socket):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, socket)
        else:
            socket.default_value = value

    def calculate(operation, left, right, label, x, y):
        operation_node = node('ShaderNodeMath', label, x, y)
        operation_node.operation = operation
        assign(left, operation_node.inputs[0])
        assign(right, operation_node.inputs[1])
        return operation_node.outputs[0]

    group = node('NodeGroupInput', "Параметры детали", -1360, 400)
    output = node('NodeGroupOutput', "Готовая деталь", 1030, 400)

    def constrain(index, low, high, x, y):
        name = PARAMETERS[index][0]
        requested = group.outputs[name]
        clamp = node('ShaderNodeClamp', name, x, y)
        for key, value in [('Value', requested), ('Min', low), ('Max', high)]:
            assign(value, clamp.inputs[key])
        actual = clamp.outputs[0]
        compare = node('FunctionNodeCompare', name, x + 180, y - 220)
        compare.data_type = 'FLOAT'
        compare.operation = 'NOT_EQUAL'
        compare.inputs['Epsilon'].default_value = 1e-7
        assign(requested, compare.inputs[0])
        assign(actual, compare.inputs[1])
        warning = node('GeometryNodeWarning', name, x + 360, y - 220)
        warning.warning_type = 'WARNING'
        assign(compare.outputs[0], warning.inputs['Show'])
        warning.inputs['Message'].default_value = LIMIT_WARNINGS[index]
        return actual

    length, width, thickness = [constrain(index, low, high, -1100, 450 - index * 410)
                               for index, (_, _, low, high) in enumerate(PARAMETERS[:3])]
    footprint = calculate('MINIMUM', length, width, "Короткая сторона", -590, 0)
    thin_side = calculate('MINIMUM', footprint, thickness, "Минимальный размер", -400, 0)
    bevel_limit = calculate('MULTIPLY', thin_side, 0.25, "Предел фаски", -220, 0)
    bevel_width = constrain(4, 0.0, bevel_limit, -40, 0)

    half_side = calculate('MULTIPLY', footprint, 0.5, "Половина стороны", -590, -540)
    top_side = calculate('SUBTRACT', half_side, bevel_width, "Отступ от фаски", -400, -540)
    radius_limit = calculate('SUBTRACT', top_side, MIN_WALL, "Предел радиуса", -220, -540)
    hole_radius = constrain(3, PARAMETERS[3][2], radius_limit, -40, -540)

    size = node('ShaderNodeCombineXYZ', "Габариты", -580, 450)
    for index, dimension in enumerate((length, width, thickness)):
        assign(dimension, size.inputs[index])
    cube = node('GeometryNodeMeshCube', "Заготовка", -380, 450)
    assign(size.outputs[0], cube.inputs['Size'])

    bevel = node('GeometryNodeMeshBevel', "Фаска наружных рёбер", 360, 450)
    assign(cube.outputs['Mesh'], bevel.inputs['Mesh'])
    for name in ('Start Left Offset', 'Start Right Offset', 'End Left Offset', 'End Right Offset'):
        assign(bevel_width, bevel.inputs[name])

    cutting_depth = calculate('MULTIPLY', thickness, 2.0, "Проход через деталь", -220, -950)
    cylinder = node('GeometryNodeMeshCylinder', "Сквозное отверстие", 360, -350)
    cylinder.inputs['Vertices'].default_value = CYLINDER_VERTICES
    assign(hole_radius, cylinder.inputs['Radius'])
    assign(cutting_depth, cylinder.inputs['Depth'])
    difference = node('GeometryNodeMeshBoolean', "Вычесть отверстие", 740, 450)
    difference.operation = 'DIFFERENCE'
    difference.solver = 'EXACT'
    assign(bevel.outputs['Mesh'], difference.inputs['Mesh 1'])
    assign(cylinder.outputs['Mesh'], difference.inputs['Mesh 2'])
    assign(difference.outputs['Mesh'], output.inputs[out.name])
    return tree


def add_plate(context):
    """Add a parametric plate at the cursor in the current collection."""
    obj = bpy.data.objects.new("Пластина", bpy.data.meshes.new("Пластина"))
    context.collection.objects.link(obj)
    obj.location = context.scene.cursor.location
    for selected in context.selected_objects:
        selected.select_set(False)
    context.view_layer.objects.active = obj
    obj.select_set(True)
    modifier = obj.modifiers.new(name=MODIFIER, type='NODES')
    modifier.node_group = make_plate_nodes()
    return obj


def create_plate_scene():
    """Create the example in a fresh factory scene before saving it as .blend."""
    for obj in list(bpy.context.scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    units = bpy.context.scene.unit_settings
    units.system = 'METRIC'
    units.scale_length = 1.0
    units.length_unit = 'MILLIMETERS'

    obj = add_plate(bpy.context)
    for area in bpy.context.screen.areas:
        if area.type == 'PROPERTIES':
            area.spaces.active.context = 'MODIFIER'
        if area.type == 'VIEW_3D':
            space = area.spaces.active
            space.clip_start = 0.001
            space.overlay.show_overlays = False
            space.region_3d.view_location = (0, 0, 0)
            space.region_3d.view_distance = 0.14
            space.region_3d.view_rotation = Euler((math.radians(46), 0, math.radians(25))).to_quaternion()
            space.region_3d.view_perspective = 'ORTHO'
    return obj
