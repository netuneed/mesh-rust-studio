# Windows x64 runtime

Mesh Rust Studio Work 0.12 uses Blender 5.3.0, CPython 3.13.18 and the maintenance branch of FFmpeg 8.1. Build the native engine with `WITH_TBB_MALLOC_PROXY=OFF`. The Windows CRT allocator works with the MinGW FFmpeg libraries when the application closes. Keep Blender's normal oneTBB task scheduler enabled.

## Rebuild the runtime overlay

The full release includes `MeshRustStudio-0.12-runtime-sources-and-build-inputs.zip`. It contains the locked downloads, build scripts, wheel source distributions, source archives and notices. The two `FFmpeg-dependency-sources` ZIP files contain the corresponding source for FFmpeg's enabled build dependencies.

Use a Windows x64 Visual C++ build shell. The tested toolchain was MSVC 19.44, the Windows SDK 10.0.26100, native Win64 Perl 5.42.3.1 and NASM 2.16.03. Qt jom 1.1.4 can parallelize the OpenSSL build. The scripts also accept Microsoft's nmake. A host Python 3.12 or newer can run the packager.

```powershell
python build_runtime_overlay.py C:\build\original-runtime C:\build\new-overlay --cache C:\build\runtime-inputs\cache
```

Start with a new destination for the public package. Copy the Blender runtime with its profile exclusions. Skip the entire original `5.3/python` directory and any original file supplied by the overlay. Copy all overlay contents to the package root. This also removes superseded package metadata and the CPython 3.13.13 runtime.

The Python archive comes from Astral's Windows x64 MSVC build dated November 24, 2026. The two XML extension modules are compiled from CPython 3.13.18 and linked to Expat 2.11.0. OpenSSL is built from the upstream 3.5 maintenance commit `0eb2651bdf5d25a017f529518b6cf5f1bbc697cb`. It reports `3.5.10-dev`. The public release of 3.5.10 was scheduled for December 1. The pinned revision includes the fixes available on November 24. The hashes and download locations are in `runtime-lock.json`. [Python release](https://www.python.org/downloads/release/python-31318/) · [Expat release](https://github.com/libexpat/libexpat/releases/tag/R_2_11_0) · [OpenSSL advisory](https://openssl-library.org/news/secadv/20261124.txt)

The pinned pip ships the legacy package metadata API. The compatibility module exposes it under the original `pkg_resources` import name for existing add-ons. The checks cover distribution lookup, missing distributions, entry points, the working set and resource lookup. PyPA recommends `importlib.metadata`, `importlib.resources` and `packaging` for new add-ons. [Setuptools compatibility notes](https://setuptools.pypa.io/en/stable/deprecated/pkg_resources.html) · [pip vendoring policy](https://pip.pypa.io/en/stable/development/vendoring-policy/)

## FFmpeg sources

The seven shared libraries come from the November 25 win64 GPL shared build of FFmpeg 8.1.3 at `a8029b6528fecbcb5d99b331cfc76e6467b9bc77`. The corresponding build recipe is pinned to `b9ebe81995b58cd4e3177a6c0ad5dc2392f7f7fd`. The upstream package includes x264, x265, AAC and Opus support. The public package includes the shared libraries and their license notices. [FFmpeg Windows downloads](https://ffmpeg.org/download.html) · [Upstream build](https://github.com/FFmpeg-Builds/FFmpeg-Builds/actions/runs/45611068521)

The source bundles contain all 102 archives selected by the recipe for `win64 gpl-shared 8.1`. The source list was verified against the upstream workflow's download-cache artifact 15019699100, SHA-256 `010feb0c89941111668a6539d5416767c3484aa8bc5e4efdcfa7777c738c506a`. Each ZIP contains a manifest with per-file sizes and SHA-256 digests. Extract both ZIP files into one directory. Copy `FFmpeg-dependencies/.cache/downloads` to the `.cache/downloads` directory of the archived FFmpeg-Builds recipe. Follow the recipe's Docker prerequisites and `makeimage.sh win64 gpl-shared 8.1` and `build.sh win64 gpl-shared 8.1` steps. Pin the FFmpeg checkout inside the build script to the full commit above before running configure. The matching FFmpeg source tree is also supplied in the runtime sources archive. [Build scripts](https://github.com/FFmpeg-Builds/FFmpeg-Builds/tree/b9ebe81995b58cd4e3177a6c0ad5dc2392f7f7fd) · [FFmpeg source](https://github.com/FFmpeg/FFmpeg/tree/a8029b6528fecbcb5d99b331cfc76e6467b9bc77)
