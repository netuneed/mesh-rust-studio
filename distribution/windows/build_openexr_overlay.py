# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the OpenEXR 3.4 runtime overlay in a Windows x64 MSVC environment."""
import argparse
import hashlib
import shutil
import subprocess
import tarfile
from pathlib import Path

from map_build_paths import map_paths


VERSION = "3.4.19"
ARCHIVE_SHA256 = "6e9fd3de716a72382622867d9a46c7202cf62d8374efd02300a0c022cafebc56"
DLLS = ("Iex", "IlmThread", "OpenEXRCore", "OpenEXR", "OpenEXRUtil")


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def stage(install, source, pybind_lic, overlay, path_roots):
    overlay.mkdir(parents=True, exist_ok=False)
    for name in DLLS:
        dest = overlay / "blender.shared" / (name + ".dll")
        copy(install / "bin" / dest.name, dest)
        map_paths(dest, path_roots)
    binding = "OpenEXR.cp313-win_amd64.pyd"
    dest = overlay / "5.3/python/lib/site-packages" / binding
    copy(install / "python/OpenEXR" / binding, dest)
    map_paths(dest, path_roots)
    copy(install / "python/OpenEXR/Imath.py", dest.with_name("Imath.py"))
    for relative, name in (("LICENSE.md", "OpenEXR-3.4.19.txt"),
                           ("external/OpenJPH/LICENSE", "OpenJPH-OpenEXR.txt"),
                           ("external/deflate/COPYING", "libdeflate-OpenEXR.txt")):
        copy(source / relative, overlay / "license/runtime-updates" / name)
    copy(pybind_lic, overlay / "license/runtime-updates/pybind11-OpenEXR.txt")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("archive", "work", "overlay", "imath-dir", "pybind11-dir", "pybind11-license",
                "python-exe", "python-include", "python-library"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--map-root", type=Path, action="append", default=[])
    args = parser.parse_args()
    with args.archive.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != ARCHIVE_SHA256:
            raise ValueError("OpenEXR source checksum mismatch")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    with tarfile.open(args.archive) as archive:
        archive.extractall(work / "source", filter="data")
    src = work / "source" / ("openexr-" + VERSION)
    build, install = work / "build", work / "install"
    subprocess.run([
        "cmake", "-G", "Ninja", "-S", src, "-B", build, "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=ON", "-DBUILD_TESTING=OFF", "-DOPENEXR_BUILD_PYTHON=ON",
        "-DOPENEXR_TEST_PYTHON=OFF", "-DOPENEXR_BUILD_EXAMPLES=OFF",
        "-DOPENEXR_INSTALL_TOOLS=OFF", "-DOPENEXR_LIB_SUFFIX=",
        f"-DImath_DIR={args.imath_dir.resolve()}", f"-Dpybind11_DIR={args.pybind11_dir.resolve()}",
        f"-DPython3_EXECUTABLE={args.python_exe.resolve()}",
        f"-DPython3_INCLUDE_DIR={args.python_include.resolve()}",
        f"-DPython3_LIBRARY={args.python_library.resolve()}", f"-DCMAKE_INSTALL_PREFIX={install}",
    ], check=True)
    subprocess.run(["cmake", "--build", build, "--parallel"], check=True)
    subprocess.run(["cmake", "--install", build], check=True)
    stage(install, src, args.pybind11_license, args.overlay, [work, *args.map_root])


if __name__ == "__main__":
    main()
