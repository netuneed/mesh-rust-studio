"""Build the replacement runtime files for Mesh Rust Studio on Windows."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
from urllib.request import urlopen
import zipfile

from build_xml_modules import build_xml_modules
from build_tls_libraries import build_tls_libraries


DLL_NAMES = {
    "avcodec-62.dll", "avdevice-62.dll", "avfilter-11.dll", "avformat-62.dll",
    "avutil-60.dll", "swresample-6.dll", "swscale-9.dll",
}
REMOVED_SITE_ROOTS = {"_distutils_hack", "distutils-precedence.pth", "pkg_resources"}


def checked_artifact(lock, cache):
    path = cache / lock["filename"]
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".incoming")
        with urlopen(lock["url"], timeout=120) as response, temporary.open("xb") as output:
            shutil.copyfileobj(response, output)
        with temporary.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != lock["sha256"]:
                raise ValueError(f"Downloaded hash mismatch for {path.name}")
        temporary.rename(path)
    with path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != lock["sha256"]:
            raise ValueError(f"Cached hash mismatch for {path.name}")
    return path


def safe_parts(name):
    rel = PurePosixPath(name)
    if not rel.parts or rel.is_absolute() or "\\" in name or ":" in name or ".." in rel.parts:
        raise ValueError(f"Unexpected archive path {name}")
    return rel.parts


def write_stream(target, stream):
    target.parent.mkdir(parents=True, exist_ok=True)
    with stream, target.open("xb") as output:
        shutil.copyfileobj(stream, output)


def replace_python(runtime, output, archive):
    original = runtime / "5.3/python"
    destination = output / "5.3/python"
    included_stdlib = {path.name.lower() for path in (original / "lib").iterdir() if path.is_dir()}
    included_dlls = {path.name.lower() for path in (original / "DLLs").iterdir()}
    included_dlls.update({"libssl-3-x64.dll", "libcrypto-3-x64.dll", "zlib1.dll"})
    with tarfile.open(archive) as files:
        for member in files:
            parts = safe_parts(member.name)
            if parts[0] != "python" or len(parts) < 2 or not member.isfile():
                continue
            _, name, *rest = parts
            if "__pycache__" in rest or member.name.endswith(".pyc"):
                continue
            if name in {"python3.dll", "python313.dll"}:
                write_stream(output / name, files.extractfile(member))
                target = destination / "bin" / name
            elif name in {"python.exe", "vcruntime140.dll", "vcruntime140_1.dll"}:
                target = destination / "bin" / name
            elif name == "LICENSE.txt":
                target = output / "license/runtime-updates/CPython-LICENSE.txt"
            elif name == "DLLs" and len(rest) == 1 and rest[0].lower() in included_dlls:
                target = destination / "DLLs" / rest[0]
            elif name == "Lib" and rest and rest[0] != "site-packages":
                if len(rest) > 1 and rest[0].lower() not in included_stdlib:
                    continue
                target = destination / "lib" / Path(*rest)
            else:
                continue
            write_stream(target, files.extractfile(member))
    for name in ("python3.dll", "python313.dll"):
        if not (output / name).is_file():
            raise ValueError(f"Missing CPython DLL: {name}")


def keep_site_package(name, names):
    if name in REMOVED_SITE_ROOTS or name in names or name == "__pycache__":
        return False
    if name.endswith((".dist-info", ".egg-info")) and name.split("-")[0] in names:
        return False
    return True


def copy_checked(source, destination):
    attrs = getattr(source.lstat(), "st_file_attributes", 0)
    if source.is_symlink() or attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Unexpected link in runtime: {source}")
    if source.is_dir():
        destination.mkdir()
        for child in source.iterdir():
            if child.name != "__pycache__" and child.suffix.lower() not in {".pyc", ".pdb"}:
                copy_checked(child, destination / child.name)
    elif source.is_file():
        shutil.copyfile(source, destination)
    else:
        raise ValueError(f"Unexpected runtime file type: {source}")


def update_site(runtime, output, wheels):
    relative = Path("5.3/python/lib/site-packages")
    destination = output / relative
    destination.mkdir(parents=True)
    for child in (runtime / relative).iterdir():
        if keep_site_package(child.name, wheels):
            copy_checked(child, destination / child.name)
    for path in wheels.values():
        with zipfile.ZipFile(path) as files:
            for member in files.infolist():
                parts = safe_parts(member.filename)
                if member.is_dir():
                    continue
                if ".data" in parts[0]:
                    raise ValueError(f"Unsupported wheel layout in {path.name}")
                write_stream(destination / Path(*parts), files.open(member))


def replace_ffmpeg(output, archive):
    found = set()
    with zipfile.ZipFile(archive) as files:
        for member in files.infolist():
            parts = safe_parts(member.filename)
            if len(parts) == 3 and parts[1] == "bin" and parts[2] in DLL_NAMES:
                found.add(parts[2])
                write_stream(output / "blender.shared" / parts[2], files.open(member))
            elif parts[1:] == ("LICENSE.txt",):
                write_stream(output / "license/runtime-updates/FFmpeg-LICENSE.txt", files.open(member))
    if DLL_NAMES != found:
        raise ValueError(f"Missing FFmpeg runtime DLLs: {DLL_NAMES - found}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path, help="Original Blender Windows runtime")
    parser.add_argument("output", type=Path, help="New, empty overlay directory")
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    locks = json.loads(Path(__file__).with_name("runtime-lock.json").read_text(encoding="utf-8"))
    python = checked_artifact(locks["python"], args.cache)
    ffmpeg = checked_artifact(locks["ffmpeg"], args.cache)
    wheels = {key: checked_artifact(lock, args.cache) for key, lock in locks["wheels"].items()}
    py_source = checked_artifact(locks["cpython_source"], args.cache)
    expat = checked_artifact(locks["expat_windows"], args.cache)
    openssl = checked_artifact(locks["openssl_source"], args.cache)
    args.output.mkdir(parents=True)
    replace_python(args.runtime, args.output, python)
    update_site(args.runtime, args.output, wheels)
    legacy = args.output / "5.3/python/lib/site-packages/pkg_resources.py"
    shutil.copyfile(Path(__file__).with_name("pkg_resources.py"), legacy)
    build_xml_modules(py_source, python, expat, args.output / "5.3/python/DLLs")
    build_tls_libraries(openssl, args.output / "5.3/python/DLLs")
    replace_ffmpeg(args.output, ffmpeg)
    notice_root = args.output / "license/runtime-updates"
    with zipfile.ZipFile(Path(__file__).with_name("runtime-notices.zip")) as notices:
        for member in notices.infolist():
            if not member.is_dir():
                write_stream(notice_root / Path(*safe_parts(member.filename)), notices.open(member))


if __name__ == "__main__":
    main()
