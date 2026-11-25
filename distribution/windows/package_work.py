# SPDX-License-Identifier: GPL-2.0-or-later
"""Assemble Work from the pinned upstream archive, reviewed sources and a dependency overlay."""

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile

from map_build_paths import map_paths


ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "distribution/windows"
ARCHIVE_SHA = "3ac567a63d59d9934f7b9213bfb27ba05760e1fb366f8f55c381573ec3eabfa8"
ENGINE_FILES = (
    "blender_cpu_check.dll",
    "BlendThumb.dll",
    "blender.shared/bf_intern_draco_bridge.dll",
    "blender.shared/bf_intern_ktx_bridge.dll",
    "blender.shared/bf_intern_meshopt_bridge.dll",
    "blender.shared/blender.shared.manifest",
)
MODULES = (
    "__init__",
    "array_stage",
    "drag",
    "drag_worker",
    "edit_drag",
    "edit_worker",
    "nonconvex",
    "ui",
    "visual_draw",
    "visual_geometry",
    "visual_reuse",
)
AUTO_UV_CATALOG = "25805c48-34ec-4434-aaef-1f13a71c0169"


def copy(source, target):
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Expected regular file: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def assemble(args):
    with args.upstream.open("rb") as fp:
        if hashlib.file_digest(fp, "sha256").hexdigest() != ARCHIVE_SHA:
            raise ValueError("Unexpected upstream archive")
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    with ZipFile(args.upstream) as archive:
        for info in archive.infolist():
            prefix, sep, rel = info.filename.partition("/")
            if not sep or info.is_dir():
                continue
            if prefix != "blender-5.3.0-windows-x64" or ".." in Path(rel).parts:
                raise ValueError("Unexpected archive member")
            if rel.startswith("blender.crt/") and rel.lower().endswith(".dll"):
                # This engine uses the application-local CRT search path.
                rel = Path(rel).name
            elif (
                not rel.startswith(
                    (
                        "blender.shared/",
                        "license/",
                        "5.3/datafiles/",
                        "5.3/extensions/",
                        "5.3/scripts/",
                    )
                )
                and rel != "copyright.txt"
            ):
                continue
            target = out / rel
            if (
                target.suffix.lower() in (".pdb", ".pyc")
                or "__pycache__" in target.parts
            ):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as stream, target.open("wb") as fp:
                shutil.copyfileobj(stream, fp)
    for rel in ENGINE_FILES:
        copy(args.runtime / rel, out / rel)
    copy(args.engine, out / "blender.exe")
    for binary in [
        out / "blender.exe",
        *(out / v for v in ENGINE_FILES if v.lower().endswith(".dll")),
    ]:
        map_paths(binary, args.map_root)
    for rel in ("OPEN_SUBDIV_LICENSE.txt", "OPEN_SUBDIV_NOTICE.txt"):
        copy(ROOT / "ports/blender-screen-split" / rel, out / "license" / rel)
    for overlay in args.overlay:
        for path in sorted(overlay.rglob("*")):
            if path.is_symlink() or path.is_junction():
                raise ValueError("Overlay must be a regular directory tree")
            if path.is_file():
                rel = path.relative_to(overlay)
                if not (
                    rel.as_posix().startswith(("5.3/python/", "blender.shared/", "license/"))
                    or rel.as_posix() in {"python3.dll", "python313.dll"}
                ):
                    raise ValueError(f"Unexpected overlay path {rel}")
                if "__pycache__" in rel.parts or path.suffix.lower() in {".pyc", ".pdb"}:
                    raise ValueError(f"Unexpected build artifact {rel}")
                copy(path, out / rel)
    if (
        not (out / "5.3/python/bin/python.exe").is_file()
        or not (out / "python313.dll").is_file()
    ):
        raise ValueError("Missing Windows Python runtime")
    startup = out / "5.3/scripts/startup"
    for name in (
        "blender_rust_welcome.py",
        "blender_rust_boolean_plus.py",
        "blender_rust_topology.py",
    ):
        copy(ROOT / "lab" / name, startup / name)
    for module in MODULES:
        copy(
            ROOT / "lab/boolean_plus" / (module + ".py"),
            out / "blenderplus/boolean_plus" / (module + ".py"),
        )
    copy(
        ROOT / "lab/boolean_plus/README.md", out / "blenderplus/boolean_plus/README.md"
    )
    for name in ("cad_plate.py", "cad_workspace.py"):
        copy(ROOT / "lab" / name, out / "blenderplus/cad" / name)
    copy(
        args.assets / "cad-workspace.blend", out / "blenderplus/cad/cad-workspace.blend"
    )
    lib = out / "5.3/datafiles/assets"
    copy(
        args.assets / "mesh_rust_studio_auto_uv.blend",
        lib / "nodes/mesh_rust_studio_auto_uv.blend",
    )
    catalog = lib / "blender_assets.cats.txt"
    rows = [
        line
        for line in catalog.read_text("utf-8").splitlines()
        if not line.startswith(AUTO_UV_CATALOG + ":")
    ]
    rows.append(AUTO_UV_CATALOG + ":Mesh Rust Studio:Mesh Rust Studio")
    catalog.write_text("\n".join(rows) + "\n", encoding="utf-8")
    (out / "edition.txt").write_text("work\n", encoding="ascii")
    copy(DIST / "MeshRustStudio-splash.png", out / "MeshRustStudio-splash.png")
    subprocess.run(
        [
            str(args.compiler),
            "/nologo",
            "/target:winexe",
            "/optimize+",
            "/reference:System.Windows.Forms.dll",
            f"/win32icon:{DIST / 'MeshRustStudio.ico'}",
            f"/out:{out / 'Mesh Rust Studio.exe'}",
            str(DIST / "PublicLauncher.cs"),
        ],
        check=True,
    )
    for name in ("README.md", "README.ru.md", "BUILDING.md", "SECURITY.md"):
        copy(ROOT / name, out / name)
    copy(ROOT / "docs/boolean-plus.png", out / "docs/boolean-plus.png")
    copy(DIST / "runtime/RUNTIME.md", out / "distribution/windows/runtime/RUNTIME.md")
    copy(ROOT / "LICENSE", out / "license/MeshRustStudio-GPL-2.0.txt")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("upstream", "runtime", "engine", "assets", "compiler", "output"):
        parser.add_argument("--" + arg, type=Path, required=True)
    parser.add_argument(
        "--overlay",
        type=Path,
        action="append",
        required=True,
        help="Apply a verified dependency overlay. Later overlays take precedence.",
    )
    parser.add_argument("--map-root", type=Path, action="append", default=[])
    parser.add_argument("--zip", action="store_true")
    args = parser.parse_args()
    assemble(args)
    if args.zip:
        archive = args.output.parent / (args.output.name + ".zip")
        with ZipFile(archive, "x", compression=ZIP_DEFLATED, compresslevel=6) as target:
            for path in sorted(args.output.rglob("*")):
                if path.is_file():
                    target.write(
                        path, Path(args.output.name) / path.relative_to(args.output)
                    )
        with archive.open("rb") as fp:
            print(hashlib.file_digest(fp, "sha256").hexdigest(), archive.name)


if __name__ == "__main__":
    main()
