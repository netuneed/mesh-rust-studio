# SPDX-License-Identifier: GPL-2.0-or-later
"""Check public setup with a generated source profile and a disposable target."""

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys


LAB = Path(__file__).resolve().parents[1] / "lab"
MANIFEST = """schema_version = "1.0.0"
id = "fixture_addon"
version = "1.0.0"
name = "Fixture Add-on"
tagline = "Check shared extension setup"
maintainer = "Fixture"
type = "add-on"
blender_version_min = "4.2.0"
license = ["SPDX:GPL-2.0-or-later"]
"""


def fixture_paths(output):
    roaming = output / "roaming"
    source = roaming / "Blender Foundation" / "Blender" / "5.3"
    return roaming, source, output / "target"


def build_fixture(bpy, output):
    preferences = bpy.context.preferences
    preferences.view.ui_scale = 1.33
    preferences.inputs.use_zoom_to_mouse = True
    preferences.inputs.mouse_double_click_time = 371
    preferences.edit.undo_steps = 49
    preferences.filepaths.save_version = 3
    preferences.filepaths.asset_libraries.new(name="Existing", directory=str(output / "assets"))
    preferences.extensions.repos.new(name="Installed", module="fixture_share",
                                    custom_directory=str(output / "installed"))
    assert bpy.ops.wm.save_userpref() == {"FINISHED"}


def check_import(bpy, output):
    sys.path.insert(0, str(LAB))
    import blender_rust_welcome as welcome

    _, source, _ = fixture_paths(output)
    assert welcome._latest_profile() == str(source)
    assert welcome._local_path(r"\\invalid.test\remote\asset") is None
    welcome.register()
    for menu in (bpy.types.WM_MT_splash_quick_setup, bpy.types.WM_MT_splash):
        assert welcome._draw_setup in menu._dyn_ui_initialize()
    assert bpy.ops.blender_rust.default_apps.poll()
    os.environ["BLENDER_RUST_PUBLIC_EDITION"] = "dev"
    assert not bpy.ops.blender_rust.default_apps.poll()

    for _ in range(2):
        assert bpy.ops.blender_rust.setup(
            source=str(source), attach_assets=True, attach_extensions=True) == {"FINISHED"}
    preferences = bpy.context.preferences
    assert abs(preferences.view.ui_scale - 1.33) < 1e-5
    assert preferences.inputs.use_zoom_to_mouse
    assert preferences.inputs.mouse_double_click_time == 371
    assert preferences.edit.undo_steps == 49
    assert preferences.filepaths.save_version == 3
    assert sum(Path(item.path) == output / "assets"
               for item in preferences.filepaths.asset_libraries) == 1
    attached = [item for item in preferences.extensions.repos if Path(item.directory) == output / "installed"]
    assert len(attached) == 1 and not attached[0].use_sync_on_startup and not attached[0].remote_url
    assert not any("fixture_addon" in addon.module for addon in preferences.addons)
    assert not (output / "FOREIGN_EXECUTED").exists()

    bad = output / "bad" / "config"
    bad.mkdir(parents=True)
    bad_preferences = bad / "userpref.blend"
    for contents in (b"corrupt", b"BLENDER-v503"):
        bad_preferences.write_bytes(contents)
        try:
            bpy.ops.blender_rust.setup(source=str(bad.parent))
        except RuntimeError:
            pass
        else:
            raise AssertionError("Invalid preferences accepted")
        assert preferences.inputs.mouse_double_click_time == 371

    welcome.unregister()
    print("PUBLIC_WELCOME_VERIFIED")


def inside(mode, output):
    import bpy

    assert bpy.app.version[:2] == (5, 3) and not bpy.app.autoexec and not bpy.app.online_access
    if mode == "fixture":
        build_fixture(bpy, output)
    else:
        check_import(bpy, output)


def run(executable, output, mode, resources):
    roaming, _, _ = fixture_paths(output)
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("BLENDER_", "PYTHON", "MESH_RUST_"))}
    env.update(BLENDER_RUST_PUBLIC="1", BLENDER_RUST_PUBLIC_EDITION="work",
               BLENDER_RUST_ORIGINAL_APPDATA=str(roaming), BLENDER_USER_RESOURCES=str(resources),
               BLENDER_RUST_PORT_CACHE_DIR=str(resources), TEMP=str(resources), TMP=str(resources),
               PYTHONDONTWRITEBYTECODE="1")
    command = [str(executable), "-b", "--factory-startup", "--disable-autoexec", "--offline-mode",
               "--python-exit-code", "1", "--python", __file__, "--", mode, str(output)]
    log = output / f"{mode}.log"
    with log.open("wb") as handle:
        result = subprocess.run(command, env=env, cwd=output, stdout=handle,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=90)
    if result.returncode:
        raise RuntimeError(f"Setup verification {mode} failed. See {log}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blender", type=Path, help="Blender 5.3 on Windows")
    parser.add_argument("--output", required=True, type=Path, help="New writable test directory")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True)
    _, source, target = fixture_paths(output)
    extension = output / "installed" / "fixture_addon"
    for path in (source / "config", target / "config", output / "assets", extension):
        path.mkdir(parents=True)
    (extension / "blender_manifest.toml").write_text(MANIFEST, encoding="utf-8")
    (extension / "__init__.py").write_text(
        "from pathlib import Path\nPath(__file__).resolve().parents[2].joinpath('FOREIGN_EXECUTED').touch()\n",
        encoding="utf-8")
    executable = args.blender.resolve(strict=True)
    run(executable, output, "fixture", source)
    original = source / "config" / "userpref.blend"
    digest = hashlib.sha256(original.read_bytes()).digest()
    run(executable, output, "import", target)
    assert hashlib.sha256(original.read_bytes()).digest() == digest
    assert (target / "config" / "userpref.blend").is_file()
    if "PUBLIC_WELCOME_VERIFIED" not in (output / "import.log").read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError("Setup verification marker missing")
    print(output / "import.log")


if __name__ == "__main__":
    if "--" in sys.argv:
        mode, output = sys.argv[sys.argv.index("--") + 1:]
        inside(mode, Path(output))
    else:
        main()
