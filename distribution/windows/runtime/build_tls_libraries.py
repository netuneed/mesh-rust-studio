"""Build the pinned OpenSSL 3.5 maintenance revision for CPython on Windows x64."""

import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory


def build_tls_libraries(source, target):
    for tool in ("cl", "perl", "nasm", "nmake"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing Windows build tool: {tool}")
    with TemporaryDirectory(prefix="openssl-build-") as temp:
        work = Path(temp)
        with tarfile.open(source) as archive:
            archive.extractall(work / "src", filter="data")
        src = next((work / "src").iterdir())
        build = work / "build"
        build.mkdir()
        env = os.environ.copy()
        env.update({
            "CC": "cl",
            "CXX": "cl",
            "SOURCE_DATE_EPOCH": "1795536000",
            "LC_ALL": "C",
            "_CL_": "/FS /Z7",
            "_LINK_": "/PDBALTPATH:%_PDB% /Brepro",
        })
        config = [
            "perl", str(src / "Configure"), "VC-WIN64A", "shared", "no-tests",
            "no-docs", "no-makedepend", "no-filenames", "--release",
            r"--prefix=C:\Program Files\Common Files\OpenSSL",
            r"--openssldir=C:\Program Files\Common Files\SSL",
        ]
        subprocess.run(config, cwd=build, env=env, check=True)
        make = ["jom", "-j8"] if shutil.which("jom") else ["nmake", "/NOLOGO"]
        names = ["libcrypto-3-x64.dll", "libssl-3-x64.dll"]
        subprocess.run(make + ["build_generated"], cwd=build, env=env, check=True)
        subprocess.run(make + names, cwd=build, env=env, check=True)
        for name in names:
            shutil.copyfile(build / name, target / name)
