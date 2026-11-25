"""Preview moving cutter vertices while preserving the live Edit Mode mesh."""

from contextlib import suppress
from math import isfinite
from time import perf_counter

import bmesh
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector
import numpy as np

from . import cutter_of, is_boolean_plus, operation_of
from . import drag
from .edit_worker import EditPreviewWorker, edit_topology
from .visual_draw import DifferenceDisplay, ReducedDisplay, SurfaceDisplay


def edit_pair(context):
    """Find the single visible Difference driven by the active edit mesh."""
    cutter = context.edit_object
    if context.mode != "EDIT_MESH" or cutter is None or not cutter.is_editable or context.window is None:
        return None, None, None
    card = getattr(context, "boolean_plus_modifier", None)
    if card is not None:
        if (is_boolean_plus(card) and card.show_viewport and cutter_of(card) == cutter
                and operation_of(card) == "DIFFERENCE" and card.id_data.visible_get(view_layer=context.view_layer)):
            return card.id_data, cutter, card
        return None, None, None
    matches = []
    for target in context.view_layer.objects:
        if target == cutter or not target.visible_get(view_layer=context.view_layer):
            continue
        matches.extend((target, cutter, modifier) for modifier in target.modifiers
                       if is_boolean_plus(modifier) and modifier.show_viewport and cutter_of(modifier) == cutter)
    if len(matches) == 1 and operation_of(matches[0][2]) == "DIFFERENCE":
        return matches[0]
    return None, None, None


def _check_tools(context, cutter):
    settings = context.tool_settings
    if (len(context.objects_in_mode) != 1 or settings.use_proportional_edit or settings.use_snap or
            settings.use_mesh_automerge or settings.use_transform_correct_face_attributes or
            context.scene.transform_orientation_slots[0].type != "GLOBAL" or
            any(getattr(cutter.data, f"use_mirror_{axis}") for axis in "xyz")):
        raise ValueError("Use G in Edit Mode with the current transform settings.")


def _coordinates(mesh):
    return np.asarray([tuple(vertex.co) for vertex in mesh.verts], dtype="f4").reshape(-1, 3)


class EditSession:
    def __init__(self, context, area, region, target, cutter):
        self.area, self.region = area, region
        self.window, self.manager = context.window, context.window_manager
        self.scene, self.layer = context.scene, context.view_layer
        self.target, self.cutter = target, cutter
        self.hidden = target.hide_get(view_layer=self.layer)
        self.space = area.spaces.active
        self.overlays = self.space.overlay.show_overlays
        self.cutter_matrix = cutter.matrix_world.copy()
        self.inverse = self.cutter_matrix.to_3x3().inverted()
        self.origin = cutter.location.copy()
        self.distance_scale = drag.input_distance_scale(context.scene.unit_settings)
        mesh = bmesh.from_edit_mesh(cutter.data)
        mesh.verts.index_update()
        self.selection = tuple(vertex.index for vertex in mesh.verts if vertex.select and not vertex.hide)
        if not self.selection:
            raise ValueError("Select vertices, edges or faces on the cutter.")
        self.base = _coordinates(mesh)
        self.points = self.base.copy()
        self.indices = np.asarray(self.selection, dtype="i4")
        self.center = self.cutter_matrix @ Vector(self.base[self.indices].mean(axis=0))
        self.edges = [(edge.verts[0].index, edge.verts[1].index) for edge in mesh.edges if not edge.hide]
        self.frame = self.scene.frame_current, self.scene.frame_subframe
        self.edit_stack = self._edit_stack()
        self.visual_inputs = drag._visual_signature(target, cutter, self.scene, 0)
        if self.visual_inputs is None:
            raise ValueError("Use the standard G command to edit this stack.")
        self.inputs_changed = False
        self.worker = None
        self.timer = None
        self.handler = None
        self.visible = False
        self.closed = False
        self.error = None
        self.confirm_requested = False
        self.motion_anchor = None
        self.cursor = None
        self.axis = None
        self.plane = False
        self.numeric = ""
        self.sequence = 0
        self.shown = -1
        self.last_event = perf_counter()
        self.refreshed_at = 0.0
        self.worker_ms = None
        self.preview_kind = None
        self.display = None
        self.surface = None
        self.surface_data = None
        self.upload = None
        self.cage_sequence = -1
        self.cage = ()
        try:
            if drag._cached_surface is not None and self.visual_inputs.surface == drag._cached_surface[0]:
                self.surface = drag._cached_surface[1]
            drag._take_prepared_visual()
            drag._visual_warmup.discard_array_stage()
            # A single vertex can make an otherwise convex cutter concave.
            # Prepare that shader along with the initial view, before motion.
            self.worker = EditPreviewWorker(target, cutter, self.scene, mesh,
                                            surface_cached=self.surface is not None,
                                            prefer_nonconvex=len(self.selection) == 1)
            self.worker.submit(self.sequence, self.points)
            self.timer = self.manager.event_timer_add(0.015, window=self.window)
            self.handler = bpy.types.SpaceView3D.draw_handler_add(self._draw, (), "WINDOW", "POST_VIEW")
            self._header()
        except Exception:
            self.close()
            raise

    def _edit_stack(self):
        return tuple((modifier.name, modifier.show_in_editmode) for modifier in self.cutter.modifiers
                     if modifier.show_viewport)

    def valid(self, context):
        return (not self.inputs_changed and self.window.scene == self.scene and self.window.view_layer == self.layer and
                self.area in self.window.screen.areas[:] and self.area.type == "VIEW_3D" and
                self.area.spaces.active == self.space and context.edit_object == self.cutter and
                (self.scene.frame_current, self.scene.frame_subframe) == self.frame and
                drag._supports_visual_preview(self.window, self.area))

    def _header(self):
        axis = f"  |  {'Plane ' if self.plane else ''}{self.axis}" if self.axis else ""
        numeric = f"  |  {self.numeric}" if self.numeric else ""
        if self.confirm_requested:
            state = "Finishing preview"
        elif self.shown < 0:
            state = "Preparing preview"
        else:
            state = "Level 0 preview" if self.preview_kind == "reduced_auto_mesh" else "Cut preview"
        self.area.header_text_set(f"Boolean Plus  |  {state}{axis}{numeric}  |  "
                                  "X/Y/Z to constrain  |  Enter for exact result  |  Esc to cancel")

    def motion(self, event, *, use_event_position=True):
        if use_event_position:
            self.cursor = Vector((event.mouse_x - self.region.x, event.mouse_y - self.region.y))
            if self.motion_anchor is None and (0 <= self.cursor.x < self.region.width and
                                               0 <= self.cursor.y < self.region.height):
                self.motion_anchor = self.cursor.copy()
        if self.motion_anchor is None:
            return
        axis = drag._axes.get(self.axis)
        view = self.space.region_3d
        displacement = drag._mouse_offset(self.region, view, self.cursor, self.motion_anchor,
                                          self.center, axis, self.plane)
        if self.numeric:
            try:
                distance = float(self.numeric.replace(",", "."))
            except ValueError:
                return
            if not isfinite(distance):
                return
            direction = axis if axis is not None and not self.plane else displacement.normalized()
            displacement = direction * distance * self.distance_scale
        points = self.base[self.indices] + np.asarray(self.inverse @ displacement, dtype="f4")
        if np.array_equal(points, self.points[self.indices]):
            return
        self.points[self.indices] = points
        self.last_event = perf_counter()
        self.sequence += 1
        self.worker.submit(self.sequence, self.points)
        self.area.tag_redraw()

    def refresh(self):
        self.refreshed_at = perf_counter()
        if self.error is not None:
            raise ValueError(self.error)
        if self.surface is None and self.surface_data is None and (path := self.worker.surface_path) is not None:
            try:
                with np.load(path, allow_pickle=False) as data:
                    self.surface_data = dict(data)
            except PermissionError:
                pass
        if (answer := self.worker.poll()) is not None:
            self.upload = answer
            self.area.tag_redraw()
        if self.shown >= 0 and not self.visible:
            self.target.hide_set(True, view_layer=self.layer)
            if self.overlays:
                self.space.overlay.show_overlays = False
            self.visible = True
            self.area.tag_redraw()

    def _draw(self):
        context = bpy.context
        if (self.error is not None or self.inputs_changed or context.window != self.window or
                context.area != self.area or context.region != self.region or context.region_data is None):
            return
        try:
            if self.upload is not None:
                sequence, tables, self.worker_ms = self.upload
                if "fallback_positions" in tables:
                    self.display = ReducedDisplay(tables, Matrix(tables["target_matrix_world"].tolist()))
                    self.preview_kind = "reduced_auto_mesh"
                else:
                    if self.surface is None:
                        if self.surface_data is None:
                            return
                        self.surface = SurfaceDisplay(
                            self.surface_data, DifferenceDisplay.shader(nonconvex="cutter_bvh" in tables))
                        self.surface_data = None
                        drag._remember_surface(self.visual_inputs, self.surface)
                    self.display = DifferenceDisplay(tables, surface=self.surface)
                    self.preview_kind = "gpu_nonconvex" if "cutter_bvh" in tables else "gpu_shells"
                self.upload = None
            else:
                sequence = self.shown
            if self.display is not None:
                self.display.draw_in_view(context.region_data, self.origin)
                if sequence != self.shown:
                    self.shown = sequence
                    self._header()
            if self.visible and self.overlays:
                self._draw_cage()
        except (KeyError, OSError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
            self.error = f"Vertex preview unavailable. {error}"

    def _draw_cage(self):
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        if self.cage_sequence != self.sequence:
            lines = batch_for_shader(shader, "LINES", {"pos": self.points}, indices=self.edges) if self.edges else None
            points = batch_for_shader(shader, "POINTS", {"pos": self.points[self.indices]})
            self.cage = lines, points
            self.cage_sequence = self.sequence
        lines, points = self.cage
        with gpu.matrix.push_pop():
            gpu.matrix.multiply_matrix(self.cutter_matrix)
            shader.bind()
            if lines is not None:
                shader.uniform_float("color", (0.13, 0.13, 0.13, 1.0))
                lines.draw(shader)
            shader.uniform_float("color", (1.0, 0.45, 0.06, 1.0))
            gpu.state.point_size_set(5.0)
            try:
                points.draw(shader)
            finally:
                gpu.state.point_size_set(1.0)

    def _commit(self):
        mesh = bmesh.from_edit_mesh(self.cutter.data)
        if (self.cutter.matrix_world != self.cutter_matrix or self.cutter.location != self.origin or
                self._edit_stack() != self.edit_stack or edit_topology(mesh) != self.worker.topology or
                drag._visual_signature(self.target, self.cutter, self.scene, 0) != self.visual_inputs or
                not np.array_equal(_coordinates(mesh), self.base)):
            raise ValueError("Source mesh or settings changed. Move cancelled.")
        mesh.verts.ensure_lookup_table()
        for index in self.selection:
            mesh.verts[index].co = self.points[index]
        mesh.normal_update()
        bmesh.update_edit_mesh(self.cutter.data, loop_triangles=True, destructive=False)

    def close(self, confirm=False):
        self.closed = True
        try:
            if confirm:
                self._commit()
        finally:
            if self.handler is not None:
                with suppress(ReferenceError, RuntimeError, ValueError):
                    bpy.types.SpaceView3D.draw_handler_remove(self.handler, "WINDOW")
            if self.timer is not None:
                drag._DragSession._cleanup(self.manager.event_timer_remove, self.timer)
            if self.worker is not None:
                drag._DragSession._cleanup(drag._visual_warmup.retire, self.worker)
            if self.visible:
                drag._DragSession._cleanup(setattr, self.space.overlay, "show_overlays", self.overlays)
                drag._DragSession._cleanup(self.target.hide_set, self.hidden, view_layer=self.layer)
            drag._DragSession._cleanup(self.area.header_text_set, None)
            drag._DragSession._cleanup(self.area.tag_redraw)
            self.display = self.surface = self.surface_data = self.upload = None
            self.cage = ()


class BOOLEANPLUS_OT_move_edit_vertices(bpy.types.Operator):
    bl_idname = "boolean_plus.move_edit_vertices"
    bl_label = "Move Cutter Vertices with Preview"
    bl_description = "Move the selected cutter vertices with a cut preview and press Enter to update the full stack"
    bl_options = {"BLOCKING", "UNDO"}

    @classmethod
    def poll(cls, context):
        if drag._session is not None or edit_pair(context)[0] is None:
            return False
        viewport = drag._viewport(context)
        if viewport is None or not drag._supports_visual_preview(context.window, viewport[0]):
            cls.poll_message_set("Use Solid viewport shading with X-Ray off.")
            return False
        return True

    def invoke(self, context, _event):
        target, cutter, modifier = edit_pair(context)
        try:
            _check_tools(context, cutter)
            target.modifiers.active = modifier
            drag._session = self._edit = EditSession(context, *drag._viewport(context), target, cutter)
        except (OSError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
            self.report({"WARNING"}, str(error))
            return {"CANCELLED"}
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        session = getattr(self, "_edit", None)
        if session is None or session is not drag._session:
            return {"CANCELLED"}
        try:
            if (event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS") or not session.valid(context):
                drag._finish()
                return {"CANCELLED"}
            if event.type == "TIMER" and perf_counter() - session.refreshed_at >= 0.012:
                session.refresh()
            elif not session.confirm_requested:
                if event.type == "MOUSEMOVE":
                    session.motion(event)
                elif event.value == "PRESS":
                    if event.type in {"LEFTMOUSE", "RET", "NUMPAD_ENTER"}:
                        if np.array_equal(session.points, session.base):
                            drag._finish()
                            return {"CANCELLED"}
                        session.confirm_requested = True
                        session._header()
                    elif event.type in drag._axes:
                        session.axis = event.type if session.axis != event.type else None
                        session.plane = event.shift
                        session.motion(event, use_event_position=False)
                        session._header()
                    elif event.type == "BACK_SPACE" or (
                            event.unicode and event.unicode in "0123456789.,-" and len(session.numeric) < 18):
                        session.numeric = (session.numeric[:-1] if event.type == "BACK_SPACE"
                                           else session.numeric + event.unicode)
                        session.motion(event, use_event_position=False)
                        session._header()
            if session.confirm_requested and session.shown == session.sequence:
                drag._finish(confirm=True)
                return {"FINISHED"}
        except (OSError, ReferenceError, RuntimeError, TypeError, ValueError) as error:
            drag._finish()
            self.report({"WARNING"}, f"Boolean Plus. {error}")
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def cancel(self, _context):
        if getattr(self, "_edit", None) is drag._session:
            drag._finish()
