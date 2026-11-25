"""Retopology and mesh simplification on a copy of the evaluated modifier stack."""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty
from idprop.types import IDPropertyGroup


bl_info = {
    "name": "Mesh Rust Studio Topology",
    "category": "Mesh",
    "location": "3D View > Sidebar > Mesh Rust Studio",
    "description": "Quad remesh and simplify the evaluated surface to a separate object",
    "version": (1, 0, 0),
    "blender": (5, 3, 0),
}

SOURCE = "blender_rust_topology_source"
RECIPE = "blender_rust_topology_recipe"
OWNED_MESH = "blender_rust_topology_output"
METHODS = (
    ("QUADRIFLOW", "QuadriFlow", "Build a quad mesh with a target face count"),
    ("COLLAPSE", "Reduce Polygons", "Collapse edges to reduce polygon count"),
    ("PLANAR", "Simplify Flat Areas", "Dissolve edges between nearly coplanar faces"),
)


def _evaluated_surface(context, source):
    graph = context.evaluated_depsgraph_get()
    evaluated = source.evaluated_get(graph)
    if source.hide_viewport or not evaluated.is_evaluated:
        raise RuntimeError("Enable the source object's viewport display to evaluate its modifier stack")
    matrix = evaluated.matrix_world.copy()
    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=graph)
    try:
        for index, slot in enumerate(evaluated.material_slots):
            if slot.link == 'OBJECT' and index < len(mesh.materials):
                mesh.materials[index] = slot.material
        return mesh, matrix
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise


def _discard_mesh(mesh):
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _solve(context, scratch, recipe):
    method = recipe["method"]
    with context.temp_override(active_object=scratch, object=scratch,
                               selected_objects=[scratch], selected_editable_objects=[scratch]):
        if method == "QUADRIFLOW":
            return bpy.ops.object.quadriflow_remesh(
                'EXEC_DEFAULT', mode='FACES', target_faces=recipe["target_faces"], seed=0,
                use_mesh_symmetry=False, use_preserve_sharp=recipe["preserve_sharp"],
                use_preserve_boundary=recipe["preserve_boundary"],
                preserve_attributes=recipe["preserve_attributes"])
        modifier = scratch.modifiers.new("Topology Simplification", 'DECIMATE')
        if method == "PLANAR":
            modifier.decimate_type = 'DISSOLVE'
            modifier.angle_limit = recipe["angle_limit"]
            modifier.delimit = {'NORMAL', 'MATERIAL', 'SEAM', 'SHARP', 'UV'}
        else:
            modifier.decimate_type = 'COLLAPSE'
            modifier.ratio = recipe["ratio"]
        return bpy.ops.object.modifier_apply(modifier=modifier.name)


def _result(context, source, recipe, previous=None):
    mesh, matrix = _evaluated_surface(context, source)
    scratch = None
    published = False
    try:
        if not mesh.polygons:
            raise RuntimeError("The evaluated mesh has no faces")
        input_faces = len(mesh.polygons)
        scratch = bpy.data.objects.new("Topology Scratch", mesh)
        context.scene.collection.objects.link(scratch)
        scratch.matrix_world = matrix
        status = _solve(context, scratch, recipe)
        if status != {'FINISHED'} or not scratch.data.polygons:
            raise RuntimeError("Could not build topology from the evaluated surface")
        output_faces = len(scratch.data.polygons)
        scratch.data[OWNED_MESH] = True
        if previous is None:
            scratch.name = f"{source.name} Topology"
            scratch.data.name = scratch.name
            scratch[SOURCE] = source
            scratch[RECIPE] = recipe
            published = True
            return scratch, input_faces, output_faces

        old_mesh = previous.data
        scratch.data.name = f"{previous.name} Mesh"
        previous.data = scratch.data
        previous.matrix_world = matrix
        if old_mesh.get(OWNED_MESH):
            _discard_mesh(old_mesh)
        return previous, input_faces, output_faces
    finally:
        if scratch is not None and not published:
            output_mesh = scratch.data
            bpy.data.objects.remove(scratch, do_unlink=True)
            if output_mesh != mesh:
                _discard_mesh(output_mesh)
        _discard_mesh(mesh)


class BLENDER_RUST_OT_optimize_topology(bpy.types.Operator):
    bl_idname = "blender_rust.optimize_topology"
    bl_label = "Optimize Topology"
    bl_description = "Build a separate mesh from the evaluated modifier stack"
    bl_options = {'REGISTER', 'UNDO'}

    method: EnumProperty(name="Method", items=METHODS, default="QUADRIFLOW")
    target_faces: IntProperty(name="Target Faces", default=2500, min=1, soft_max=100000)
    preserve_sharp: BoolProperty(name="Preserve Sharp Features", default=True)
    preserve_boundary: BoolProperty(name="Preserve Boundaries", default=True)
    preserve_attributes: BoolProperty(
        name="Reproject Attributes", default=True,
        description="Approximate existing mesh attributes on the new topology")
    ratio: FloatProperty(name="Keep Ratio", default=0.5, min=0.0, max=1.0, subtype='FACTOR')
    angle_limit: FloatProperty(
        name="Dissolve Angle", default=math.radians(5), min=0.0, max=math.pi, subtype='ANGLE')

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.object is not None and context.object.type == 'MESH'

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "method")
        if self.method == "QUADRIFLOW":
            for name in ("target_faces", "preserve_sharp", "preserve_boundary", "preserve_attributes"):
                layout.prop(self, name)
        elif self.method == "COLLAPSE":
            layout.prop(self, "ratio")
        else:
            layout.prop(self, "angle_limit")

    def execute(self, context):
        source = context.object
        recipe = {name: getattr(self, name) for name in
                  ("method", "target_faces", "preserve_sharp", "preserve_boundary",
                   "preserve_attributes", "ratio", "angle_limit")}
        try:
            output, faces_before, faces_after = _result(context, source, recipe)
        except RuntimeError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        for selected in context.selected_objects:
            selected.select_set(False)
        output.select_set(True)
        context.view_layer.objects.active = output
        self.report({'INFO'}, f"Created {output.name} ({faces_before:,} to {faces_after:,} faces)")
        return {'FINISHED'}


class BLENDER_RUST_OT_refresh_topology(bpy.types.Operator):
    bl_idname = "blender_rust.refresh_topology"
    bl_label = "Update Topology"
    bl_description = "Recompute the selected result from its source using the same settings"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return (context.mode == 'OBJECT' and obj is not None and obj.type == 'MESH'
                and obj.is_editable and SOURCE in obj and RECIPE in obj)

    def execute(self, context):
        output = context.object
        source = output.get(SOURCE)
        if (not isinstance(source, bpy.types.Object) or source.type != 'MESH' or source == output
                or context.view_layer.objects.get(source.name) != source):
            self.report({'WARNING'}, "The topology source is unavailable in the current view layer")
            return {'CANCELLED'}
        recipe = output[RECIPE]
        if not isinstance(recipe, IDPropertyGroup) or recipe.get("method") not in {
                'QUADRIFLOW', 'COLLAPSE', 'PLANAR'}:
            self.report({'WARNING'}, "The topology settings are invalid")
            return {'CANCELLED'}
        try:
            _, faces_before, faces_after = _result(context, source, recipe, previous=output)
        except (KeyError, TypeError, RuntimeError) as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Updated {output.name} ({faces_before:,} to {faces_after:,} faces)")
        return {'FINISHED'}


class BLENDER_RUST_PT_topology(bpy.types.Panel):
    bl_label = "Topology"
    bl_idname = "BLENDER_RUST_PT_topology"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Mesh Rust Studio"

    @classmethod
    def poll(cls, context):
        return BLENDER_RUST_OT_optimize_topology.poll(context)

    def draw(self, context):
        layout = self.layout
        obj = context.object
        if BLENDER_RUST_OT_refresh_topology.poll(context):
            source = obj.get(SOURCE)
            if isinstance(source, bpy.types.Object):
                layout.label(text=f"Source {source.name}")
            layout.operator("blender_rust.refresh_topology")
        else:
            layout.operator("blender_rust.optimize_topology")


register, unregister = bpy.utils.register_classes_factory((
    BLENDER_RUST_OT_optimize_topology,
    BLENDER_RUST_OT_refresh_topology,
    BLENDER_RUST_PT_topology,
))
