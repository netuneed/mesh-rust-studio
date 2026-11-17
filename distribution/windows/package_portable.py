"""Assemble local Windows Work and Dev validation packages from a supplied runtime."""
import argparse
from hashlib import file_digest
from pathlib import Path
import shutil
import stat
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[2]
PRODUCT = "Mesh Rust Studio"
CATALOG = "25805c48-34ec-4434-aaef-1f13a71c0169"


def copy_runtime_file(source, target):
    info = source.lstat()
    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or not source.is_file():
        raise ValueError(f"Expected a regular packaged file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def copy_runtime_tree(source, target):
    if source.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Reparse points are unsupported in runtime input: {source}")
    for path in source.iterdir():
        name = path.name.casefold()
        if name in {"__pycache__", ".git", "config", "profiles", "presets-local"} or path.suffix in {".pyc", ".pdb"}:
            continue
        if path.is_dir():
            copy_runtime_tree(path, target / path.name)
        else:
            copy_runtime_file(path, target / path.name)


def package(runtime, directory, launcher, asset, edition):
    directory.mkdir()
    for path in runtime.iterdir():
        if path.name.casefold() == "blender.exe" or path.suffix.casefold() == ".dll":
            copy_runtime_file(path, directory / path.name)
    for relative in ("blender.shared", "license", "5.3/datafiles", "5.3/extensions", "5.3/python", "5.3/scripts"):
        source = runtime / relative
        if source.exists():
            copy_runtime_tree(source, directory / relative)

    for relative in ("blender.exe", "5.3/scripts/startup", "license"):
        if not (directory / relative).exists():
            raise ValueError(f"Runtime did not provide {relative}")

    copy_runtime_file(launcher, directory / (PRODUCT + ".exe"))
    (directory / "edition.txt").write_text(edition + "\n", encoding="ascii")
    startup = directory / "5.3/scripts/startup"
    copy_runtime_file(ROOT / "lab/blender_rust_welcome.py", startup / "blender_rust_welcome.py")
    topology = startup / "blender_rust_topology.py"
    asset_root = directory / "5.3/datafiles/assets"
    if edition == "work":
        topology.write_text((ROOT / "lab/blender_rust_topology.py").read_text(encoding="utf-8")
                            .replace("Blender+ RUST", PRODUCT), encoding="utf-8")
        copy_runtime_file(asset, asset_root / "nodes/mesh_rust_studio_auto_uv.blend")
    if (asset_root / "nodes/blender_rust_auto_uv.blend").exists():
        (asset_root / "nodes/blender_rust_auto_uv.blend").unlink()
    if edition == "dev" and topology.exists():
        topology.unlink()
    catalog = asset_root / "blender_assets.cats.txt"
    original = catalog.read_text(encoding="utf-8")
    entries = [line for line in original.splitlines() if not line.startswith(CATALOG + ":")]
    if edition == "work":
        entries.append(f"{CATALOG}:{PRODUCT}:{PRODUCT}")
    catalog.write_text("\n".join(entries) + "\n", encoding="utf-8")
    for name in ("README.md", "README.ru.md", "SECURITY.md", "BUILDING.md"):
        copy_runtime_file(ROOT / name, directory / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path, help="Clean Windows 5.3 runtime to validate")
    parser.add_argument("asset", type=Path, help="Verified Auto UV.blend from the asset library")
    parser.add_argument("output", type=Path, help="Fresh output directory")
    parser.add_argument("--compiler", type=Path, required=True, help="Path to Microsoft csc.exe")
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    output = args.output.resolve()
    if output.is_relative_to(runtime) or runtime.is_relative_to(output):
        parser.error("Runtime and output must be separate")
    output.mkdir(parents=True)
    launcher = output / (PRODUCT + ".exe")
    subprocess.run([args.compiler.resolve(strict=True), "/nologo", "/target:winexe", "/optimize+",
                    f"/out:{launcher}", "/reference:System.Windows.Forms.dll",
                    ROOT / "distribution/windows/PublicLauncher.cs"], check=True)
    sums = []
    for edition in ("work", "dev"):
        name = f"MeshRustStudio-{edition.title()}-Windows-x64"
        directory = output / name
        package(runtime, directory, launcher, args.asset.resolve(strict=True), edition)
        zip_path = output / f"{name}.zip"
        with ZipFile(zip_path, "x", compression=ZIP_DEFLATED, compresslevel=1) as archive:
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    archive.write(path, Path(name) / path.relative_to(directory))
        if zip_path.stat().st_size >= 2 * 1024**3:
            raise ValueError(f"GitHub release assets must be smaller than 2 GiB: {zip_path}")
        with zip_path.open("rb") as storage:
            sums.append(f"{file_digest(storage, 'sha256').hexdigest()}  {zip_path.name}")
    (output / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="ascii")


if __name__ == "__main__":
    main()
