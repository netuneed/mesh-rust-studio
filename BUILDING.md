# Building Mesh Rust Studio

This source preview contains the Rust code, native patch series, asset generator, first-run setup and portable launcher. Local test outputs and development history are stored separately.

## Blender base

Blender 5.3.0 is pinned at `02f0899d78e374b63e7181d9061e1f7ab03419cc`. Obtain the source from [Blender Projects](https://projects.blender.org/blender/blender) or its [GitHub mirror](https://github.com/blender/blender/tree/02f0899d78e374b63e7181d9061e1f7ab03419cc). Use the Blender Windows build prerequisites, including Visual Studio C++ tools, Git LFS, CMake, Ninja and Rustup. [Upstream Windows build guide](https://developer.blender.org/docs/handbook/building_blender/windows/)

The historical local configuration reproduces the engine used to develop the changes. Clone the pinned source into `upstream/blender`, load the Visual Studio x64 build environment and run the port's Prepare and Build stages. `ports/blender-screen-split/Build-Windows.ps1` contains the ordered patch list, Rust toolchain and Blender Windows library pin. Its optional Vulkan/OptiX configuration expects the pinned official 5.3.0 Windows ZIP at `upstream/official-5.3.0/blender-5.3.0-windows-x64.zip`. Keep the original license notices with any redistribution.

```powershell
git clone --no-checkout https://projects.blender.org/blender/blender.git upstream/blender
git -C upstream/blender cat-file -e 02f0899d78e374b63e7181d9061e1f7ab03419cc
.\ports\blender-screen-split\Build-Windows.ps1 -Stage Prepare
.\ports\blender-screen-split\Build-Windows.ps1 -Stage Build
```

The portable layout generator takes a clean runtime, a verified `Auto UV.blend` library file and Microsoft's .NET Framework C# compiler. The included generator serializes the node group to an asset file. Windows settings registration is performed by the Work launcher when requested from setup.

```powershell
$runtime = Resolve-Path 'build/blender-port-windows-x64-subdiv-rust'
$engine = Join-Path $runtime 'blender.exe'
$asset = Join-Path $PWD 'build/Auto UV.blend'
$csc = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $engine -b --factory-startup --offline-mode --disable-autoexec --python-exit-code 1 --python distribution/windows/build_auto_uv.py -- $asset
python distribution/windows/package_portable.py $runtime $asset build/windows-preview --compiler $csc
```

## Binary release status

The local runtime still contains affected FFmpeg 8.1, CPython 3.13.13 and HTTP dependencies. The presence of the affected FFmpeg MagicYUV decoder was verified. Public builds require a verified updated dependency set and a rebuild. The portable packaging script prepares test candidates and does not publish. The public engine also needs a separate identity and icon. Each published binary release should carry the exact corresponding sources, build steps, redistribution notices, checksums and test results. [FFmpeg security](https://ffmpeg.org/security.html) · [Python 3.13.18](https://www.python.org/downloads/release/python-31318/) · [Blender trademark policy](https://www.blender.org/about/trademark-policy/)
