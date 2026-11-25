"""Stage Array Count changes outside the editing process until confirmation."""

from math import isfinite, prod
import os
from struct import Struct
from time import monotonic

import bpy

from . import find_modifier


def _count_input(modifier):
    group = modifier.node_group if modifier.type == "NODES" else None
    if group is None:
        raise ValueError("Choose an Array modifier from Blender's asset library.")
    for item in group.interface.items_tree:
        if (item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == "Count" and
                item.socket_type == "NodeSocketInt"):
            prop = getattr(modifier.properties.inputs, item.identifier, None)
            if prop is not None and not getattr(prop, "use_attribute", False) and type(prop.value) is int:
                return item, prop
            break
    raise ValueError("Array preview requires a fixed integer Count.")


def array_modifiers(cutter):
    """List supported Array rows in modifier order."""
    if cutter is None or cutter.type != "MESH" or cutter.mode != "OBJECT" or not cutter.is_editable:
        return ()
    from .visual_geometry import _rigid_array_modifiers

    try:
        modifiers = _rigid_array_modifiers(cutter)
        for modifier in modifiers:
            _count_input(modifier)
    except (AttributeError, ValueError):
        return ()
    return tuple(modifier.name for modifier in modifiers)


def array_counts(cutter, names):
    return tuple(_count_input(cutter.modifiers[name])[1].value for name in names)


def _assign_counts(cutter, props, counts):
    original = tuple(prop.value for prop in props)
    if counts == original:
        return
    written = []
    try:
        for prop, before, after in zip(props, original, counts, strict=True):
            if before != after:
                prop.value = after
                written.append((prop, before))
        cutter.update_tag(refresh={"DATA"})
    except (RuntimeError, TypeError, ValueError):
        for prop, value in reversed(written):
            prop.value = value
        if written:
            cutter.update_tag(refresh={"DATA"})
        raise


def _set_transform_group(group, relative):
    # Transform Geometry uses the same native matrix transform as Object Info.
    # Putting it after Array also preserves Blender's post-transform tessellation.
    group.is_modifier = True
    for direction in ("INPUT", "OUTPUT"):
        group.interface.new_socket(name="Geometry", in_out=direction, socket_type="NodeSocketGeometry")
    incoming = group.nodes.new("NodeGroupInput")
    outgoing = group.nodes.new("NodeGroupOutput")
    transform = group.nodes.new("GeometryNodeTransform")
    transform.inputs["Mode"].default_value = "Matrix"
    matrix = group.nodes.new("FunctionNodeCombineMatrix")
    for column in range(4):
        for row in range(4):
            matrix.inputs[f"Column {column + 1} Row {row + 1}"].default_value = relative[row][column]
    group.links.new(incoming.outputs["Geometry"], transform.inputs["Geometry"])
    group.links.new(matrix.outputs["Matrix"], transform.inputs["Transform"])
    group.links.new(transform.outputs["Geometry"], outgoing.inputs["Geometry"])


class ArrayStage:
    """Evaluate native Arrays on one disposable cutter and commit confirmed counts."""

    def __init__(self, target, cutter, scene, *, source=None):
        from .drag_worker import PreviewWorker, _publish_settings, _start_process
        from .visual_geometry import _validate_rigid_stacks, requires_separated_cutters
        from .visual_reuse import visual_inputs

        if not cutter.is_editable or not PreviewWorker.eligible(target, cutter, scene):
            raise ValueError("This cutter cannot be isolated for Array preview.")
        modifier = find_modifier(target)
        modifiers = _validate_rigid_stacks(target, cutter, modifier)
        if not modifiers:
            raise ValueError("Enable an Array modifier with a Count input on the cutter.")
        self.names = tuple(modifier.name for modifier in modifiers)
        sockets, props = zip(*(_count_input(modifier) for modifier in modifiers), strict=True)
        self.original = self.counts = tuple(prop.value for prop in props)
        self._limits = tuple((max(1, socket.min_value), socket.max_value) for socket in sockets)
        self._vertices = len(cutter.data.vertices)
        self._edges = len(cutter.data.edges)
        self._triangles = len(cutter.data.loop_triangles)
        self._validate_counts(self.original)
        self._target, self._cutter, self._scene = target, cutter, scene
        self._source_inputs = visual_inputs(target, cutter, scene, 0)
        self._origin = tuple(cutter.location)
        target_world = target.matrix_world
        cutter_world = cutter.matrix_world
        if min(target_world.to_3x3().determinant(), cutter_world.to_3x3().determinant()) <= 0:
            raise ValueError("Array preview requires transforms with a positive, nonzero determinant.")
        relative = target_world.inverted() @ cutter_world
        if not all(isfinite(value) for row in relative for value in row):
            raise ValueError("The cutter transform contains invalid values.")

        self._request = Struct(f"=Q{len(self.names)}i")
        self._sequence = 0
        self._pending = None
        self._last_result = None
        self._sent_at = 0.0
        self._requests = None
        self._closed = False
        self._directory, self._process = source.release() if source is not None else PreviewWorker._claim_process()
        try:
            directory = self._directory
            settings = {"array_stage": True, "cutter": cutter.name, "names": self.names,
                        "relative": [tuple(row) for row in relative], "origin": self._origin,
                        "frame": scene.frame_current, "subframe": scene.frame_subframe,
                        "fps": scene.render.fps, "fps_base": scene.render.fps_base,
                        "separated": requires_separated_cutters(modifier)}
            if source is None:
                bpy.data.libraries.write(str(directory / "inputs.blend"), {cutter}, path_remap="ABSOLUTE")
            self._requests = (directory / "array-requests.bin").open("wb", buffering=0)
            _publish_settings(directory, settings, "settings" if source is None else "array-stage")
            if self._process is None:
                self._process = _start_process(directory)
        except Exception:
            self.close()
            raise

    def _validate_counts(self, counts):
        from .visual_geometry import _MAX_CUTTER_ISLANDS, _MAX_TRIANGLES

        if len(counts) != len(self._limits) or any(type(count) is not int or not low <= count <= high
                                                  for count, (low, high) in zip(counts, self._limits)):
            raise ValueError("Count is outside the range supported by this Array.")
        copies = prod(counts)
        if (copies > _MAX_CUTTER_ISLANDS or not 0 < self._triangles * copies <= _MAX_TRIANGLES or
                max(self._vertices, self._edges) * copies > 3 * _MAX_TRIANGLES):
            raise ValueError("Reduce the number of copies to use Array preview.")

    @property
    def stopped(self):
        return self._process is None or self._process.poll() is not None

    def matches(self, target, cutter, scene):
        from .visual_reuse import visual_inputs

        return (not self.stopped and not self._closed and target == self._target and cutter == self._cutter and
                scene == self._scene and tuple(cutter.location) == self._origin and
                visual_inputs(target, cutter, scene, 0) == self._source_inputs)

    def submit(self, counts):
        self._validate_counts(counts)
        if self._closed:
            raise RuntimeError("Array preview has closed.")
        if counts != self._pending:
            self._sequence += 1
            request = self._request.pack(self._sequence, *counts)
            if self._requests.write(request) != len(request):
                raise OSError("Could not send the new Count to the preview.")
            self._pending = counts
            self._sent_at = monotonic()

    def poll(self):
        if self._closed:
            return None
        if self.stopped:
            diagnostic = self._directory / "error.txt"
            if diagnostic.is_file():
                print(f"Boolean Plus Array: {diagnostic.read_text(encoding='utf-8')}", flush=True)
            raise RuntimeError("The Array preview process stopped unexpectedly.")
        if self._pending is None:
            return None
        if monotonic() - self._sent_at > 15:
            raise RuntimeError("The Array preview process did not respond.")
        import numpy as np

        response = self._directory / "array.npz"
        try:
            state = response.stat()
            stamp = state.st_mtime_ns, state.st_size
            if stamp == self._last_result:
                return None
            with np.load(response, allow_pickle=False) as archive:
                sequence = int(archive["sequence"])
                self._last_result = stamp
                if sequence != self._sequence:
                    return None  # A more recent edit superseded this result.
                counts, self._pending = self._pending, None
                if "error" in archive:
                    raise ValueError(str(archive["error"].item()))
                tables = {name: archive[name] for name in archive.files if name != "sequence"}
        except (FileNotFoundError, PermissionError):
            return None
        self.counts = counts
        return counts, tables

    def commit(self):
        from .visual_reuse import visual_inputs

        if self._closed:
            raise RuntimeError("Array preview has closed.")
        cutter = self._cutter
        if (not cutter.is_editable or tuple(cutter.location) != self._origin or
                visual_inputs(self._target, cutter, self._scene, 0) != self._source_inputs):
            raise ValueError("The cutter or its Boolean Plus stack changed during preview.")
        props = tuple(_count_input(cutter.modifiers[name])[1] for name in self.names)
        _assign_counts(cutter, props, self.counts)

    def close(self):
        from .drag_worker import _close_process

        if not self._closed:
            self._closed = True
            if self._requests is not None:
                self._requests.close()
            _close_process(self._directory, self._process)


def serve_array(directory, stopped, settings, *, cutter=None):
    """Run native Array and save GPU inputs for the latest complete request only."""
    import numpy as np
    from mathutils import Matrix
    from .drag_worker import _load_preview_objects, _replace_preview
    from .visual_geometry import cutter_mesh_tables

    if cutter is None:
        cutter, = _load_preview_objects(directory, [settings["cutter"]])
    scene = bpy.context.scene
    layer = bpy.context.view_layer
    scene.render.fps = settings["fps"]
    scene.render.fps_base = settings["fps_base"]
    cutter.location = settings["origin"]
    props = tuple(_count_input(cutter.modifiers[name])[1] for name in settings["names"])
    transform = bpy.data.node_groups.new("Boolean Plus Array Preview", "GeometryNodeTree")
    _set_transform_group(transform, Matrix(settings["relative"]))
    cutter.modifiers.new("Boolean Plus Array Preview", "NODES").node_group = transform
    scene.frame_set(settings["frame"], subframe=settings["subframe"])
    width = len(cutter.data.vertices)
    request = Struct(f"=Q{len(props)}i")
    consumed = 0
    staged = directory / "array.pending"
    (directory / "ready").touch()
    with (directory / "array-requests.bin").open("rb", buffering=0) as requests:
        while not stopped.is_set():
            available = os.fstat(requests.fileno()).st_size // request.size
            if available == consumed:
                stopped.wait(0.008)
                continue
            requests.seek((available - 1) * request.size)
            sequence, *counts = request.unpack(requests.read(request.size))
            consumed = available
            try:
                _assign_counts(cutter, props, tuple(counts))
                layer.update()
                evaluated = cutter.evaluated_get(layer.depsgraph)
                tables = cutter_mesh_tables(evaluated.data, settings["origin"], width, separated=settings["separated"])
            except ValueError as error:
                tables = {"error": str(error)}
            if os.fstat(requests.fileno()).st_size // request.size > consumed:
                continue
            with staged.open("wb") as output:
                np.savez(output, sequence=np.uint64(sequence), **tables)
            _replace_preview(staged, directory / "array.npz", stopped)
