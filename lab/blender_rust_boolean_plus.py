"""Register the Work package's Boolean Plus and CAD tools in Blender."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import addon_utils
import bpy
from bpy.app.handlers import persistent


_boolean_plus = None
_cad = None


def _load_module(name, path):
    installed = sys.modules.get(name)
    if installed is not None:
        if Path(installed.__file__).resolve() != path.resolve():
            raise ImportError(f"Conflicting Work module {name!r}")
        return installed

    spec = spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load packaged Work module {path}")
    module = module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _addon_provides(name):
    preferences = bpy.context.preferences
    if name in sys.modules or any(addon.module.rsplit(".", 1)[-1] == name for addon in preferences.addons):
        return True
    if any((Path(directory) / f"{name}.py").is_file() or
           (Path(directory) / name / "__init__.py").is_file() for directory in addon_utils.paths()):
        return True
    return any(repo.directory and (Path(repo.directory) / name / "blender_manifest.toml").is_file()
               for repo in preferences.extensions.repos)


def _load_cad(root):
    plate = _load_module("_blender_rust_work_cad_plate", root / "cad_plate.py")
    # CAD's standalone entry point uses an absolute import of its sibling.
    previous = sys.modules.get("cad_plate")
    sys.modules["cad_plate"] = plate
    try:
        return _load_module("_blender_rust_work_cad_workspace", root / "cad_workspace.py")
    finally:
        if previous is None:
            del sys.modules["cad_plate"]
        else:
            sys.modules["cad_plate"] = previous


@persistent
def _set_session_defaults(*_args):
    manager = bpy.context.window_manager
    if manager is not None and not manager.is_property_set("boolean_plus_visual_preview"):
        manager.boolean_plus_visual_preview = True


def register():
    global _boolean_plus, _cad
    if bpy.app.background or _boolean_plus is not None or _cad is not None:
        return

    bundled = Path(bpy.app.binary_path).parent / "blenderplus"
    boolean_package = bundled / "boolean_plus" / "__init__.py"
    cad_package = bundled / "cad"
    if not boolean_package.is_file() or not (cad_package / "cad-workspace.blend").is_file():
        raise RuntimeError(f"Work Boolean Plus or CAD resources are missing under {bundled}")

    try:
        # Leave the operator namespace to any existing installed add-on.
        if not _addon_provides("boolean_plus") and not hasattr(bpy.types, "BOOLEANPLUS_OT_fast_drag"):
            module = _load_module("_blender_rust_work_boolean_plus", boolean_package)
            module.register()
            _boolean_plus = module
            _set_session_defaults()
            bpy.app.handlers.load_post.append(_set_session_defaults)
        if not _addon_provides("cad_workspace") and not hasattr(bpy.types, "VIEW3D_MT_rust_cad"):
            module = _load_cad(cad_package)
            module.register()
            _cad = module
    except (ImportError, OSError, RuntimeError, ValueError):
        unregister()
        raise

    if _boolean_plus is not None and _cad is not None:
        version = ".".join(map(str, _boolean_plus.bl_info["version"]))
        print(f"Mesh Rust Studio Work tools ready (Boolean Plus {version}, CAD registered)", flush=True)


def unregister():
    global _boolean_plus, _cad
    if _cad is not None:
        cad, _cad = _cad, None
        cad.unregister()
    if _boolean_plus is not None:
        if _set_session_defaults in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.remove(_set_session_defaults)
        module, _boolean_plus = _boolean_plus, None
        module.unregister()
