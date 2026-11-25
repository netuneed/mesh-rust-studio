"""Rebuild CPython's two XML bindings with the matching Windows import libraries."""

from pathlib import Path
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import zipfile


def build_xml_modules(python_source, python_package, expat_package, target):
    with TemporaryDirectory(prefix="xml-bindings-") as temp:
        work = Path(temp)
        with tarfile.open(python_source) as archive:
            for member in archive:
                if not member.isfile():
                    continue
                parts = Path(member.name).parts
                if len(parts) < 3:
                    continue
                if parts[1] == "Include" or "/Modules/" in member.name and (
                    parts[-1] in {"pyexpat.c", "_elementtree.c", "expat_config.h"}
                    or parts[-1] in {"pyexpat.c.h", "_elementtree.c.h"}
                ):
                    archive.extract(member, work, filter="data")
        with tarfile.open(python_package) as archive:
            for name in ("python/include/pyconfig.h", "python/libs/python313.lib"):
                archive.extract(name, work, filter="data")
        with zipfile.ZipFile(expat_package) as archive:
            for name in ("Source/lib/expat.h", "Source/lib/expat_external.h", "Bin/libexpat.lib", "Bin/libexpat.dll"):
                archive.extract(name, work / "expat")
        pybase = next(child for child in work.iterdir() if child.name.startswith("Python-"))
        for module in ("pyexpat", "_elementtree"):
            subprocess.run([
                "cl", "/nologo", "/O2", "/MD", "/GS", "/guard:cf", "/LD", "/utf-8",
                "/DWIN32", "/DNDEBUG", "/DXML_DTD", "/DXML_NS", "/DXML_GE",
                "/Ipython/include", f"/I{pybase.name}/Include", f"/I{pybase.name}/Include/internal",
                "/Iexpat/Source/lib", f"/I{pybase.name}/Modules/expat",
                f"{pybase.name}/Modules/{module}.c", "python/libs/python313.lib", "expat/Bin/libexpat.lib",
                "/link", "/RELEASE", "/Brepro", "/DYNAMICBASE", "/NXCOMPAT", "/guard:cf", f"/OUT:{module}.pyd",
            ], cwd=work, check=True)
            shutil.copyfile(work / f"{module}.pyd", target / f"{module}.pyd")
        shutil.copyfile(work / "expat/Bin/libexpat.dll", target / "libexpat.dll")
