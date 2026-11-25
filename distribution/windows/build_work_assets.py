# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the two Work libraries from an empty factory profile."""

from pathlib import Path
import sys

import bpy


if not bpy.app.background:
    raise RuntimeError("Run in background mode with --factory-startup")
output = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
output.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lab"))
from auto_uv import create_auto_uv
from cad_workspace import write_workspace_library

bpy.data.libraries.write(
    str(output / "mesh_rust_studio_auto_uv.blend"), {create_auto_uv()}
)
write_workspace_library(output / "cad-workspace.blend")
