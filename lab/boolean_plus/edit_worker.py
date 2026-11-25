"""Keep native Boolean evaluation of staged Edit Mode geometry off the main thread."""

from array import array
from hashlib import sha256
from math import isfinite, prod
from struct import Struct
from time import monotonic, perf_counter

import bpy
import numpy as np

from . import cutter_of, find_modifier, input_control, operation_of
from .array_stage import _count_input, _set_transform_group
from .drag_worker import (PreviewWorker, _close_process, _load_preview_objects, _publish_settings,
                          _replace_preview, _start_process)
from .visual_geometry import (_MAX_CUTTER_ISLANDS, _MAX_TRIANGLES, _capture, _cutter_mesh_arrays,
                              _cutter_tables, _validate_rigid_stacks, build_bvh, requires_separated_cutters)


_SEQUENCE = Struct("=Q")
_TIMEOUT = 15.0
_IDLE_TIMEOUT = 60.0


def _topology(vertices, edges, faces):
    signature = sha256(array("I", (vertices, len(edges), len(faces))))
    for ends in sorted(tuple(sorted(edge)) for edge in edges):
        signature.update(array("I", ends))
    for face in faces:
        # Preserve winding and ordered vertex identity. The first corner of a
        # polygon may change when BMesh is converted to a standalone Mesh.
        first = face.index(min(face))
        signature.update(array("I", (len(face), *face[first:], *face[:first])))
    return signature.hexdigest()


def edit_topology(bm):
    """Return the topology in the vertex order used for staged coordinates."""
    bm.verts.index_update()
    edges = [tuple(vertex.index for vertex in edge.verts) for edge in bm.edges]
    faces = [tuple(vertex.index for vertex in face.verts) for face in bm.faces]
    return _topology(len(bm.verts), edges, faces)


def _mesh_topology(mesh):
    return _topology(len(mesh.vertices), [tuple(edge.vertices) for edge in mesh.edges],
                     [tuple(face.vertices) for face in mesh.polygons])


class EditPreviewWorker:
    """Send complete coordinate buffers and return previews in increasing sequence order."""

    def __init__(self, target, cutter, scene, bm, *, surface_cached=False, prefer_nonconvex=False):
        boolean = find_modifier(target)
        if (cutter.mode != "EDIT" or not cutter.is_editable or cutter.data.shape_keys is not None or
                target.data == cutter.data or boolean is None or not boolean.show_viewport or
                operation_of(boolean) != "DIFFERENCE" or cutter_of(boolean) != cutter or
                not PreviewWorker.eligible(target, cutter, scene, allow_edit_cutter=True)):
            raise ValueError("Select a separate, editable cutter and use the Difference operation.")
        modifiers = _validate_rigid_stacks(target, cutter, boolean)
        if any(not modifier.show_in_editmode for modifier in modifiers):
            raise ValueError("Enable Show in Edit Mode on each active Array on the cutter.")
        copies = prod(_count_input(modifier)[1].value for modifier in modifiers)
        triangles = sum(len(face.verts) - 2 for face in bm.faces)
        if (not 0 < copies <= _MAX_CUTTER_ISLANDS or not 0 < triangles * copies <= _MAX_TRIANGLES or
                max(len(bm.verts), len(bm.edges)) * copies > 3 * _MAX_TRIANGLES):
            raise ValueError("This cutter exceeds the geometry limits for interactive preview.")
        target_world, cutter_world = target.matrix_world, cutter.matrix_world
        if min(target_world.to_3x3().determinant(), cutter_world.to_3x3().determinant()) <= 0:
            raise ValueError("Interactive editing requires transforms with a positive, nonzero determinant.")
        relative = target_world.inverted() @ cutter_world
        if not all(isfinite(value) for row in relative for value in row):
            raise ValueError("The cutter transform contains invalid values.")

        self.topology = edit_topology(bm)
        self._vertices = len(bm.verts)
        self._requested = self._received = self._unpublished = -1
        self._last_result = None
        self._closed = False
        self._progress_at = monotonic()
        self._directory, self._process = PreviewWorker._claim_process()
        try:
            bpy.data.libraries.write(str(self._directory / "inputs.blend"), {target, cutter}, path_remap="ABSOLUTE")
            # Library serialization of an Edit Mode object writes its stored
            # mesh. Export the live BMesh separately so unsaved changes to
            # faces, vertex order and custom data are visible in the helper.
            snapshot = bpy.data.meshes.new("Boolean Plus Edit Snapshot")
            try:
                bm.to_mesh(snapshot)
                if _mesh_topology(snapshot) != self.topology:
                    raise ValueError("The cutter vertex order changed while the preview was loading.")
                bpy.data.libraries.write(str(self._directory / "edit-input.blend"), {snapshot},
                                         path_remap="ABSOLUTE")
                mesh_name = snapshot.name
            finally:
                bpy.data.meshes.remove(snapshot)
            settings = {
                "edit_stage": True, "target": target.name, "cutter": cutter.name, "mesh": mesh_name,
                "relative": [tuple(row) for row in relative], "origin": tuple(cutter.location),
                "topology": self.topology, "vertex_count": self._vertices,
                "surface_cached": surface_cached, "prefer_nonconvex": prefer_nonconvex,
                "separated": requires_separated_cutters(boolean),
                "frame": scene.frame_current, "subframe": scene.frame_subframe,
                "fps": scene.render.fps, "fps_base": scene.render.fps_base,
            }
            _publish_settings(self._directory, settings)
            if self._process is None:
                self._process = _start_process(self._directory)
            self._progress_at = monotonic()
        except Exception:
            self.close()
            raise

    @property
    def stopped(self):
        return self._process is None or self._process.poll() is not None

    def _check_running(self):
        if self.stopped:
            diagnostic = self._directory / "error.txt"
            if diagnostic.is_file():
                print(f"Boolean Plus Edit: {diagnostic.read_text(encoding='utf-8')}", flush=True)
            raise RuntimeError("The vertex preview process stopped unexpectedly.")

    @property
    def ready(self):
        if self._closed:
            return False
        self._check_running()
        ready = (self._directory / "edit-ready").is_file()
        if not ready and monotonic() - self._progress_at > _TIMEOUT:
            raise RuntimeError("The vertex preview process could not start.")
        return ready

    @property
    def surface_path(self):
        path = self._directory / "edit-surface.npz"
        return path if not self._closed and path.is_file() else None

    @property
    def surface_unavailable(self):
        return not self._closed and (self._directory / "edit-surface-unavailable.txt").is_file()

    def _publish_request(self):
        if self._unpublished >= 0:
            try:
                (self._directory / "edit-request.pending").replace(self._directory / "edit-request.bin")
            except PermissionError:
                return  # The helper is reading the last request on Windows. Retry next poll.
            self._unpublished = -1

    def submit(self, sequence, coords):
        if self._closed:
            raise RuntimeError("Vertex preview has closed.")
        self._check_running()
        if type(sequence) is not int or not self._requested < sequence < 2 ** 64:
            raise ValueError("The preview request arrived out of order.")
        coords = np.asarray(coords, dtype=np.float32)
        if coords.shape != (self._vertices, 3) or not np.all(np.isfinite(coords)):
            raise ValueError("Preview coordinates must match the current cutter mesh.")
        if self._requested == self._received:
            self._progress_at = monotonic()
        with (self._directory / "edit-request.pending").open("wb") as destination:
            destination.write(_SEQUENCE.pack(sequence))
            destination.write(coords.tobytes(order="C"))
        self._requested = self._unpublished = sequence
        self._publish_request()

    def poll(self):
        if self._closed:
            return None
        self._check_running()
        self._publish_request()
        if self._received == self._requested:
            return None
        response = self._directory / "edit.npz"
        result = None
        try:
            state = response.stat()
            stamp = state.st_mtime_ns, state.st_size
            if stamp != self._last_result:
                with np.load(response, allow_pickle=False) as archive:
                    seq = int(archive["sequence"].item())
                    if not self._received < seq <= self._requested:
                        raise ValueError("The vertex preview returned an unexpected response number.")
                    tables = {name: archive[name] for name in archive.files
                              if name not in {"sequence", "worker_compute_ms"}}
                    result = seq, tables, float(archive["worker_compute_ms"].item())
                self._received = seq
                self._last_result = stamp
                self._progress_at = monotonic()
        except (FileNotFoundError, PermissionError):
            pass
        if monotonic() - self._progress_at > _TIMEOUT:
            raise RuntimeError("The vertex preview process did not respond.")
        if result is not None and "error" in result[1]:
            raise ValueError(str(result[1]["error"].item()))
        return result

    def close(self):
        if not self._closed:
            self._closed = True
            _close_process(self._directory, self._process)


def _write_surface(directory, target, cutter, scene, settings, stopped):
    try:
        base, faces, visible, _, _, matrix, _ = _capture(target, cutter, scene, settings)
        bvh, bvh_faces = build_bvh(base, faces, validate_solid=settings["separated"])
        pending = directory / "edit-surface.pending"
        with pending.open("wb") as stream:
            np.savez(stream, target_positions=base, target_triangles=faces[visible], target_bvh=bvh,
                     target_bvh_triangles=bvh_faces, target_matrix_world=matrix)
        _replace_preview(pending, directory / "edit-surface.npz", stopped)
        return True
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        (directory / "edit-surface-unavailable.txt").write_text(str(error), encoding="utf-8")
        return False


def _fallback_tables(layer, target):
    layer.update()
    evaluated = target.evaluated_get(layer.depsgraph)
    mesh = evaluated.data
    count = len(mesh.loop_triangles)
    if count > _MAX_TRIANGLES or len(mesh.vertices) > 3 * _MAX_TRIANGLES:
        raise ValueError("The preview mesh exceeds the supported geometry limit.")
    positions = np.empty((len(mesh.vertices), 3), dtype=np.float32)
    triangles = np.empty((count, 3), dtype=np.int32)
    mesh.vertices.foreach_get("co", positions.ravel())
    mesh.loop_triangles.foreach_get("vertices", triangles.ravel())
    if not np.all(np.isfinite(positions)):
        raise ValueError("The preview mesh contains invalid coordinates.")
    return {"fallback_positions": positions, "fallback_triangles": triangles,
            "target_matrix_world": np.asarray(evaluated.matrix_world, dtype=np.float32)}


def _edited_cutter_tables(mesh, origin, source_vertices, *, separated, prefer_nonconvex):
    from .nonconvex import nonconvex_cutter_tables

    coords, corners = _cutter_mesh_arrays(mesh)
    methods = _cutter_tables, nonconvex_cutter_tables
    if prefer_nonconvex:
        methods = methods[::-1]
    try:
        return methods[0](coords, corners, origin, source_vertices, separated=separated)
    except ValueError:
        return methods[1](coords, corners, origin, source_vertices, separated=separated)


def serve_edit(directory, stopped, settings):
    """Evaluate the newest cutter shape, using a reduced native mesh outside the GPU budget."""
    from mathutils import Matrix

    target, cutter = _load_preview_objects(directory, [settings["target"], settings["cutter"]])
    with bpy.data.libraries.load(str(directory / "edit-input.blend"), link=False) as (_, loaded):
        loaded.meshes = [settings["mesh"]]
    mesh, = loaded.meshes
    if mesh is None or _mesh_topology(mesh) != settings["topology"]:
        raise RuntimeError("The preview cutter topology differs from the current mesh.")
    cutter.data = mesh
    scene, layer = bpy.context.scene, bpy.context.view_layer
    scene.render.fps = settings["fps"]
    scene.render.fps_base = settings["fps_base"]
    for modifier in target.modifiers:
        if modifier.type == "SUBSURF" and modifier.show_viewport:
            modifier.levels = 0
    has_surface = settings["surface_cached"] or _write_surface(directory, target, cutter, scene, settings, stopped)

    # The normal layer only evaluates the cutter. A separate dependency graph
    # can evaluate the reduced Boolean when the shader cannot represent
    # the edit, with no scene relinking or GPU tables rebuilt per request.
    collection = bpy.data.collections.new("Boolean Plus Edit Result")
    scene.collection.children.link(collection)
    collection.objects.link(target)
    scene.collection.objects.unlink(target)
    layer.layer_collection.children[collection.name].exclude = True
    input_control(find_modifier(target), "Solver").value = "Auto"
    probe = cutter.copy()
    scene.collection.objects.link(probe)
    transform = bpy.data.node_groups.new("Boolean Plus Edit Transform", "GeometryNodeTree")
    _set_transform_group(transform, Matrix(settings["relative"]))
    probe.modifiers.new("Boolean Plus Edit Transform", "NODES").node_group = transform
    scene.frame_set(settings["frame"], subframe=settings["subframe"])
    fallback_layer = scene.view_layers.new("Boolean Plus Edit Result")
    # New view layers may inherit the active collection state.
    fallback_layer.layer_collection.children[collection.name].exclude = False

    width = 3 * settings["vertex_count"]
    record_size = _SEQUENCE.size + np.dtype(np.float32).itemsize * width
    origin = np.asarray(settings["origin"], dtype="f4")
    prefer_nonconvex = settings["prefer_nonconvex"]
    consumed = -1
    idle_deadline = monotonic() + _IDLE_TIMEOUT
    request_path, staged = directory / "edit-request.bin", directory / "edit.pending"
    (directory / "edit-ready").touch()
    while not stopped.is_set():
        try:
            with request_path.open("rb") as source:
                record = source.read(record_size)
            sequence, = _SEQUENCE.unpack_from(record)
        except (FileNotFoundError, PermissionError):
            sequence = consumed
        if sequence == consumed:
            if monotonic() >= idle_deadline:
                return
            stopped.wait(0.005)
            continue
        consumed = sequence
        coords = np.frombuffer(record, dtype=np.float32, offset=_SEQUENCE.size, count=width)
        started = perf_counter()
        mesh.vertices.foreach_set("co", coords)
        mesh.update()
        tables = None
        if has_surface:
            try:
                layer.update()
                evaluated = probe.evaluated_get(layer.depsgraph)
                tables = _edited_cutter_tables(evaluated.data, origin, settings["vertex_count"],
                                               separated=settings["separated"],
                                               prefer_nonconvex=prefer_nonconvex)
                prefer_nonconvex = "cutter_bvh" in tables
            except ValueError:
                pass
        if tables is None:
            try:
                tables = _fallback_tables(fallback_layer, target)
            except (RuntimeError, ValueError) as error:
                tables = {"error": str(error)}
        elapsed = (perf_counter() - started) * 1000
        with staged.open("wb") as output:
            np.savez(output, sequence=np.uint64(sequence), worker_compute_ms=elapsed, **tables)
        _replace_preview(staged, directory / "edit.npz", stopped)
        idle_deadline = monotonic() + _IDLE_TIMEOUT
