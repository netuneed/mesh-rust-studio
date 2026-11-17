"""Portable automatic angle-seam UV modifier for Blender 5.3."""

from math import radians

import bpy


ASSET_NAME = 'Auto UV'
DEFAULT_MAP = 'Auto UV'
CATALOG_ID = '25805c48-34ec-4434-aaef-1f13a71c0169'


def create_auto_uv():
    tree = bpy.data.node_groups.new(ASSET_NAME, 'GeometryNodeTree')
    tree.is_modifier = True
    tree.asset_mark()
    tree.asset_data.catalog_id = CATALOG_ID
    tree.asset_data.description = 'Unwrap evaluated mesh using marked seams and optional angle cuts'

    for direction in ('INPUT', 'OUTPUT'):
        tree.interface.new_socket(name='Geometry', in_out=direction, socket_type='NodeSocketGeometry')

    def parameter(name, socket_type, default, description):
        socket = tree.interface.new_socket(name=name, in_out='INPUT', socket_type=socket_type,
                                           description=description)
        socket.default_value = default
        socket.force_non_field = True
        return socket

    parameter('Angle Seams', 'NodeSocketBool', True, 'Add seams on sharp edges')
    angle = parameter('Seam Angle', 'NodeSocketFloat', radians(35),
                      'Cut edges where the angle between faces exceeds this value')
    angle.subtype = 'ANGLE'
    angle.min_value, angle.max_value = 0, radians(180)
    margin = parameter('Island Margin', 'NodeSocketFloat', 0.01, 'Spacing between UV islands')
    margin.min_value, margin.max_value = 0, 0.25
    parameter('UV Map', 'NodeSocketString', DEFAULT_MAP, 'Name of the generated UV map')

    def node(node_type, x, y):
        item = tree.nodes.new(node_type)
        item.location = (x, y)
        return item

    def boolean(operation, left, right, x, y):
        item = node('FunctionNodeBooleanMath', x, y)
        item.operation = operation
        tree.links.new(left, item.inputs[0])
        tree.links.new(right, item.inputs[1])
        return item.outputs[0]

    inputs = node('NodeGroupInput', -780, 100).outputs
    output = node('NodeGroupOutput', 710, 100)
    marked = node('GeometryNodeInputNamedAttribute', -560, -235)
    marked.data_type = 'BOOLEAN'
    marked.inputs['Name'].default_value = 'uv_seam'
    edge_angle = node('GeometryNodeInputMeshEdgeAngle', -780, -230)
    compare = node('ShaderNodeMath', -560, 20)
    compare.operation = 'GREATER_THAN'
    tree.links.new(edge_angle.outputs['Unsigned Angle'], compare.inputs[0])
    tree.links.new(inputs['Seam Angle'], compare.inputs[1])
    auto = boolean('AND', compare.outputs[0], inputs['Angle Seams'], -340, 20)
    seams = boolean('OR', marked.outputs['Attribute'], auto, -130, 0)
    unwrap = node('GeometryNodeUVUnwrap', 90, 0)
    tree.links.new(seams, unwrap.inputs['Seam'])
    tree.links.new(inputs['Island Margin'], unwrap.inputs['Margin'])
    store = node('GeometryNodeStoreNamedAttribute', 340, 100)
    store.data_type = 'FLOAT2'
    store.domain = 'CORNER'
    tree.links.new(inputs['Geometry'], store.inputs['Geometry'])
    tree.links.new(inputs['UV Map'], store.inputs['Name'])
    tree.links.new(unwrap.outputs['UV'], store.inputs['Value'])
    tree.links.new(store.outputs['Geometry'], output.inputs['Geometry'])
    return tree
