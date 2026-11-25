"""Interactive cutter movement with an isolated, lower-resolution cut preview."""

from contextlib import ExitStack, suppress
from math import isfinite
from time import monotonic

import bpy
from bpy.app.handlers import persistent
from bpy_extras.view3d_utils import region_2d_to_location_3d
from mathutils import Vector

from . import context_modifier, cutter_of, find_modifier, is_boolean_plus
from .drag_worker import PreviewWorker
from .visual_reuse import visual_inputs


_session = None
_prepared_visual = None
_cached_visual = None
_cached_surface = None
_keymaps = []
_axes = {"X": Vector((1, 0, 0)), "Y": Vector((0, 1, 0)), "Z": Vector((0, 0, 1))}
_length_units_in_meters = {
    "KILOMETERS": 1000.0, "METERS": 1.0, "CENTIMETERS": 0.01,
    "MILLIMETERS": 0.001, "MICROMETERS": 0.000001, "MILES": 1609.344,
    "FEET": 0.3048, "INCHES": 0.0254, "THOU": 0.0000254,
}


def _pair(context, use_keymap=False):
    active = context.object
    if (active is None or active.mode != "OBJECT" or context.window is None or
            _session is not None or not active.is_editable):
        return None
    if use_keymap and (not context.window_manager.boolean_plus_fast_g or
                       len(context.selected_objects) != 1):
        return None
    layer = context.view_layer
    modifier = context_modifier(context)
    if modifier is not None and not use_keymap:
        target, cutter = modifier.id_data, cutter_of(modifier)
    else:
        cutter = active
        targets = []
        for obj in layer.objects:
            if not obj.visible_get(view_layer=layer):
                continue
            targets.extend((obj, plus) for plus in obj.modifiers
                           if is_boolean_plus(plus) and plus.show_viewport and cutter_of(plus) == cutter)
        if len(targets) != 1:
            return None
        target, modifier = targets[0]
    if (cutter is None or not modifier.show_viewport or cutter.parent is not None or
            cutter.constraints or not cutter.is_editable or layer.objects.get(cutter.name) != cutter):
        return None
    if not target.is_editable or not target.visible_get(view_layer=layer):
        return None
    return target, cutter, modifier


def _viewport(context):
    if context.area is not None and context.area.type == "VIEW_3D":
        area = context.area
    else:
        area = max((item for item in context.window.screen.areas if item.type == "VIEW_3D"),
                   key=lambda item: item.width * item.height, default=None)
    if area is None or area.spaces.active.shading.type not in {"SOLID", "WIREFRAME"}:
        return None
    if area.spaces.active.region_quadviews or area.spaces.active.local_view is not None:
        return None
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    return (area, region) if region else None


def _supports_visual_preview(window, area):
    viewport = area.spaces.active
    return (viewport.shading.type == "SOLID" and not viewport.shading.show_xray and
            not viewport.region_3d.use_clip_planes and len(bpy.context.window_manager.windows) == 1 and
            sum(item.type == "VIEW_3D" for item in window.screen.areas) == 1)


def _drop_proxy(obj):
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def input_distance_scale(unit_settings):
    if unit_settings.system == "NONE":
        return 1.0
    base_unit = 0.3048 if unit_settings.system == "IMPERIAL" else 1.0
    return _length_units_in_meters.get(unit_settings.length_unit, base_unit) / unit_settings.scale_length


def _mouse_offset(region, view, cursor, anchor, center, axis, plane):
    offset = (region_2d_to_location_3d(region, view, cursor, center) -
              region_2d_to_location_3d(region, view, anchor, center))
    if axis is None:
        return offset
    if plane:
        return offset - axis * offset.dot(axis)
    depth = view.view_rotation @ Vector((0, 0, 1))
    projected = axis - depth * axis.dot(depth)
    length_sq = projected.length_squared
    if length_sq > 0.01:
        return axis * (offset.dot(projected) / length_sq)
    return axis * (offset.length if cursor.y >= anchor.y else -offset.length)


def _visual_signature(target, cutter, scene, level):
    try:
        return visual_inputs(target, cutter, scene, level)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return None


def _remember_display(inputs, display):
    global _cached_visual
    _cached_visual = inputs, display
    _remember_surface(inputs, display.surface)


def _remember_surface(inputs, surface):
    global _cached_surface
    _cached_surface = inputs.surface, surface


def _take_prepared_visual(signature=None):
    global _prepared_visual
    prepared, _prepared_visual = _prepared_visual, None
    if prepared is None:
        return None
    _visual_warmup.clear_upload()
    inputs, worker = prepared
    invalid = _visual_warmup.invalid_job is prepared
    if invalid:
        _visual_warmup.invalid_job = None
    if signature is not None and signature == inputs and not invalid:
        worker.poll()
        if worker.error is None:
            return worker
    _visual_warmup.retire(worker)
    return None


class _VisualWarmup:
    """Prepare one known pair in the background after its edits settle."""

    def __init__(self):
        self.timer = self.tick
        self.pair = None
        self.due = None
        self.attempted = None
        self.watched = frozenset()
        self.job_started = 0.0
        self.invalid_job = None
        self.retiring = None
        self.array_stage = None
        self.upload_handler = None
        self.upload = None
        self.finished_upload = False
        self.want_standby = False

    def watch(self, window, scene, target, cutter):
        pair = window, scene, target, cutter
        if pair != self.pair:
            self.reset()
            self.pair = pair

    def schedule(self, delay=0.15):
        if self.pair is None:
            return
        self.due = monotonic() + delay
        if not bpy.app.timers.is_registered(self.timer):
            bpy.app.timers.register(self.timer, first_interval=delay)

    def note_update(self, scene, changed):
        if self.pair is None or _session is not None or scene != self.pair[1]:
            return
        target, cutter = self.pair[2:]
        try:
            relevant = {target.session_uid, cutter.session_uid, target.data.session_uid, cutter.data.session_uid}
            if any(item.session_uid in relevant or item.session_uid in self.watched for item in changed):
                self.schedule()
        except ReferenceError:
            self.schedule(0.0)

    def clear_upload(self):
        if self.upload_handler is not None:
            with suppress(ReferenceError, RuntimeError, ValueError):
                bpy.types.SpaceView3D.draw_handler_remove(self.upload_handler, "WINDOW")
            self.upload_handler = None
        self.upload = None
        self.finished_upload = False

    def retire(self, worker):
        worker.close()
        self.retiring = worker

    def take_array_stage(self, target, cutter, scene):
        stage, self.array_stage = self.array_stage, None
        if stage is not None:
            with suppress(AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                if stage.matches(target, cutter, scene):
                    return stage
            self.retire(stage)
        return None

    def discard_array_stage(self):
        stage, self.array_stage = self.array_stage, None
        if stage is not None:
            self.retire(stage)

    def reset(self):
        self.pair = None
        self.due = None
        self.attempted = None
        self.watched = frozenset()
        self.invalid_job = None
        self.want_standby = False
        _take_prepared_visual()
        self.discard_array_stage()
        self.clear_upload()
        PreviewWorker.cancel_prewarm()

    def _upload_in_view(self):
        if self.upload is None or self.due is not None or _session is not None:
            return
        job, area, tables = self.upload
        if (self.finished_upload or _prepared_visual is not job or self.invalid_job is job or
                bpy.context.window != self.pair[0] or bpy.context.area != area or
                not _supports_visual_preview(self.pair[0], area)):
            return
        try:
            from .visual_draw import DifferenceDisplay

            _remember_display(job[0], DifferenceDisplay(tables))
            self.want_standby = True
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            print(f"Boolean Plus idle GPU preview unavailable: {error}", flush=True)
        finally:
            self.finished_upload = True

    def _poll_worker(self, area):
        job = _prepared_visual
        if job is None:
            return None
        worker = job[1]
        if self.finished_upload or (_cached_visual is not None and _cached_visual[0] == job[0]):
            from .array_stage import array_modifiers

            if self.invalid_job is not job and not worker.stopped and array_modifiers(self.pair[3]):
                # This worker has already loaded the same cutter that supplied the GPU frame.
                self.clear_upload()
                self.want_standby = False
                return None
        if (self.finished_upload or self.invalid_job is job or worker.visual_unavailable or worker.stopped or
                monotonic() - self.job_started > 15.0):
            _take_prepared_visual()
            return 0.04 if self.want_standby else None
        if self.upload is None and (path := worker.visual_path) is not None:
            import numpy as np

            try:
                with np.load(path, allow_pickle=False) as data:
                    self.upload = job, area, dict(data)
                self.upload_handler = bpy.types.SpaceView3D.draw_handler_add(
                    self._upload_in_view, (), "WINDOW", "POST_VIEW")
                area.tag_redraw()
            except (OSError, RuntimeError, ValueError) as error:
                print(f"Boolean Plus idle GPU tables unavailable: {error}", flush=True)
                _take_prepared_visual()
                return None
        return 0.025

    def tick(self):
        global _prepared_visual
        if self.pair is None or _session is not None:
            return None
        now = monotonic()
        if self.due is not None and now < self.due:
            return self.due - now
        if self.retiring is not None:
            if not self.retiring.stopped:
                return 0.03
            self.retiring = None
        try:
            window, scene, target, cutter = self.pair
            layer = window.view_layer
            area = next((item for item in window.screen.areas if item.type == "VIEW_3D"), None)
            manager = bpy.context.window_manager
            if (window.scene != scene or not manager.boolean_plus_visual_preview or area is None or
                    not _supports_visual_preview(window, area) or
                    layer.objects.get(target.name) != target or layer.objects.get(cutter.name) != cutter or
                    layer.objects.active not in (target, cutter)):
                self.reset()
                return None
            if target.mode != "OBJECT" or cutter.mode != "OBJECT" or window.screen.is_animation_playing:
                _take_prepared_visual()
                return 0.2
            if self.due is not None:
                level = manager.boolean_plus_preview_level
                inputs = _visual_signature(target, cutter, scene, level)
                self.due = None
                self.watched = inputs.watched if inputs else frozenset()
                if _prepared_visual is not None and (_prepared_visual[0] != inputs or
                                                     self.invalid_job is _prepared_visual):
                    _take_prepared_visual()
                    self.invalid_job = None
                if (inputs is not None and _prepared_visual is None and self.attempted != inputs and
                        (_cached_visual is None or _cached_visual[0] != inputs)):
                    if self.retiring is not None and not self.retiring.stopped:
                        self.due = now + 0.03
                        return 0.03
                    self.attempted = inputs
                    self.discard_array_stage()
                    if self.retiring is not None and not self.retiring.stopped:
                        self.attempted = None
                        self.due = now + 0.03
                        return 0.03
                    worker = PreviewWorker(target, cutter, level, scene, visual=True)
                    _prepared_visual = inputs, worker
                    self.job_started = monotonic()
                    self.want_standby = False
            if _prepared_visual is not None:
                return self._poll_worker(area)
            if self.want_standby:
                self.want_standby = False
                PreviewWorker.prewarm(visual=True)
        except ReferenceError:
            self.reset()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
            print(f"Boolean Plus will prepare the GPU preview on demand: {error}", flush=True)
            _take_prepared_visual()
        return None


_visual_warmup = _VisualWarmup()


def prepare_visual_demo(target, context):
    """Start watching the demo pair after the working copy has opened."""
    manager = context.window_manager
    modifier = find_modifier(target)
    if not manager.boolean_plus_visual_preview or modifier is None or context.window is None:
        return False
    cutter = cutter_of(modifier)
    if cutter is None:
        return False
    _visual_warmup.watch(context.window, context.scene, target, cutter)
    _visual_warmup.schedule(0.0)
    return True


@persistent
def _invalidate_visual_inputs(_scene, depsgraph):
    global _cached_visual, _cached_surface
    if not (_visual_warmup.pair or _cached_visual or _cached_surface or _prepared_visual or
            (_session and _session.visual_inputs)):
        return
    changed = [item.id.original or item.id for item in depsgraph.updates
               if isinstance(item.id, (bpy.types.Object, bpy.types.NodeTree, bpy.types.Action, bpy.types.Mesh))]
    _visual_warmup.note_update(_scene, changed)
    trees_and_actions = {item.session_uid for item in changed if isinstance(item, (bpy.types.NodeTree, bpy.types.Action))}
    meshes = [item for item in changed if isinstance(item, bpy.types.Mesh)]

    def affects(inputs):
        try:
            return bool(trees_and_actions.intersection(inputs.watched)) or any(map(inputs.changed_mesh, meshes))
        except (ReferenceError, RuntimeError, ValueError):
            return True

    if _cached_visual is not None and affects(_cached_visual[0]):
        _cached_visual = None
        _visual_warmup.attempted = None
    if _cached_surface is not None and affects(_cached_surface[0]):
        _cached_surface = None
    if _prepared_visual is not None and affects(_prepared_visual[0]):
        _visual_warmup.invalid_job = _prepared_visual
        _visual_warmup.attempted = None
        _visual_warmup.schedule()
    if _session is not None and _session.visual_inputs and affects(_session.visual_inputs):
        _session.inputs_changed = True


class _DragSession:
    def __init__(self, context, area, region, target, cutter):
        self.area, self.region = area, region
        self.layer = context.view_layer
        self.scene = context.scene
        self.window = context.window
        self.manager = context.window_manager
        self.target, self.cutter = target, cutter
        self.start = cutter.location.copy()
        self.world = cutter.matrix_world.translation.copy()
        self.original_selection = tuple(context.selected_objects)
        self.active = self.layer.objects.active
        self.hidden = [(obj, obj.hide_get(view_layer=self.layer)) for obj in (target, cutter)]
        self.worker = None
        self.timer = None
        self.target_proxy = None
        self.cutter_proxy = None
        self.visual_handler = None
        self.visual_display = None
        self.visual_data = None
        self.visual_surface = None
        self.visual_failed = False
        self.visual_active = False
        self.visual_frames = 0
        self.visual_sequence = -1
        self.visual_ready = False
        self.first_visual_at = None
        self.visual_inputs = None
        self.inputs_changed = False
        self.sequence = 0
        self.sent = -1
        self.mesh_sequence = -1
        self.axis = None
        self.plane = False
        self.numeric = ""
        self.anchor = None
        self.location = self.start.copy()
        self.level = self.manager.boolean_plus_preview_level
        self.distance_scale = input_distance_scale(context.scene.unit_settings)
        self.refreshed_at = 0.0
        self.visual_requested = (self.manager.boolean_plus_visual_preview and
                                 _supports_visual_preview(self.window, self.area))
        try:
            graph = context.evaluated_depsgraph_get()
            if self.visual_requested:
                self.visual_inputs = _visual_signature(target, cutter, self.scene, self.level)
                if self.visual_inputs is not None:
                    _visual_warmup.watch(self.window, self.scene, target, cutter)
                self._prepare_visual(graph)
            else:
                _take_prepared_visual()
            self.used_cached_visual = self.visual_ready
            self.reused_surface = self.visual_surface is not None
            self.used_prepared_visual = self.worker is not None
            if self.worker is None and not (self.used_cached_visual or self.reused_surface):
                self._start_worker(visual=self.visual_requested)
            self.target_proxy = self._proxy(context, graph, target, "Boolean Plus Preview")
            self.cutter_proxy = self._proxy(context, graph, cutter, "Boolean Plus Cutter Preview")
            self.cutter_proxy.display_type = "WIRE"
            self.cutter_proxy.show_in_front = True
            self.cutter_proxy.color = (1.0, 0.44, 0.12, 1.0)
            target.hide_set(True, view_layer=self.layer)
            cutter.hide_set(True, view_layer=self.layer)
            self.timer = self.manager.event_timer_add(0.025, window=self.window)
            if self.used_cached_visual or self.reused_surface:
                self._add_visual_handler()
            self.refresh()
        except Exception:
            self.close()
            raise

    def _prepare_visual(self, graph):
        if _cached_visual is not None and _cached_visual[0] == self.visual_inputs:
            self.visual_display = _cached_visual[1]
            self.visual_ready = True
            if _prepared_visual is not None and _prepared_visual[0] != self.visual_inputs:
                _take_prepared_visual()
            return
        if (self.visual_inputs is not None and _cached_surface is not None and
                self.visual_inputs.surface == _cached_surface[0]):
            from .visual_geometry import evaluated_cutter_tables

            try:
                if PreviewWorker.eligible(self.target, self.cutter, self.scene):
                    self.visual_data = evaluated_cutter_tables(self.target, self.cutter, graph)
                    self.visual_surface = _cached_surface[1]
                    _take_prepared_visual()
                    return
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                pass  # Unsupported cutters follow the existing snapshot path.
        self.worker = _take_prepared_visual(self.visual_inputs)

    def _start_worker(self, *, visual=False):
        _take_prepared_visual()
        _visual_warmup.discard_array_stage()
        self.worker = PreviewWorker(self.target, self.cutter, self.level, self.scene, visual=visual)

    @staticmethod
    def _proxy(context, graph, source, name):
        evaluated = source.evaluated_get(graph)
        mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=graph)
        proxy = bpy.data.objects.new(name, mesh)
        proxy.matrix_world = evaluated.matrix_world.copy()
        proxy.color = source.color
        proxy.hide_select = True
        proxy.hide_render = True
        try:
            context.scene.collection.objects.link(proxy)
        except Exception:
            _drop_proxy(proxy)
            raise
        return proxy

    @property
    def status(self):
        axis = f"  Axis {self.axis}" if self.axis else ""
        number = f"  {self.numeric}" if self.numeric else ""
        if self.visual_active:
            pending = "GPU preview"
        else:
            pending = "Updating cuts" if self.mesh_sequence != self.sequence else "Cuts updated"
        return f"Boolean Plus  |  Preview L{self.level}  |  {pending}{axis}{number}  |  Enter to confirm    Esc to cancel"

    def refresh(self):
        self.refreshed_at = monotonic()
        if self.inputs_changed:
            raise ValueError("The source mesh settings changed during the move.")
        if self.visual_requested:
            self._refresh_visual()
        waiting_for_draw = (self.visual_data is not None and self.visual_handler is not None and
                            not self.visual_failed and _supports_visual_preview(self.window, self.area))
        if self.worker is None and not (self.visual_active or waiting_for_draw):
            self._start_worker()
        if self.worker is not None:
            self._refresh_mesh()
        self.area.header_text_set(self.status)

    def _refresh_mesh(self):
        if self.sent != self.sequence:
            self.worker.request(self.sequence, self.location)
            self.sent = self.sequence
        answer = self.worker.poll()
        if answer is not None:
            sequence, mesh = answer
            if sequence >= self.mesh_sequence:
                old_mesh = self.target_proxy.data
                for material in old_mesh.materials:
                    if material is not None:
                        mesh.materials.append(material)
                self.target_proxy.data = mesh
                if old_mesh.users == 0:
                    bpy.data.meshes.remove(old_mesh)
                self.mesh_sequence = sequence
            elif mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            self.area.tag_redraw()

    def _refresh_visual(self):
        if self.visual_failed or not _supports_visual_preview(self.window, self.area):
            if self.visual_active:
                self.target_proxy.hide_set(False, view_layer=self.layer)
                self.cutter_proxy.display_type = "WIRE"
                self.visual_active = False
                self.area.tag_redraw()
            return
        if self.visual_ready and not self.visual_active:
            self.target_proxy.hide_set(True, view_layer=self.layer)
            self.cutter_proxy.display_type = "BOUNDS"
            self.visual_active = True
            self.area.tag_redraw()
        elif self.visual_handler is None and self.worker is not None and (path := self.worker.visual_path) is not None:
            import numpy as np

            try:
                with np.load(path, allow_pickle=False) as data:
                    self.visual_data = dict(data)
            except (OSError, ValueError) as error:
                print(f"Boolean Plus GPU preview unavailable: {error}", flush=True)
                self.visual_failed = True
                return
            self._add_visual_handler()

    def _add_visual_handler(self):
        self.visual_handler = bpy.types.SpaceView3D.draw_handler_add(self._draw_visual, (), "WINDOW", "POST_VIEW")
        self.area.tag_redraw()

    def _draw_visual(self):
        global _cached_visual
        context = bpy.context
        if (self.visual_failed or self.inputs_changed or context.window != self.window or context.area != self.area or
                context.region != self.region or context.region_data is None or
                not _supports_visual_preview(self.window, self.area)):
            return
        try:
            if self.visual_data is not None:
                from .visual_draw import DifferenceDisplay

                self.visual_display = DifferenceDisplay(self.visual_data, surface=self.visual_surface)
                self.visual_data = None
            self.visual_display.draw_in_view(context.region_data, self.location)
            if not self.visual_ready and self.visual_inputs is not None and not self.inputs_changed:
                _remember_display(self.visual_inputs, self.visual_display)
                _visual_warmup.want_standby = True
            self.visual_ready = True
            if self.visual_active:
                self.visual_frames += 1
                self.visual_sequence = self.sequence
                if self.first_visual_at is None:
                    self.first_visual_at = monotonic()
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            if _cached_visual is not None and _cached_visual[1] is self.visual_display:
                _cached_visual = None
            print(f"Boolean Plus GPU preview unavailable: {error}", flush=True)
            self.visual_failed = True

    def mouse(self, event):
        cursor = Vector((event.mouse_x - self.region.x, event.mouse_y - self.region.y))
        if self.anchor is None:
            if not (0 <= cursor.x < self.region.width and 0 <= cursor.y < self.region.height):
                return
            self.anchor = cursor.copy()
        axis = _axes.get(self.axis)
        offset = _mouse_offset(self.region, self.area.spaces.active.region_3d,
                               cursor, self.anchor, self.world, axis, self.plane)
        if self.numeric:
            try:
                amount = float(self.numeric.replace(",", "."))
            except ValueError:
                return
            if not isfinite(amount):
                return
            direction = axis if axis is not None and not self.plane else offset.normalized()
            offset = direction * (amount * self.distance_scale)
        elif event.ctrl:
            step = (0.1 if event.shift else 1.0) * self.distance_scale
            offset = Vector(tuple(round(value / step) * step for value in offset))
        location = self.start + offset
        for index, locked in enumerate(self.cutter.lock_location):
            if locked:
                location[index] = self.start[index]
        if (location - self.location).length_squared > 1e-12:
            self.location = location
            self.sequence += 1
            self.cutter_proxy.location = self.world + self.location - self.start
            self.area.tag_redraw()

    def close(self, confirm=False):
        def restore_active():
            self.layer.objects.active = self.active

        # Each cleanup still runs if Blender has already invalidated a different
        # RNA reference, for example during a workspace or file change.
        with ExitStack() as cleanup:
            cleanup.callback(self._cleanup, lambda: self.area.tag_redraw())
            cleanup.callback(self._cleanup, lambda: self.area.header_text_set(None))
            cleanup.callback(self._cleanup, restore_active)
            for obj in self.original_selection:
                cleanup.callback(self._cleanup, lambda obj=obj: obj.select_set(True, view_layer=self.layer))
            if confirm:
                self.cutter.location = self.location
            for obj, hidden in self.hidden:
                cleanup.callback(self._cleanup,
                                 lambda obj=obj, hidden=hidden: obj.hide_set(hidden, view_layer=self.layer))
            for obj in (self.target_proxy, self.cutter_proxy):
                if obj is not None:
                    cleanup.callback(self._cleanup, _drop_proxy, obj)
            self.target_proxy = self.cutter_proxy = None
            if self.timer is not None:
                cleanup.callback(self._cleanup, self.manager.event_timer_remove, self.timer)
                self.timer = None
            if self.worker is not None:
                cleanup.callback(self._cleanup, _visual_warmup.retire, self.worker)
                self.worker = None
            if self.visual_handler is not None:
                cleanup.callback(self._cleanup, bpy.types.SpaceView3D.draw_handler_remove,
                                 self.visual_handler, "WINDOW")
                self.visual_handler = None
        self.visual_display = self.visual_data = self.visual_surface = None

    @staticmethod
    def _cleanup(callback, *args, **kwargs):
        with suppress(ReferenceError, RuntimeError, OSError):
            callback(*args, **kwargs)


class _ArraySession(_DragSession):
    def __init__(self, context, area, region, target, cutter, array_index):
        from .array_stage import array_counts, array_modifiers

        self.array_names = array_modifiers(cutter)
        if not 0 <= array_index < len(self.array_names):
            raise ValueError("This Array does not support interactive count preview.")
        self.array_index = array_index
        self.original_counts = array_counts(cutter, self.array_names)
        self.counts = self.requested_counts = self.original_counts
        self.stage = None
        self._array_source = None
        self.digits = ""
        self.message = ""
        self.confirm_requested = False
        super().__init__(context, area, region, target, cutter)

    def _prepare_visual(self, graph):
        if _cached_visual is not None and _cached_visual[0] == self.visual_inputs:
            self._array_source = _take_prepared_visual(self.visual_inputs)
        super()._prepare_visual(graph)

    @property
    def status(self):
        fields = "    ".join(f"{name}: {count}" if index == self.array_index else f"{name} {count}"
                             for index, (name, count) in enumerate(zip(self.array_names, self.requested_counts)))
        pending = "Preparing cuts    " if not self.ready_to_confirm else ""
        message = f"{self.message}    " if self.message else ""
        return f"{fields}    |    {message}{pending}Up/Down or type count    Tab: next array    Enter to confirm    Esc to cancel"

    @property
    def ready_to_confirm(self):
        return (self.visual_active and self.requested_counts == self.counts and
                self.visual_sequence == self.sequence)

    def request_count(self, count):
        values = list(self.requested_counts)
        values[self.array_index] = max(1, count)
        self.requested_counts = tuple(values)
        self.message = ""

    def refresh(self):
        if (self.window.scene != self.scene or not self.manager.boolean_plus_visual_preview or self.visual_failed or
                not _supports_visual_preview(self.window, self.area)):
            raise ValueError("Array preview requires Solid shading with X-Ray off.")
        if self.worker is not None and self.worker.visual_unavailable:
            raise ValueError("GPU array preview is unavailable for this mesh.")
        super().refresh()
        if not self.visual_active:
            return
        if not self.cutter_proxy.hide_get(view_layer=self.layer):
            self.cutter_proxy.hide_set(True, view_layer=self.layer)
        if self.stage is None:
            self.stage = _visual_warmup.take_array_stage(self.target, self.cutter, self.scene)
        if self.stage is None:
            if _visual_warmup.retiring is not None and not _visual_warmup.retiring.stopped:
                return
            from .array_stage import ArrayStage

            source = self._array_source or self.worker
            self._array_source = self.worker = None
            try:
                self.stage = ArrayStage(self.target, self.cutter, self.scene, source=source)
            finally:
                if self.stage is None and source is not None:
                    source.close()
        if self.requested_counts == self.counts:
            return
        try:
            self.stage.submit(self.requested_counts)
            result = self.stage.poll()
        except ValueError as error:
            self.message = str(error)
            self.requested_counts = self.counts
            self.confirm_requested = False
        else:
            if result is None:
                return
            self.counts, self.visual_data = result
            self.visual_surface = self.visual_display.surface
            self.sequence += 1
            self.area.tag_redraw()
        self.area.header_text_set(self.status)

    def close(self, confirm=False):
        with ExitStack() as cleanup:
            cleanup.callback(super().close)
            if self._array_source is not None:
                cleanup.callback(_visual_warmup.retire, self._array_source)
                self._array_source = None
            if self.stage is not None:
                if confirm:
                    cleanup.callback(_visual_warmup.retire, self.stage)
                    _visual_warmup.want_standby = True
                    self.stage.commit()
                elif not self.stage.stopped and not self.inputs_changed:
                    _visual_warmup.array_stage = self.stage
                else:
                    cleanup.callback(_visual_warmup.retire, self.stage)
                self.stage = None


def _finish(confirm=False):
    global _session
    active, _session = _session, None
    if active is not None:
        active.close(confirm)
        _visual_warmup.schedule()


@persistent
def _abort_preview(*_args):
    global _cached_visual, _cached_surface
    _finish()
    _visual_warmup.reset()
    _cached_visual = None
    _cached_surface = None


@persistent
def _visual_frame_changed(scene, _depsgraph):
    if _session is None and _visual_warmup.pair is not None and scene == _visual_warmup.pair[1]:
        _visual_warmup.schedule()


def _visual_setting_changed(_manager, _context):
    _visual_warmup.schedule(0.0)


class BOOLEANPLUS_OT_fast_drag(bpy.types.Operator):
    bl_idname = "boolean_plus.fast_drag"
    bl_label = "Move Cutter with Preview"
    bl_description = "Move the cutter with a live preview and evaluate the full stack on confirmation"
    bl_options = {"BLOCKING", "UNDO"}

    from_keymap: bpy.props.BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return context.window is not None and _viewport(context) is not None and _pair(context) is not None

    def invoke(self, context, event):
        global _session
        selected = _pair(context, self.from_keymap)
        viewport = _viewport(context)
        if selected is None or viewport is None:
            return {"PASS_THROUGH"} if self.from_keymap else {"CANCELLED"}
        try:
            target, cutter, modifier = selected
            target.modifiers.active = modifier
            _session = _DragSession(context, *viewport, target, cutter)
            self._drag = _session
            _session.mouse(event)
        except (OSError, RuntimeError, ValueError) as error:
            _finish()
            self.report({"WARNING"}, f"Boolean Plus: {error}")
            return {"PASS_THROUGH"} if self.from_keymap else {"CANCELLED"}
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if _session is None or getattr(self, "_drag", None) is not _session:
            return {"CANCELLED"}
        try:
            if _session.inputs_changed:
                raise ValueError("The source mesh settings changed during the move.")
            if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
                _finish()
                return {"CANCELLED"}
            if event.type in {"LEFTMOUSE", "RET", "NUMPAD_ENTER"} and event.value == "PRESS":
                changed = _session.location != _session.start
                _finish(confirm=changed)
                return {"FINISHED"} if changed else {"CANCELLED"}
            if event.type == "TIMER" and monotonic() - _session.refreshed_at >= 0.02:
                _session.refresh()
                if _session.worker is not None and _session.worker.error:
                    self.report({"WARNING"}, _session.worker.error)
                    _finish()
                    return {"CANCELLED"}
            elif event.type == "MOUSEMOVE":
                _session.mouse(event)
            elif event.value == "PRESS":
                if event.type in _axes:
                    _session.axis = event.type if _session.axis != event.type else None
                    _session.plane = event.shift
                elif event.type == "BACK_SPACE":
                    _session.numeric = _session.numeric[:-1]
                elif event.unicode and event.unicode in "0123456789.,-" and len(_session.numeric) < 20:
                    _session.numeric += event.unicode
                else:
                    return {"RUNNING_MODAL"}
                _session.mouse(event)
        except (OSError, ReferenceError, RuntimeError, ValueError) as error:
            _finish()
            self.report({"WARNING"}, f"Boolean Plus: {error}")
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def cancel(self, _context):
        if getattr(self, "_drag", None) is _session:
            _finish()


class BOOLEANPLUS_OT_adjust_array(bpy.types.Operator):
    bl_idname = "boolean_plus.adjust_array"
    bl_label = "Adjust Array Count"
    bl_description = "Preview the number of cuts and press Enter to evaluate the full stack"
    bl_options = {"BLOCKING", "UNDO"}

    array_index: bpy.props.IntProperty(min=0, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        from . import operation_of
        from .array_stage import array_modifiers

        if context.window is None or not context.window_manager.boolean_plus_visual_preview:
            return False
        viewport = _viewport(context)
        operands = _pair(context)
        if viewport is None or operands is None or not _supports_visual_preview(context.window, viewport[0]):
            return False
        return operation_of(operands[2]) == "DIFFERENCE" and bool(array_modifiers(operands[1]))

    def invoke(self, context, _event):
        global _session
        area, region = _viewport(context)
        target, cutter, modifier = _pair(context)
        try:
            target.modifiers.active = modifier
            _session = _ArraySession(context, area, region, target, cutter, self.array_index)
            self._adjustment = _session
        except (OSError, ReferenceError, RuntimeError, ValueError) as error:
            _finish()
            self.report({"WARNING"}, str(error))
            return {"CANCELLED"}
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        session = getattr(self, "_adjustment", None)
        if session is None or session is not _session:
            return {"CANCELLED"}
        try:
            if event.type == "TIMER" and monotonic() - session.refreshed_at >= 0.02:
                session.refresh()
                if session.worker is not None and session.worker.error:
                    raise ValueError(session.worker.error)
            elif event.value == "PRESS":
                if event.type in {"ESC", "RIGHTMOUSE"}:
                    _finish()
                    return {"CANCELLED"}
                if event.type in {"RET", "NUMPAD_ENTER"}:
                    session.confirm_requested = True
                elif event.type == "TAB":
                    session.array_index = (session.array_index + 1) % len(session.array_names)
                    session.digits = ""
                elif event.type in {"UP_ARROW", "DOWN_ARROW", "WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
                    step = 1 if event.type in {"UP_ARROW", "WHEELUPMOUSE"} else -1
                    session.digits = ""
                    session.request_count(session.requested_counts[session.array_index] + step)
                elif event.type == "BACK_SPACE":
                    session.digits = session.digits[:-1]
                    if session.digits:
                        session.request_count(int(session.digits))
                elif event.unicode in "0123456789" and event.unicode and len(session.digits) < 5:
                    session.digits += event.unicode
                    session.request_count(int(session.digits))
            if session.confirm_requested and (session.ready_to_confirm or session.requested_counts == session.original_counts):
                changed = session.ready_to_confirm and session.counts != session.original_counts
                _finish(confirm=changed)
                return {"FINISHED"} if changed else {"CANCELLED"}
        except (OSError, ReferenceError, RuntimeError, ValueError) as error:
            _finish()
            self.report({"WARNING"}, str(error))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def cancel(self, _context):
        if getattr(self, "_adjustment", None) is _session:
            _finish()


def register():
    from .edit_drag import BOOLEANPLUS_OT_move_edit_vertices

    bpy.utils.register_class(BOOLEANPLUS_OT_fast_drag)
    bpy.utils.register_class(BOOLEANPLUS_OT_adjust_array)
    bpy.utils.register_class(BOOLEANPLUS_OT_move_edit_vertices)
    bpy.types.WindowManager.boolean_plus_fast_g = bpy.props.BoolProperty(
        name="Use Preview for G", default=False, options={"SKIP_SAVE"},
        description="Preview cuts when moving a supported Boolean Plus cutter with G")
    bpy.types.WindowManager.boolean_plus_preview_level = bpy.props.IntProperty(
        name="Preview Level", default=0, min=0, max=2, options={"SKIP_SAVE"},
        update=_visual_setting_changed,
        description="Maximum subdivision level for background previews")
    bpy.types.WindowManager.boolean_plus_visual_preview = bpy.props.BoolProperty(
        name="GPU Cut Preview", default=False, options={"SKIP_SAVE"},
        update=_visual_setting_changed,
        description="Update cuts on the GPU while moving supported cutters")
    for name in ("load_pre", "save_pre", "undo_pre", "redo_pre", "render_pre"):
        getattr(bpy.app.handlers, name).append(_abort_preview)
    bpy.app.handlers.depsgraph_update_post.append(_invalidate_visual_inputs)
    bpy.app.handlers.frame_change_post.append(_visual_frame_changed)
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is not None:
        keymap = keyconfig.keymaps.new(name="Object Mode", space_type="EMPTY")
        item = keymap.keymap_items.new(BOOLEANPLUS_OT_fast_drag.bl_idname, "G", "PRESS", head=True)
        item.properties.from_keymap = True
        _keymaps.append((keymap, item))


def unregister():
    from .edit_drag import BOOLEANPLUS_OT_move_edit_vertices

    _abort_preview()
    for keymap, item in _keymaps:
        keymap.keymap_items.remove(item)
    _keymaps.clear()
    for name in ("load_pre", "save_pre", "undo_pre", "redo_pre", "render_pre"):
        getattr(bpy.app.handlers, name).remove(_abort_preview)
    bpy.app.handlers.depsgraph_update_post.remove(_invalidate_visual_inputs)
    bpy.app.handlers.frame_change_post.remove(_visual_frame_changed)
    if bpy.app.timers.is_registered(_visual_warmup.timer):
        bpy.app.timers.unregister(_visual_warmup.timer)
    del bpy.types.WindowManager.boolean_plus_preview_level
    del bpy.types.WindowManager.boolean_plus_fast_g
    del bpy.types.WindowManager.boolean_plus_visual_preview
    bpy.utils.unregister_class(BOOLEANPLUS_OT_move_edit_vertices)
    bpy.utils.unregister_class(BOOLEANPLUS_OT_adjust_array)
    bpy.utils.unregister_class(BOOLEANPLUS_OT_fast_drag)
