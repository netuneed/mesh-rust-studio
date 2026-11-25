"""Blender entry points and editing tools around the native modifier card."""

import bpy

from . import (
    context_modifier,
    BOOLEANPLUS_OT_apply,
    BOOLEANPLUS_OT_bake_stack,
    OPERATIONS,
    RENDER_SEAM_GROUP,
    add_boolean_plus,
    cutter_of,
    input_control,
    operation_of,
    selected_cutter,
    upgrade_modifier,
)


class BOOLEANPLUS_OT_add(bpy.types.Operator):
    bl_idname = "boolean_plus.add"
    bl_label = "Add Boolean Plus Modifier"
    bl_description = "Append Boolean Plus to the active mesh"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        target = context.object
        return context.mode == "OBJECT" and target is not None and target.type == "MESH" and target.is_editable

    def execute(self, context):
        try:
            add_boolean_plus(context.object, selected_cutter(context))
        except (RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BOOLEANPLUS_OT_upgrade(bpy.types.Operator):
    bl_idname = "boolean_plus.upgrade"
    bl_label = "Enable Editable Controls"
    bl_description = "Upgrade this saved modifier to expose its operation, cutter and solver"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        modifier = context_modifier(context)
        return modifier is not None and modifier.id_data.is_editable and input_control(modifier, "Operation") is None

    def execute(self, context):
        try:
            upgrade_modifier(context_modifier(context))
        except (RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BOOLEANPLUS_OT_select_cutter(bpy.types.Operator):
    bl_idname = "boolean_plus.select_cutter"
    bl_label = "Select Cutter"
    bl_description = "Make this cutter active to edit its shape and modifiers"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        if context.mode != "OBJECT":
            return False
        modifier = context_modifier(context)
        cutter = cutter_of(modifier) if modifier else None
        return (cutter is not None and cutter.name in context.view_layer.objects and not cutter.hide_select
                and cutter.visible_get(view_layer=context.view_layer))

    def execute(self, context):
        cutter = cutter_of(context_modifier(context))
        for item in context.selected_objects:
            item.select_set(False)
        cutter.select_set(True)
        context.view_layer.objects.active = cutter
        return {"FINISHED"}


class OBJECT_MT_modifier_add_blender_plus(bpy.types.Menu):
    bl_label = "Blender Plus"
    bl_options = {"SEARCH_ON_KEY_PRESS"}

    def draw(self, _context):
        layout = self.layout
        layout.operator(BOOLEANPLUS_OT_add.bl_idname, text="Boolean Plus", icon="MOD_BOOLEAN")
        layout.template_modifier_asset_menu_items(catalog_path="Blender+ RUST")


def draw_add_modifier(self, context):
    if context.object is not None and context.object.type == "MESH":
        self.layout.menu(OBJECT_MT_modifier_add_blender_plus.__name__)


def draw_array_controls(layout, cutter):
    if not bpy.ops.boolean_plus.adjust_array.poll():
        return
    from .array_stage import array_counts, array_modifiers

    names = array_modifiers(cutter)
    for index, (name, count) in enumerate(zip(names, array_counts(cutter, names))):
        button = layout.operator("boolean_plus.adjust_array", text=f"{name} Count: {count}", icon="MOD_ARRAY")
        button.array_index = index


class OBJECT_MT_boolean_plus_card(bpy.types.Menu):
    bl_label = "Boolean Plus"

    def draw(self, context):
        layout = self.layout
        modifier = getattr(context, "boolean_plus_modifier", None)
        if modifier is None:
            return
        cutter = cutter_of(modifier)
        operation = input_control(modifier, "Operation")
        if operation is None:
            layout.label(text=OPERATIONS[operation_of(modifier)], icon="MOD_BOOLEAN")
            if cutter is not None:
                layout.label(text=cutter.name, icon="OBJECT_DATA")
            layout.operator(BOOLEANPLUS_OT_upgrade.bl_idname, icon="FILE_REFRESH")
            return

        operations = layout.row(align=True)
        for label in OPERATIONS.values():
            operations.prop_enum(operation, "value", label, text=label)
        fields = layout.column()
        fields.use_property_split = True
        fields.prop_search(input_control(modifier, "Object"), "value", bpy.data, "objects",
                           text="Cutter", icon="OBJECT_DATA")
        fields.prop(input_control(modifier, "Solver"), "value", text="Solver")
        if cutter is not None:
            fields.prop(cutter, "display_type", text="Cutter Display")

        header, tools = layout.panel("boolean_plus_tools", default_closed=True)
        header.label(text="Tools")
        if tools is None:
            return
        tools.operator_context = "INVOKE_DEFAULT"
        if operation_of(modifier) == "DIFFERENCE" and RENDER_SEAM_GROUP in modifier.node_group:
            tools.label(text="Closed-volume R11", icon="MESH_DATA")
        if cutter is not None:
            if context.mode == "EDIT_MESH" and context.edit_object == cutter:
                tools.operator("boolean_plus.move_edit_vertices", icon="VIEW_PAN")
            else:
                tools.operator(BOOLEANPLUS_OT_select_cutter.bl_idname, icon="RESTRICT_SELECT_OFF")
                tools.operator("boolean_plus.fast_drag", icon="VIEW_PAN")
                draw_array_controls(tools, cutter)
        settings = tools.column()
        settings.use_property_split = True
        manager = context.window_manager
        settings.prop(manager, "boolean_plus_preview_level")
        settings.prop(manager, "boolean_plus_fast_g")
        settings.prop(manager, "boolean_plus_visual_preview")
        if context.mode == "OBJECT":
            tools.separator()
            if modifier.id_data.modifiers[0] == modifier:
                tools.operator(BOOLEANPLUS_OT_apply.bl_idname, icon="CHECKMARK")
            else:
                tools.operator(BOOLEANPLUS_OT_bake_stack.bl_idname, icon="CHECKMARK")


CLASSES = (
    BOOLEANPLUS_OT_add,
    BOOLEANPLUS_OT_upgrade,
    BOOLEANPLUS_OT_select_cutter,
    OBJECT_MT_boolean_plus_card,
    OBJECT_MT_modifier_add_blender_plus,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.OBJECT_MT_modifier_add.append(draw_add_modifier)


def unregister():
    bpy.types.OBJECT_MT_modifier_add.remove(draw_add_modifier)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
