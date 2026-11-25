"""Evaluate Boolean Plus drag previews in a separate local Blender process."""

from array import array
from functools import lru_cache
from hashlib import file_digest
from math import isfinite
from pathlib import Path
from struct import Struct
from threading import Event, Thread
from time import monotonic, sleep
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile

import bpy


_REQUEST = Struct("=Q3d")
_HEADER = Struct("=4sQ4I")
_MAGIC = b"BPD1"
_IDLE_SECONDS = 120
_MESH_FIELDS = (
    ("position", "POINT", "FLOAT_VECTOR", "f", 3, "vector"),
    (".edge_verts", "EDGE", "INT32_2D", "i", 2, "value"),
    (".corner_vert", "CORNER", "INT", "i", 1, "value"),
    (".corner_edge", "CORNER", "INT", "i", 1, "value"),
    ("sharp_edge", "EDGE", "BOOLEAN", "b", 1, "value"),
    ("sharp_face", "FACE", "BOOLEAN", "b", 1, "value"),
    ("material_index", "FACE", "INT", "i", 1, "value"),
)
_STATIC_MODIFIERS = {"ARRAY", "BEVEL", "BOOLEAN", "DECIMATE", "EDGE_SPLIT", "MASK", "MIRROR", "NODES",
                     "SCREW", "SOLIDIFY", "SUBSURF", "TRIANGULATE", "WELD", "WEIGHTED_NORMAL"}
_SNAPSHOT_IDS = {"ACTION", "IMAGE", "KEY", "MATERIAL", "MESH", "NODETREE", "OBJECT", "TEXTURE"}
_UNSUPPORTED_NODES = {"GeometryNodeBake", "GeometryNodeInputActiveCamera", "GeometryNodeSimulationInput",
                      "GeometryNodeSimulationOutput"}


def _snapshot_dependencies(objects):
    references = {}
    pending = list(objects)
    dependencies = set(objects)
    for dependency, users in bpy.data.user_map().items():
        for user in users:
            references.setdefault(user, []).append(dependency)
    for item in pending:
        new = set(references.get(item, ())) - dependencies
        dependencies.update(new)
        pending.extend(new)
    return dependencies


def _file_stamp(path):
    info = path.stat()
    return info.st_size, info.st_mtime_ns


@lru_cache(maxsize=8)
def _digest(path, stamp):
    with path.open("rb") as source:
        return file_digest(source, "sha256").digest()


def _bundled_node_library(library):
    """Allow the R11 arrays linked to an identical Blender Essentials asset."""
    source = Path(bpy.path.abspath(library.filepath, library=library.parent))
    asset = Path("assets/nodes/geometry_nodes_essentials.blend")
    bundled = Path(bpy.utils.system_resource("DATAFILES")) / asset
    if source == bundled:
        return True
    package = Path(bpy.app.binary_path).parent
    packages = package.parent
    if not source.is_relative_to(packages):
        return False
    try:
        package_name, *relative = source.relative_to(packages).parts
        if not package_name.startswith("5.3-work-") or Path(*relative) != bundled.relative_to(package):
            return False
        # The source scene can keep its links to a sibling Work installation.
        if not source.resolve(strict=True).is_relative_to(packages.resolve(strict=True)):
            return False
        return _digest(source, _file_stamp(source)) == _digest(bundled, _file_stamp(bundled))
    except (OSError, ValueError):
        return False


def _start_process(directory):
    binary = Path(bpy.app.binary_path).resolve(strict=True)
    script = Path(__file__).resolve(strict=True)
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("BLENDER_", "PYTHON"))}
    environment["BLENDER_USER_RESOURCES"] = str(directory / "profile")
    for name in ("TMP", "TEMP", "TMPDIR"):
        environment[name] = str(directory)
    return subprocess.Popen(
        [str(binary), "--background", "--factory-startup", "--disable-autoexec", "--offline-mode",
         "--threads", "4", "--python-exit-code", "12", "--python", str(script), "--", str(directory)],
        cwd=binary.parent,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _publish_settings(directory, settings, name="settings"):
    staged = directory / f"{name}.pending"
    staged.write_text(json.dumps(settings), encoding="utf-8")
    staged.replace(directory / f"{name}.json")


def _close_process(directory, process):
    if process is None:
        shutil.rmtree(directory, ignore_errors=True)
        return
    # Leave CPU time to the host if it is about to calculate the final mesh.
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    process.stdin.close()
    deadline = monotonic() + 5

    def clean_at_exit():
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return
        shutil.rmtree(directory, ignore_errors=True)

    def reap():
        if process.poll() is not None:
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                atexit.unregister(clean_at_exit)
                return None
        return 0.1 if monotonic() < deadline else None

    if reap() is not None:
        atexit.register(clean_at_exit)
        bpy.app.timers.register(reap, first_interval=0.1, persistent=True)


class PreviewWorker:
    """Own a snapshot and a replaceable stream of target-local preview meshes."""

    _standby = None

    @classmethod
    def cancel_prewarm(cls):
        standby, cls._standby = cls._standby, None
        if standby is not None:
            directory, process, _ = standby
            process.stdin.close()
            if process.poll() is not None:
                shutil.rmtree(directory, ignore_errors=True)

    @classmethod
    def prewarm(cls, *, visual=False):
        """Start one idle process with no scene data for the next preview session."""
        if cls._standby is not None:
            directory, process, _ = cls._standby
            if process.poll() is None:
                return
            process.stdin.close()
            shutil.rmtree(directory, ignore_errors=True)
            cls._standby = None
        directory = Path(tempfile.mkdtemp(prefix="boolean-plus-preview-"))
        try:
            if visual:
                (directory / "warm-visual").touch()
            cls._standby = directory, _start_process(directory), monotonic()
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @classmethod
    def _claim_process(cls):
        standby, cls._standby = cls._standby, None
        if standby is not None:
            directory, process, started = standby
            if process.poll() is None and monotonic() - started < _IDLE_SECONDS - 5:
                try:
                    (directory / "claimed").touch()
                    return directory, process
                except OSError:
                    pass
            process.stdin.close()
            if process.poll() is not None:
                shutil.rmtree(directory, ignore_errors=True)
        return Path(tempfile.mkdtemp(prefix="boolean-plus-preview-")), None

    @staticmethod
    def eligible(target, cutter, scene, *, allow_edit_cutter=False):
        if target is None or cutter is None or target == cutter or scene.render.use_simplify:
            return False
        cutter_modes = {"OBJECT", "EDIT"} if allow_edit_cutter else {"OBJECT"}
        if target.mode != "OBJECT" or cutter.mode not in cutter_modes:
            return False
        objects = {target, cutter}
        if any(obj.type != "MESH" or obj.parent or obj.constraints
               or obj.is_instancer or obj.hide_viewport for obj in objects):
            return False
        if cutter.animation_data is not None:
            return False
        if any(mod.type not in _STATIC_MODIFIERS for obj in objects for mod in obj.modifiers if mod.show_viewport):
            return False
        for item in _snapshot_dependencies(objects):
            geometry_nodes = isinstance(item, bpy.types.GeometryNodeTree)
            if item.library and not (geometry_nodes and _bundled_node_library(item.library)):
                return False
            if item.override_library or item.id_type not in _SNAPSHOT_IDS:
                return False
            if item.id_type == "OBJECT" and item not in objects:
                return False
            if item.id_type == "IMAGE" and item.source != "GENERATED" and not item.packed_file:
                return False
            animation = getattr(item, "animation_data", None)
            if animation is not None and animation.drivers:
                return False
            if geometry_nodes and any(node.bl_idname in _UNSUPPORTED_NODES or
                                      node.bl_idname.startswith("GeometryNodeImport") for node in item.nodes):
                return False
        return bool(bpy.app.binary_path)

    def __init__(self, target, cutter, level, scene, *, visual=False):
        if not self.eligible(target, cutter, scene):
            raise ValueError("The target and cutter cannot be evaluated in an isolated preview.")
        self.ready = False
        self.error = None
        self._received = -1
        self._requested = -1
        self._last_result = None
        self._closed = False
        self._requests = None
        self._directory, self._process = self._claim_process()
        try:
            directory = self._directory
            settings = {
                "target": target.name,
                "cutter": cutter.name,
                "level": int(level),
                "frame": scene.frame_current,
                "subframe": scene.frame_subframe,
                "fps": scene.render.fps,
                "fps_base": scene.render.fps_base,
                "visual": bool(visual),
            }
            bpy.data.libraries.write(str(directory / "inputs.blend"), {target, cutter}, path_remap="ABSOLUTE")
            self._requests = (directory / "requests.bin").open("wb", buffering=0)
            _publish_settings(directory, settings)
            if self._process is None:
                self._process = _start_process(directory)
        except Exception:
            self.close()
            raise

    @property
    def visual_path(self):
        if self._closed:
            return None
        path = self._directory / "visual.npz"
        return path if path.is_file() else None

    @property
    def visual_unavailable(self):
        return not self._closed and (self._directory / "visual-unavailable.txt").is_file()

    @property
    def stopped(self):
        return self._process is None or self._process.poll() is not None

    def request(self, seq, cutter_location):
        if self._closed or self.error is not None or seq <= self._requested:
            return
        location = tuple(cutter_location)
        if len(location) != 3 or not all(map(isfinite, location)):
            raise ValueError("The cutter preview needs a finite three-dimensional location.")
        request = _REQUEST.pack(seq, *location)
        if self._requests.write(request) != len(request):
            raise OSError("Writing the Boolean Plus preview location failed.")
        self._requested = seq

    def release(self):
        """Hand the loaded snapshot and its process to the Array stage."""
        if self._closed or self.stopped:
            raise RuntimeError("The Blender preview snapshot is no longer available.")
        self._requests.close()
        self._closed = True
        return self._directory, self._process

    def poll(self):
        if self._closed or self.error is not None:
            return None
        directory = self._directory
        try:
            if not self.ready:
                self.ready = (directory / "ready").is_file()
            code = self._process.poll()
            if code is not None:
                error_path = directory / "error.txt"
                self.error = (error_path.read_text(encoding="utf-8") if error_path.is_file()
                              else f"The Blender preview process stopped ({code}).")
                return None
            preview = directory / "preview.bin"
            try:
                state = preview.stat()
                stamp = state.st_mtime_ns, state.st_size
                if stamp == self._last_result:
                    return None
                with preview.open("rb") as source:
                    header = source.read(_HEADER.size)
                    if len(header) != _HEADER.size:
                        raise ValueError("The Blender preview is missing its mesh header.")
                    magic, seq, points, edges, corners, faces = _HEADER.unpack(header)
                    if magic != _MAGIC or seq > self._requested:
                        raise ValueError("The Blender preview contains an invalid mesh header.")
                    if seq <= self._received:
                        self._last_result = stamp
                        return None
                    mesh = _read_mesh(source, points, edges, corners, faces)
            except (FileNotFoundError, PermissionError):
                return None
        except (EOFError, OSError, RuntimeError, ValueError) as exc:
            self.error = str(exc)
            return None
        self._received = seq
        self._last_result = stamp
        return seq, mesh

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._requests is not None:
            self._requests.close()
        _close_process(self._directory, self._process)


def _read_mesh(source, points, edges, corners, faces):
    counts = {"POINT": points, "EDGE": edges, "CORNER": corners, "FACE": faces}
    size = _HEADER.size + faces * 4 + sum(counts[domain] * width * array(kind).itemsize
                                         for _, domain, _, kind, width, _ in _MESH_FIELDS)
    if os.fstat(source.fileno()).st_size != size:
        raise ValueError("The Blender preview mesh data is incomplete.")
    mesh = bpy.data.meshes.new("Boolean Plus Preview")
    try:
        mesh.vertices.add(points)
        mesh.edges.add(edges)
        mesh.loops.add(corners)
        mesh.polygons.add(faces)
        for name, domain, data_type, kind, width, prop in _MESH_FIELDS:
            count = counts[domain] * width
            if count:
                data = array(kind)
                data.fromfile(source, count)
                attribute = mesh.attributes.get(name)
                if attribute is None and any(data):
                    attribute = mesh.attributes.new(name, data_type, domain)
                if attribute is not None:
                    attribute.data.foreach_set(prop, data)
        if faces:
            starts = array("i")
            starts.fromfile(source, faces)
            mesh.polygons.foreach_set("loop_start", starts)
        mesh.update()
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise
    return mesh


def _write_mesh(directory, seq, mesh, stopped):
    temporary = directory / "writing.bin"
    points, edges, corners, faces = len(mesh.vertices), len(mesh.edges), len(mesh.loops), len(mesh.polygons)
    counts = {"POINT": points, "EDGE": edges, "CORNER": corners, "FACE": faces}
    with temporary.open("wb") as destination:
        destination.write(_HEADER.pack(_MAGIC, seq, points, edges, corners, faces))
        for name, domain, _, kind, width, prop in _MESH_FIELDS:
            data = array(kind, [0]) * (counts[domain] * width)
            attribute = mesh.attributes.get(name)
            if data and attribute is not None:
                attribute.data.foreach_get(prop, data)
            data.tofile(destination)
        if faces:
            starts = array("i", [0]) * faces
            mesh.polygons.foreach_get("loop_start", starts)
            starts.tofile(destination)
    _replace_preview(temporary, directory / "preview.bin", stopped)


def _replace_preview(temporary, destination, stopped):
    deadline = monotonic() + 2
    while not stopped.is_set():
        try:
            temporary.replace(destination)
            return
        except PermissionError:
            if monotonic() >= deadline:
                raise
            sleep(0.005)


def _visual_writer():
    if __package__:
        from .visual_geometry import write_snapshot
    else:
        package_root = str(Path(__file__).resolve().parents[1])
        if package_root not in sys.path:
            sys.path.insert(0, package_root)
        from boolean_plus.visual_geometry import write_snapshot
    return write_snapshot


def _write_visual_preview(directory, target, cutter, scene, settings):
    try:
        _visual_writer()(directory, target, cutter, scene, settings)
    except Exception as exc:
        try:
            (directory / "visual-unavailable.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        except OSError:
            pass


def _load_preview_objects(directory, names):
    # Remove the factory objects so the snapshot's names and dependencies resolve unchanged.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    with bpy.data.libraries.load(str(directory / "inputs.blend"), link=False) as (_, loaded):
        loaded.objects = names
    scene = bpy.context.scene
    for obj in loaded.objects:
        scene.collection.objects.link(obj)
    return loaded.objects


def _array_runner():
    if __package__:
        from .array_stage import serve_array
    else:
        _visual_writer()  # Make the installed package importable in background Blender.
        from boolean_plus.array_stage import serve_array
    return serve_array


def _edit_runner():
    if __package__:
        from .edit_worker import serve_edit
    else:
        _visual_writer()
        from boolean_plus.edit_worker import serve_edit
    return serve_edit


def _serve(directory, stopped):
    settings = json.loads((directory / "settings.json").read_text(encoding="utf-8"))
    if settings.get("edit_stage"):
        _edit_runner()(directory, stopped, settings)
        return
    if settings.get("array_stage"):
        _array_runner()(directory, stopped, settings)
        return
    target, cutter = _load_preview_objects(directory, [settings["target"], settings["cutter"]])
    scene = bpy.context.scene
    scene.render.fps = settings["fps"]
    scene.render.fps_base = settings["fps_base"]
    for mod in target.modifiers:
        if mod.type == "SUBSURF" and mod.show_viewport:
            mod.levels = min(mod.levels, settings["level"])
    (directory / "ready").touch()
    if settings.get("visual") and not stopped.is_set():
        _write_visual_preview(directory, target, cutter, scene, settings)
    consumed = 0
    with (directory / "requests.bin").open("rb", buffering=0) as requests:
        while not stopped.is_set():
            stage = directory / "array-stage.json"
            if stage.is_file():
                scene.collection.objects.unlink(target)
                _array_runner()(directory, stopped, json.loads(stage.read_text(encoding="utf-8")), cutter=cutter)
                return
            available = os.fstat(requests.fileno()).st_size // _REQUEST.size
            if available == consumed:
                stopped.wait(0.008)
                continue
            requests.seek((available - 1) * _REQUEST.size)
            seq, x, y, z = _REQUEST.unpack(requests.read(_REQUEST.size))
            cutter.location = x, y, z
            if consumed == 0:
                scene.frame_set(settings["frame"], subframe=settings["subframe"])
            consumed = available
            graph = bpy.context.evaluated_depsgraph_get()
            _write_mesh(directory, seq, target.evaluated_get(graph).data, stopped)


def _run(directory):
    if not bpy.app.background or bpy.app.autoexec or bpy.app.online_access:
        raise RuntimeError("Run the Boolean Plus preview in an offline background Blender.")
    stopped = Event()

    def watch_parent():
        try:
            os.read(sys.stdin.fileno(), 1)
        finally:
            stopped.set()

    Thread(target=watch_parent, name="Boolean Plus parent monitor", daemon=True).start()
    settings = directory / "settings.json"
    deadline = monotonic() + _IDLE_SECONDS
    try:
        if (directory / "warm-visual").is_file() and not stopped.is_set():
            try:
                _visual_writer()
            except ImportError:
                pass
        (directory / "started").touch()
        while not stopped.is_set():
            if settings.is_file():
                _serve(directory, stopped)
                break
            if monotonic() >= deadline and not (directory / "claimed").exists():
                break
            stopped.wait(0.02)
    except Exception as exc:
        (directory / "error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        raise
    finally:
        if stopped.is_set() or not settings.exists():
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    _run(Path(sys.argv[sys.argv.index("--") + 1]).resolve(strict=True))
