"""Boolean Plus live modifier and editing tools for Blender 5.3."""

import bpy

bl_info = {
    "name": "Boolean Plus",
    "author": "Mesh Rust Studio",
    "version": (0, 12, 0),
    "blender": (5, 3, 0),
    "location": "Properties > Modifiers > Add Modifier > Blender Plus",
    "description": "Live Difference, Union and Intersection with guarded solver routing",
    "category": "Object",
}

OPERATIONS = {
    "DIFFERENCE": "Difference",
    "UNION": "Union",
    "INTERSECT": "Intersection",
}
GROUP_MARKER = "boolean_plus_operation"
SEAM_GROUP = "boolean_plus_seam_group"
RENDER_SEAM_GROUP = "boolean_plus_render_seam_group"
CUTTER_NODE = "Boolean Plus Cutter"
CONTROLS_MARKER = "boolean_plus_controls"
SOLVERS = {
    "Auto": "Prefer Manifold where suitable and keep Exact as a fallback",
    "Exact": "Use Exact for intersecting operands",
}


class BooleanGraph:
    """Build a serializable modifier graph evaluated by Blender's dependency graph."""

    def __init__(self):
        self.tree = bpy.data.node_groups.new("Boolean Plus", "GeometryNodeTree")
        self.tree.is_modifier = True
        for direction in ("INPUT", "OUTPUT"):
            self.tree.interface.new_socket(name="Geometry", in_out=direction, socket_type="NodeSocketGeometry")
        for name, kind, description in (
            ("Operation", "Menu", "How the target and cutter are combined"),
            ("Object", "Object", "Mesh used as the cutter, including its evaluated modifiers"),
            ("Solver", "Menu", "Choose automatic routing or Exact"),
        ):
            socket = self.tree.interface.new_socket(name=name, in_out="INPUT", socket_type=f"NodeSocket{kind}")
            socket.description = description
            if kind == "Menu":
                socket.menu_expanded = True
        self.row = {}
        self.owned_seams = []

    def node(self, kind, label, column):
        node = self.tree.nodes.new(kind)
        node.label = label
        node.location = (column * 225, self.row.get(column, 0))
        self.row[column] = node.location.y - 170
        return node

    def wire(self, node, **values):
        for name, value in values.items():
            socket = next(socket for socket in node.inputs if socket.name == name and socket.enabled)
            if isinstance(value, bpy.types.NodeSocket):
                self.tree.links.new(value, socket)
            else:
                socket.default_value = value

    def compare(self, data_type, operation, left, right, column):
        node = self.node("FunctionNodeCompare", operation, column)
        node.data_type = data_type
        node.operation = operation
        self.wire(node, A=left, B=right)
        return node.outputs["Result"]

    def switch(self, condition, on_false, on_true, label, column):
        node = self.node("GeometryNodeSwitch", label, column)
        node.input_type = "GEOMETRY"
        self.wire(node, **{"Switch": condition, "False": on_false})
        if on_true is not None:
            self.wire(node, **{"True": on_true})
        return node.outputs[0]

    def menu(self, source, items, data_type, label, column):
        node = self.node("GeometryNodeMenuSwitch", label, column)
        node.data_type = data_type
        for index, (name, description) in enumerate(items.items()):
            item = node.enum_items[index] if index < len(node.enum_items) else node.enum_items.new(name)
            item.name = name
            item.description = description
        self.wire(node, Menu=source)
        return node

    def logic(self, operation, left, right, column):
        node = self.node("FunctionNodeBooleanMath", operation.title(), column)
        node.operation = operation
        self.tree.links.new(left, node.inputs[0])
        self.tree.links.new(right, node.inputs[1])
        return node.outputs[0]

    def solve(self, operation, solver, target, cutter, column):
        node = self.node("GeometryNodeMeshBoolean", f"{solver.title()} {OPERATIONS[operation]}", column)
        node.operation = operation
        node.solver = solver
        # Union and Intersection take both operands in one multi-input socket.
        if operation == "DIFFERENCE":
            self.wire(node, **{"Mesh 1": target, "Mesh 2": cutter})
        else:
            socket = next(socket for socket in node.inputs if socket.identifier == "Mesh 2" and socket.enabled)
            self.tree.links.new(target, socket)
            self.tree.links.new(cutter, socket)
        return node.outputs["Mesh"]

    def mesh_size(self, geometry, column):
        node = self.node("GeometryNodeAttributeDomainSize", "Mesh domain sizes", column)
        node.component = "MESH"
        self.wire(node, Geometry=geometry)
        return node

    def closed(self, geometry, incident_faces):
        bad = self.compare("INT", "NOT_EQUAL", incident_faces, 2, 2)
        node = self.node("GeometryNodeAttributeStatistic", "Count open or non-manifold edges", 3)
        node.domain = "EDGE"
        node.data_type = "FLOAT"
        self.wire(node, Geometry=geometry, Attribute=bad)
        return self.compare("FLOAT", "LESS_THAN", node.outputs["Max"], 0.5, 4)

    def bounds(self, geometry):
        box = self.node("GeometryNodeBoundBox", "Operand bounds", 1)
        self.wire(box, Geometry=geometry)
        return tuple(self.split(box.outputs[name]) for name in ("Min", "Max"))

    def split(self, vector):
        node = self.node("ShaderNodeSeparateXYZ", "Bounds coordinates", 2)
        self.wire(node, Vector=vector)
        return node.outputs

    def separated(self, target, cutter):
        target_min, target_max = self.bounds(target)
        cutter_min, cutter_max = self.bounds(cutter)
        # Strict separation keeps touching and coincident components in the real solver.
        axis_separation = []
        for axis in ("X", "Y", "Z"):
            before = self.compare("FLOAT", "LESS_THAN", target_max[axis], cutter_min[axis], 3)
            after = self.compare("FLOAT", "LESS_THAN", cutter_max[axis], target_min[axis], 3)
            axis_separation.append(self.logic("OR", before, after, 4))
        x, y, z = axis_separation
        return self.logic("OR", self.logic("OR", x, y, 5), z, 6)

    def join_disjoint(self, target, cutter):
        node = self.node("GeometryNodeJoinGeometry", "Join separated solids", 7)
        self.tree.links.new(target, node.inputs["Geometry"])
        self.tree.links.new(cutter, node.inputs["Geometry"])
        return node.outputs["Geometry"]

    def nested(self, group, target, cutter, label):
        node = self.node("GeometryNodeGroup", label, 8)
        node.node_tree = group
        self.wire(node, Geometry=target, Object=cutter)
        return node.outputs["Geometry"]

    def guarded_manifold(self, operation, target, cutter, closed, exact):
        manifold = self.solve(operation, "MANIFOLD", target, cutter, 8)
        valid = self.compare("INT", "GREATER_THAN", self.mesh_size(manifold, 9).outputs["Face Count"], 0, 10)
        guard = self.node("GeometryNodeSwitch", "Closed inputs and valid manifold result", 11)
        guard.input_type = "BOOLEAN"
        self.wire(guard, **{"Switch": closed, "False": False, "True": valid})
        return self.switch(guard.outputs[0], exact, manifold, "Guarded live Boolean", 12)

    def create(self, cutter, operation, seam=None, render_seam=None):
        tree = self.tree
        inputs = self.node("NodeGroupInput", "Boolean Plus inputs", 0).outputs
        target, cutter_input = inputs["Geometry"], inputs["Object"]
        source = self.node("GeometryNodeObjectInfo", "Live cutter", 0)
        source.name = CUTTER_NODE
        source.transform_space = "RELATIVE"
        self.wire(source, **{"Object": cutter_input, "As Instance": False})
        transformed = source.outputs["Geometry"]
        determinant = self.node("FunctionNodeMatrixDeterminant", "Relative transform determinant", 1)
        self.wire(determinant, Matrix=source.outputs["Transform"])
        mirrored = self.compare("FLOAT", "LESS_THAN", determinant.outputs["Determinant"], 0.0, 2)
        flip = self.node("GeometryNodeFlipFaces", "Restore mirrored cutter winding", 2)
        self.wire(flip, Mesh=transformed, Selection=True)
        tool = self.switch(mirrored, transformed, flip.outputs["Mesh"], "Orient the cutter", 3)
        face_count = self.node("GeometryNodeInputMeshEdgeNeighbors", "Incident faces", 2).outputs["Face Count"]
        closed = self.logic("AND", self.closed(target, face_count), self.closed(tool, face_count), 5)
        apart = self.separated(target, tool)
        has_cutter = self.compare("INT", "GREATER_THAN", self.mesh_size(transformed, 2).outputs["Face Count"], 0, 3)
        solver = self.menu(inputs["Solver"], SOLVERS, "BOOLEAN", "Solver choice", 5)
        outputs = self.menu(inputs["Operation"], dict.fromkeys(OPERATIONS.values(), ""), "GEOMETRY",
                            "Boolean operation", 14)

        seam = self.seam_with_object_input(seam)
        render_seam = self.seam_with_object_input(render_seam)
        outside = {"DIFFERENCE": target, "UNION": self.join_disjoint(target, tool), "INTERSECT": None}
        for mode, label in OPERATIONS.items():
            exact = self.solve(mode, "EXACT", target, tool, 8)
            if seam and mode == "DIFFERENCE":
                auto = self.nested(seam, target, cutter_input, "Guarded planar seam")
                if render_seam:
                    render = self.nested(render_seam, target, cutter_input, "Exact closed-volume result")
                    exact = self.switch(mirrored, render, exact, "Exact on mirrored R11 operands", 10)
                viewport = self.node("GeometryNodeIsViewport", "Viewport evaluation", 8)
                use_live = self.logic("AND", viewport.outputs[0], solver.outputs["Auto"], 9)
                result = self.switch(use_live, exact, auto, "Live in Auto viewport", 11)
                result = self.switch(mirrored, result, exact, "Exact for mirrored R11 operands", 12)
            else:
                auto = self.guarded_manifold(mode, target, tool, closed, exact)
                result = self.switch(solver.outputs["Exact"], auto, exact, f"{label} solver", 13)
            separated = self.switch(apart, result, outside[mode], f"Separated {label}", 13)
            self.tree.links.new(separated, outputs.inputs[label])

        result = self.switch(has_cutter, target, outputs.outputs[0], "Keep target without a mesh cutter", 15)
        self.wire(self.node("NodeGroupOutput", "Live Boolean result", 16), Geometry=result)
        sockets = {item.name: item for item in tree.interface.items_tree
                   if item.item_type == "SOCKET" and item.in_out == "INPUT"}
        sockets["Operation"].default_value = OPERATIONS[operation]
        sockets["Solver"].default_value = "Auto"
        sockets["Object"].default_value = cutter
        tree[CONTROLS_MARKER] = {name: sockets[name].identifier for name in ("Operation", "Object", "Solver")}
        tree[GROUP_MARKER] = operation
        if seam:
            tree[SEAM_GROUP] = seam
        if render_seam:
            tree[RENDER_SEAM_GROUP] = render_seam
        return tree

    def seam_with_object_input(self, group):
        """Use the modifier's current cutter in both R11 paths without modifying old groups."""
        if group is None:
            return None
        if any(item.name == "Object" and item.item_type == "SOCKET" and item.in_out == "INPUT"
               for item in group.interface.items_tree):
            return group
        if group.users:
            group = group.copy()
            self.owned_seams.append(group)
        source = next(node for node in group.nodes if node.bl_idname == "GeometryNodeObjectInfo")
        input_node = next(node for node in group.nodes if node.bl_idname == "NodeGroupInput")
        group.interface.new_socket(name="Object", in_out="INPUT", socket_type="NodeSocketObject")
        source.inputs["Object"].default_value = None
        group.links.new(input_node.outputs["Object"], source.inputs["Object"])
        return group

    def discard(self):
        if self.tree.users == 0:
            bpy.data.node_groups.remove(self.tree)
        for group in self.owned_seams:
            if group.users == 0:
                bpy.data.node_groups.remove(group)


def is_boolean_plus(modifier):
    return (modifier is not None and modifier.type == "NODES"
            and modifier.node_group is not None and GROUP_MARKER in modifier.node_group)


def find_modifier(target):
    """Return the active Boolean Plus modifier, or the last one in the target stack."""
    if target is None:
        return None
    active = target.modifiers.active
    if is_boolean_plus(active):
        return active
    return next((modifier for modifier in reversed(tuple(target.modifiers)) if is_boolean_plus(modifier)), None)


def context_modifier(context):
    """Use the card that drew the button, including in a pinned Properties editor."""
    modifier = getattr(context, "boolean_plus_modifier", None)
    if modifier is not None:
        return modifier if is_boolean_plus(modifier) else None
    return find_modifier(context.object)


def input_control(modifier, name):
    """Return the modifier's editable RNA socket, if this graph exposes it."""
    controls = modifier.node_group.get(CONTROLS_MARKER)
    return getattr(modifier.properties.inputs, controls[name], None) if controls and name in controls else None


def operation_of(modifier):
    """Read the selected operation from either the modifier or a previously saved graph."""
    control = input_control(modifier, "Operation")
    choice = control.value if control is not None else None
    return next((mode for mode, label in OPERATIONS.items() if label == choice), modifier.node_group[GROUP_MARKER])


def cutter_of(modifier):
    """Read the live cutter object without evaluating the modifier."""
    control = input_control(modifier, "Object")
    if control is not None:
        cutter = control.value
        return cutter if cutter != modifier.id_data else None
    source = modifier.node_group.nodes.get(CUTTER_NODE)
    return source.inputs["Object"].default_value if source and source.bl_idname == "GeometryNodeObjectInfo" else None


def set_modifier_group(modifier, group):
    modifier.node_group = group
    modifier.show_group_selector = False
    modifier.show_manage_panel = False


def add_boolean_plus(target, cutter=None, operation="DIFFERENCE", seam_builder=None,
                     render_seam_builder=None):
    """Append an editable Boolean Plus after the target's existing modifiers."""
    if target.type != "MESH" or (cutter is not None and (target == cutter or cutter.type != "MESH")):
        raise ValueError("Choose two different meshes. The active mesh receives the modifier.")
    if operation not in OPERATIONS:
        raise ValueError("Unknown Boolean Plus operation.")
    seam = None
    render_seam = None
    modifier = None
    graph = BooleanGraph()
    try:
        seam = seam_builder(cutter) if seam_builder else None
        render_seam = render_seam_builder(cutter) if render_seam_builder else None
        group = graph.create(cutter, operation, seam=seam, render_seam=render_seam)
        modifier = target.modifiers.new("Boolean Plus", "NODES")
        set_modifier_group(modifier, group)
        modifier.show_in_editmode = True
        return modifier
    except Exception:
        if modifier is not None:
            target.modifiers.remove(modifier)
        graph.discard()
        for nested in (seam, render_seam):
            if nested is not None and nested.users == 0:
                bpy.data.node_groups.remove(nested)
        raise


def set_operation(target, operation):
    """Change the live operation on the target without changing its stack order."""
    if operation not in OPERATIONS:
        raise ValueError("Unknown Boolean Plus operation.")
    modifier = find_modifier(target)
    if modifier is None:
        raise ValueError("The active mesh has no Boolean Plus modifier.")
    control = input_control(upgrade_modifier(modifier), "Operation")
    control.value = OPERATIONS[operation]
    return modifier


def upgrade_modifier(modifier):
    """Expose in-stack controls on a legacy modifier while keeping its stack position."""
    if not is_boolean_plus(modifier):
        raise ValueError("Boolean Plus modifier not found.")
    if input_control(modifier, "Operation") is not None:
        return modifier
    previous = modifier.node_group
    graph = BooleanGraph()
    try:
        group = graph.create(cutter_of(modifier), operation_of(modifier), seam=previous.get(SEAM_GROUP),
                             render_seam=previous.get(RENDER_SEAM_GROUP))
        set_modifier_group(modifier, group)
    except Exception:
        graph.discard()
        raise
    if previous.users == 0:
        bpy.data.node_groups.remove(previous)
    return modifier


def selected_cutter(context):
    target = context.object
    selected = [item for item in context.selected_objects if item.type == "MESH" and item != target]
    return selected[0] if len(selected) == 1 else None


class _BooleanPlusOperation:
    bl_options = {"REGISTER", "UNDO"}
    operation = ""

    @classmethod
    def poll(cls, context):
        target = context.object
        return (target is not None and target.type == "MESH" and target.is_editable
                and (find_modifier(target) is not None
                     or (target.mode == "OBJECT" and selected_cutter(context) is not None)))

    def execute(self, context):
        target = context.object
        try:
            if find_modifier(target):
                set_operation(target, self.operation)
            else:
                cutter = selected_cutter(context)
                if cutter is None:
                    raise ValueError("Select the cutter and make the target mesh active.")
                add_boolean_plus(target, cutter, operation=self.operation)
        except (RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BOOLEANPLUS_OT_difference(_BooleanPlusOperation, bpy.types.Operator):
    bl_idname = "boolean_plus.difference"
    bl_label = "Boolean Plus Difference"
    bl_description = "Subtract the cutter from the active mesh"
    operation = "DIFFERENCE"


class BOOLEANPLUS_OT_union(_BooleanPlusOperation, bpy.types.Operator):
    bl_idname = "boolean_plus.union"
    bl_label = "Boolean Plus Union"
    bl_description = "Combine the active mesh and cutter"
    operation = "UNION"


class BOOLEANPLUS_OT_intersection(_BooleanPlusOperation, bpy.types.Operator):
    bl_idname = "boolean_plus.intersection"
    bl_label = "Boolean Plus Intersection"
    bl_description = "Keep the volume shared by both meshes"
    operation = "INTERSECT"


class BOOLEANPLUS_OT_apply(bpy.types.Operator):
    bl_idname = "boolean_plus.apply"
    bl_label = "Apply Boolean Plus"
    bl_description = "Apply this modifier to the mesh"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        target = context.object
        if (context.mode != "OBJECT" or target is None or target != context.view_layer.objects.active
                or not target.is_editable):
            return False
        modifier = context_modifier(context)
        return (modifier is not None and modifier.id_data == target and target.modifiers[0] == modifier
                and bpy.ops.object.modifier_apply.poll())

    def execute(self, context):
        modifier = context_modifier(context)
        return bpy.ops.object.modifier_apply(modifier=modifier.name, single_user=True,
                                             use_selected_objects=False)


class BOOLEANPLUS_OT_bake_stack(bpy.types.Operator):
    bl_idname = "boolean_plus.bake_stack"
    bl_label = "Bake Visible Stack"
    bl_description = "Apply the entire visible modifier stack to the active mesh"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        target = context.object
        modifier = context_modifier(context)
        return (context.mode == "OBJECT" and target is not None and target.is_editable
                and target == context.view_layer.objects.active
                and target.type == "MESH" and target.mode == "OBJECT" and target.data.shape_keys is None
                and modifier is not None and modifier.id_data == target
                and bpy.ops.object.convert.poll())

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(
            self, event, title="Bake Visible Stack?", message="Apply all visible modifiers to this mesh.")

    def execute(self, context):
        target = context.object
        selected = tuple(context.selected_objects)
        others = [item for item in selected if item != target]
        try:
            for item in others:
                item.select_set(False)
            target.select_set(True)
            return bpy.ops.object.convert(target="MESH", keep_original=False)
        finally:
            for item in others:
                item.select_set(True)


OPERATORS = (BOOLEANPLUS_OT_difference, BOOLEANPLUS_OT_union, BOOLEANPLUS_OT_intersection)
CLASSES = (*OPERATORS, BOOLEANPLUS_OT_apply, BOOLEANPLUS_OT_bake_stack)


def register():
    """Register the modifier tools and independently assignable Blender operators."""
    from . import drag, ui

    for cls in CLASSES:
        bpy.utils.register_class(cls)
    drag.register()
    ui.register()


def unregister():
    """Unregister the user interface and leave serialized scene graphs in place."""
    from . import drag, ui

    ui.unregister()
    drag.unregister()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
