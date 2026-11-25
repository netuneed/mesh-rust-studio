# Build Mesh Rust Studio Work 0.12

The Windows build is based on Blender 5.3.0 at commit `02f0899d78e374b63e7181d9061e1f7ab03419cc`. The Windows library submodule is pinned to `4d0478217fd7f53e785cdbac95de8f726f35cd55`. The source archive in the release contains the upstream Blender source tarball, the port, the local modifications in application order, and the portable distribution scripts. The same release lists the updated runtime sources. [Work 0.12 release](https://github.com/netuneed/mesh-rust-studio/releases/tag/v0.12.0-work)

## Compile the engine

Use an x64 Visual Studio developer PowerShell with the MSVC C++ tools, Windows SDK, CMake, Ninja, Git LFS, Rustup and Python 3.13. The port selects Rust 1.99.0 for `x86_64-pc-windows-msvc`. Run the commands from the port directory. The build script fetches the OptiX 9.0.0 headers when `-VulkanOptix` is enabled. [Blender Windows build prerequisites](https://developer.blender.org/docs/handbook/building_blender/windows/)

Place the official Windows ZIP in `upstream/official-5.3.0/blender-5.3.0-windows-x64.zip`. Its SHA-256 is `3ac567a63d59d9934f7b9213bfb27ba05760e1fb366f8f55c381573ec3eabfa8`. The package sources CUDA and OptiX kernels, the redistributable C runtime and standard application assets from that archive. [Official Windows ZIP](https://download.blender.org/release/Blender5.3/blender-5.3.0-windows-x64.zip) · [Upstream checksums](https://download.blender.org/release/Blender5.3/blender-5.3.0.sha256)

```powershell
git clone --no-checkout https://projects.blender.org/blender/blender.git upstream/blender
$native = Join-Path $PWD 'build/native-work'
$cache = Join-Path $PWD 'build/cmake-work'
$build = '.\ports\blender-screen-split\Build-Windows.ps1'
& $build -Stage Prepare -VulkanOptix -BinaryDirectory $cache -PackageDirectory $native
& $build -Stage Build -VulkanOptix -BinaryDirectory $cache -PackageDirectory $native
```

The ordered patch list and CMake settings are in `ports/blender-screen-split/Build-Windows.ps1`. Work enables Vulkan, OpenGL, Cycles CPU, CUDA and OptiX. The renderer uses oneTBB with `WITH_TBB_MALLOC_PROXY=OFF`. The public desktop patch supplies the application name, taskbar identity, icon and relaunch command. C++ diagnostics embedded by the compiler can contain absolute build paths. The packaging script maps supplied build roots inside the read-only data of unsigned project binaries.

## Assemble the portable download

The runtime updater uses the checked-in lockfile in `distribution/windows/runtime`. Its input is the installed Windows runtime built in the previous step. Run it from the developer shell so that any updated native Python modules can be compiled. The runtime build uses native Win64 Perl and NASM to build the TLS libraries. [Runtime instructions and dependency pins](distribution/windows/runtime/RUNTIME.md)

```powershell
$upstream = Resolve-Path 'upstream/official-5.3.0/blender-5.3.0-windows-x64.zip'
$assets = Join-Path $PWD 'build/work-assets'
$overlay = Join-Path $PWD 'build/runtime-overlay'
$downloadCache = Join-Path $PWD 'build/dependency-downloads'
$engine = Join-Path $native 'blender.exe'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$output = Join-Path $PWD 'build/distribution/MeshRustStudio-Work-0.12-Windows-x64'
& $engine -b --factory-startup --offline-mode --disable-autoexec --python-exit-code 1 --python distribution/windows/build_work_assets.py -- $assets
python distribution/windows/runtime/build_runtime_overlay.py $native $overlay --cache $downloadCache
```

Unpack `MeshRustStudio-Work-0.12-Source.zip` and set `$inputs` below to its `upstream` directory. The Python headers and import library come from the same pinned downloads cached in the previous step. The Windows x64 Imath development package was fetched by the engine's Prepare step.

```powershell
$inputs = Resolve-Path 'upstream'
$sdk = New-Item -ItemType Directory -Path 'build/exr-sdk'
$py = Get-ChildItem $downloadCache -Filter 'cpython-3.13.18+*-install_only_stripped.tar.gz' | Select-Object -First 1
tar -xf $py.FullName -C $sdk python/libs/python313.lib
tar -xf (Join-Path $downloadCache 'cpython-3.13.18.tar.xz') -C $sdk
$headers = Get-ChildItem $sdk -Directory -Filter '*ython-3.13.18' | Select-Object -First 1
Copy-Item (Join-Path $headers.FullName 'PC/pyconfig.h') (Join-Path $headers.FullName 'Include/pyconfig.h')
python -m zipfile -e (Join-Path $inputs 'pybind11-3.0.1-py3-none-any.whl') $sdk
$exr = Join-Path $PWD 'build/openexr-overlay'
python distribution/windows/build_openexr_overlay.py --archive (Join-Path $inputs 'openexr-3.4.19.tar.gz') --work build/openexr-cmake --overlay $exr --imath-dir build/blender-port-source/lib/windows_x64/imath/lib/cmake/Imath --pybind11-dir (Join-Path $sdk 'pybind11/share/cmake/pybind11') --pybind11-license (Join-Path $sdk 'pybind11-3.0.1.dist-info/licenses/LICENSE') --python-exe (Join-Path $overlay '5.3/python/bin/python.exe') --python-include (Join-Path $headers.FullName 'Include') --python-library (Join-Path $sdk 'python/libs/python313.lib') --map-root $PWD
python distribution/windows/package_work.py --upstream $upstream --runtime $native --engine $engine --overlay $overlay --overlay $exr --assets $assets --compiler $compiler --output $output --map-root $PWD --zip
```

The output directory and ZIP must be new. Supply an additional `--map-root` if the native compiler also embedded its build cache outside your project. First-run settings are created on the end user's machine by `Mesh Rust Studio.exe`. The package contains two generated modeling libraries, Auto UV and the CAD workspace.

## Source archives and licenses

The upstream Blender source tarball has SHA-256 `a8faedaef3e8bcf1465ec3f672733b94854ff75b8c1755dcf43424af8bcacab2`. Blender also publishes a 5.3.0 source archive containing the original dependency sources under `packages/`. The engine's dependency build recipes and patches are in Blender's `build_files/build_environment/`. For updated runtime dependencies, use the matching archives and source manifest on the Work 0.12 release. [Blender source](https://download.blender.org/source/blender-5.3.0.tar.xz) · [Blender and original dependency sources](https://download.blender.org/source/blender-with-libraries-5.3.0.tar.xz) · [Windows library repository](https://projects.blender.org/blender/lib-windows_x64)

See `license/license.md`, `license/licenses.json`, `license/spdx/`, `license/others/` and `license/runtime-updates/` in the Windows archive for the relevant notices. The included tools remain subject to their own licenses. The distribution is an independent Blender build. [Blender license](https://www.blender.org/about/license/)
