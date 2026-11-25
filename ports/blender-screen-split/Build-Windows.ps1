[CmdletBinding()]
param(
    [ValidateSet('Prepare', 'Build', 'Package')]
    [string]$Stage = 'Build',
    [string]$CMake = 'cmake.exe',
    [string]$Ninja = 'ninja.exe',
    [ValidateRange(1, 32)]
    [int]$Jobs = 8,
    [string]$BinaryDirectory,
    [string]$PackageDirectory,
    [switch]$VulkanOptix,
    [string]$RustToolchain = '1.99.0-x86_64-pc-windows-msvc'
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$upstream = Join-Path $root 'upstream/blender'
$source = Join-Path $root 'build/blender-port-source'
$build = Join-Path $root 'build'
New-Item -ItemType Directory -Force -Path $build | Out-Null
$build = (Resolve-Path $build).Path
$base = '02f0899d78e374b63e7181d9061e1f7ab03419cc'
$libraryCommit = '4d0478217fd7f53e785cdbac95de8f726f35cd55'
$rustup = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cargo/bin/rustup.exe'
$target = 'x86_64-pc-windows-msvc'
$rustDir = Join-Path $root 'build/blender-port-rust'
$patches = @(
    'blender-v5.3.0.patch',
    'blender-v5.3.0-area-plan.patch',
    'blender-v5.3.0-edit-mesh.patch',
    'blender-v5.3.0-armature.patch',
    'blender-v5.3.0-curve-ring.patch',
    'blender-v5.3.0-subdiv-edges.patch',
    'blender-v5.3.0-subdiv-profile.patch',
    'blender-v5.3.0-subdiv-topology.patch',
    'blender-v5.3.0-subdiv-evaluate.patch',
    'blender-v5.3.0-subdiv-evaluate-uv.patch',
    'blender-v5.3.0-bmesh-pools.patch',
    'blender-v5.3.0-bmesh-quads.patch',
    'blender-v5.3.0-vulkan-subdiv-lazy-patches.patch',
    'blender-v5.3.0-vulkan-subdiv-parallel.patch',
    'blender-v5.3.0-vulkan-viewport-startup.patch',
    'blender-v5.3.0-edit-selection-import.patch',
    'blender-v5.3.0-edit-sparse-transform.patch',
    'blender-v5.3.0-bmesh-parallel-export-loops.patch',
    'blender-v5.3.0-port-instance.patch',
    'blender-v5.3.0-geometry-unused-id-inputs.patch',
    'blender-v5.3.0-vulkan-shader-library-cache.patch',
    'blender-v5.3.0-bmesh-pool-phases.patch',
    'blender-v5.3.0-bmesh-uv-pin-layout.patch',
    'blender-v5.3.0-work-desktop.patch',
    'blender-v5.3.0-gpu-patch-array-cache.patch',
    'blender-v5.3.0-smart-uv-direction-search.patch',
    'blender-v5.3.0-exact-sparse-intersections.patch',
    'blender-v5.3.0-exact-topology-cache.patch',
    'blender-v5.3.0-boolean-plus-manifold.patch',
    'blender-v5.3.0-file-smoothscroll-timer.patch',
    'blender-v5.3.0-boolean-plus-card.patch',
    'blender-v5.3.0-public-desktop.patch'
)
$gpuRoot = Join-Path $env:LOCALAPPDATA 'Blender-Rust-Port'
$gpuCache = Join-Path $env:LOCALAPPDATA 'Temp/blender-rust-port-5.3-cmake'
$optix = Join-Path $gpuRoot 'sdk/optix-9.0.0'
if (!$BinaryDirectory) {
    $BinaryDirectory = if ($VulkanOptix) { $gpuCache } else { Join-Path $build 'blender-port-windows-cmake' }
}
if (!$PackageDirectory) {
    $PackageDirectory = if ($VulkanOptix) { Join-Path $gpuRoot 'packages/5.3-dev' }
        else { Join-Path $build 'blender-port-windows-x64-subdiv-rust' }
}

function Resolve-BuildDirectory([string]$Path) {
    $absolute = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $allowed = @($build)
    if ($VulkanOptix) { $allowed += @($gpuRoot, $gpuCache) }
    $parent = $allowed | Where-Object { $absolute.Equals($_, [StringComparison]::OrdinalIgnoreCase) -or
            $absolute.StartsWith($_ + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) } |
        Select-Object -First 1
    if (!$parent -or ($absolute -eq $parent -and $parent -ne $gpuCache)) {
        throw "Use a directory inside the project build folder or the local Blender Rust GPU build folder: $absolute"
    }
    for ($current = $absolute; $current; $current = Split-Path -Parent $current) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Use a regular directory for the isolated build: $current"
        }
        if ($current -eq $parent) { break }
    }
    return $absolute
}
$BinaryDirectory = Resolve-BuildDirectory $BinaryDirectory
$package = Resolve-BuildDirectory $PackageDirectory

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program exited with code $LASTEXITCODE."
    }
}

$officialArchive = Join-Path $root 'upstream/official-5.3.0/blender-5.3.0-windows-x64.zip'
function Assert-GpuArchive {
    if ((Get-FileHash -LiteralPath $officialArchive -Algorithm SHA256).Hash -ne
        '3AC567A63D59D9934F7B9213BFB27BA05760E1FB366F8F55C381573EC3EABFA8') {
        throw 'Expected the pinned official Blender Windows archive for the precompiled GPU kernels.'
    }
}

function Complete-Package {
    $exe = Join-Path $package 'blender.exe'
    if (!(Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "The install target did not produce $exe."
    }
    $licenseDirectory = New-Item -ItemType Directory -Path (Join-Path $package 'license') -Force
    $notices = @('OPEN_SUBDIV_LICENSE.txt', 'OPEN_SUBDIV_NOTICE.txt')
    Copy-Item -LiteralPath ($notices | ForEach-Object { Join-Path $PSScriptRoot $_ }) -Destination $licenseDirectory
    if ($VulkanOptix) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $kernelPath = '5.3/scripts/addons_core/cycles/lib'
        $kernelDir = New-Item -ItemType Directory -Path (Join-Path $package $kernelPath) -Force
        $archive = [IO.Compression.ZipFile]::OpenRead($officialArchive)
        try {
            foreach ($entry in $archive.Entries) {
                if ($entry.FullName -eq "blender-5.3.0-windows-x64/$kernelPath/$($entry.Name)" -and
                    $entry.Name -match '^kernel_(optix(_mnee|_shader_raytrace)?\.ptx|(compute_\d+\.ptx)|(sm_\d+[a-z]?\.cubin))\.zst$') {
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $kernelDir $entry.Name), $true)
                }
            }
        } finally {
            $archive.Dispose()
        }
        foreach ($kernel in @('kernel_optix.ptx.zst', 'kernel_optix_mnee.ptx.zst', 'kernel_optix_shader_raytrace.ptx.zst',
                'kernel_compute_75.ptx.zst', 'kernel_sm_120.cubin.zst')) {
            if (!(Test-Path -LiteralPath (Join-Path $kernelDir $kernel) -PathType Leaf)) {
                throw "Missing $kernel in the pinned Blender Windows archive."
            }
        }
    }
    Write-Output $exe
}

if ($Stage -eq 'Package') {
    if ($VulkanOptix) { Assert-GpuArchive }
    Complete-Package
    return
}

if (!(Test-Path -LiteralPath (Join-Path $source '.git'))) {
    $previousSmudge = $env:GIT_LFS_SKIP_SMUDGE
    try {
        $env:GIT_LFS_SKIP_SMUDGE = '1'
        Invoke-Checked git @('-C', $upstream, 'worktree', 'add', '--detach', $source, $base)
    } finally {
        $env:GIT_LFS_SKIP_SMUDGE = $previousSmudge
    }
}
if ((Invoke-Checked git @('-C', $source, 'rev-parse', 'HEAD')) -ne $base) {
    throw "Expected the isolated source checkout at $base."
}
$buildPatches = @($patches | ForEach-Object { Join-Path $PSScriptRoot $_ })
Copy-Item -LiteralPath (Join-Path $root 'distribution/windows/MeshRustStudio.ico') -Destination (Join-Path $source 'release/windows/icons/meshruststudio.ico') -Force
$alreadyApplied = @()
$restored = @()
# A later patch may replace context used by an earlier one. Undo known patches
# from the end, then reapply the series to the same isolated working tree.
try {
    for ($i = $buildPatches.Count - 1; $i -ge 0; $i--) {
        $patch = $buildPatches[$i]
        & git -C $source apply --reverse --check $patch 2>$null
        if ($LASTEXITCODE -eq 0) {
            Invoke-Checked git @('-C', $source, 'apply', '--reverse', $patch)
            $alreadyApplied = @($patch) + $alreadyApplied
        }
    }
    foreach ($patch in $buildPatches) {
        Invoke-Checked git @('-C', $source, 'apply', '--check', $patch)
        Invoke-Checked git @('-C', $source, 'apply', $patch)
        $restored += $patch
    }
} catch {
    foreach ($patch in $alreadyApplied) {
        if ($patch -notin $restored) { & git -C $source apply $patch }
    }
    throw
}

if ($Stage -eq 'Prepare') {
    $library = Join-Path $source 'lib/windows_x64'
    if (!(Test-Path -LiteralPath (Join-Path $library '.git'))) {
        $previousSmudge = $env:GIT_LFS_SKIP_SMUDGE
        try {
            $env:GIT_LFS_SKIP_SMUDGE = '1'
            Invoke-Checked git @('-C', $source, '-c', 'submodule.lib/windows_x64.update=checkout',
                'submodule', 'update', '--init', '--depth', '1', 'lib/windows_x64')
        } finally {
            $env:GIT_LFS_SKIP_SMUDGE = $previousSmudge
        }
    }
    if ((Invoke-Checked git @('-C', $library, 'rev-parse', 'HEAD')) -ne $libraryCommit) {
        throw "Expected the Windows libraries at $libraryCommit."
    }
    $official = 'https://projects.blender.org/blender/blender.git'
    Invoke-Checked git @('-C', $source, '-c', "remote.blender-lfs.url=$official", 'lfs', 'pull',
        '--include=release/**,assets/**,scripts/**,doc/**', 'blender-lfs')
    Invoke-Checked git @('-C', $library, 'lfs', 'pull')
    Invoke-Checked $rustup @('toolchain', 'install', $RustToolchain, '--profile', 'minimal', '--no-self-update')
    Invoke-Checked $rustup @('target', 'add', '--toolchain', $RustToolchain, $target)
    return
}

$windowsLibraries = Join-Path $source 'lib/windows_x64'
if (!(Test-Path -LiteralPath (Join-Path $windowsLibraries '.git')) -or
    (Invoke-Checked git @('-C', $windowsLibraries, 'rev-parse', 'HEAD')) -ne $libraryCommit -or
    (Get-Item -LiteralPath (Join-Path $source 'release/datafiles/startup.blend')).Length -lt 1024) {
    throw 'Run the Prepare stage to fetch the pinned Blender files and Windows libraries.'
}

$cmakePath = (Get-Command $CMake -CommandType Application -ErrorAction Stop).Source
$ninjaPath = (Get-Command $Ninja -CommandType Application -ErrorAction Stop).Source
Invoke-Checked $rustup @('run', $RustToolchain, 'cargo', 'rustc', '--target', $target, '--release', '--locked', '--offline',
    '--manifest-path', (Join-Path $PSScriptRoot 'Cargo.toml'), '--target-dir', $rustDir,
    '--', '--print=native-static-libs')
$rustLibrary = Join-Path $rustDir "$target/release/blender_screen_split.lib"
$crtPath = @()
if ($env:VCToolsRedistDir) {
    $crtPath = @("-DMSVC_REDIST_DIR=$env:VCToolsRedistDir")
}

$gpu = @('-DWITH_VULKAN_BACKEND=OFF', '-DWITH_CYCLES_DEVICE_CUDA=OFF', '-DWITH_CYCLES_DEVICE_OPTIX=OFF')
if ($VulkanOptix) {
    $optixCommit = 'fff65c2a7c592f1ea5f1661ad7d2381cf965f9bd'
    if (!(Test-Path -LiteralPath $optix)) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $optix) -Force | Out-Null
        Invoke-Checked git @('clone', '--depth', '1', '--branch', 'v9.0.0', 'https://github.com/NVIDIA/optix-dev.git', $optix)
    }
    if ((Invoke-Checked git @('-C', $optix, 'rev-parse', 'HEAD')) -ne $optixCommit -or
        (Invoke-Checked git @('-C', $optix, 'status', '--porcelain'))) {
        throw 'Expected the unmodified NVIDIA OptiX SDK 9.0.0 headers.'
    }
    Assert-GpuArchive
    $gpu = @('-DWITH_VULKAN_BACKEND=ON', '-DWITH_CUDA_DYNLOAD=ON', '-DWITH_CYCLES_CUDA_BINARIES=OFF',
        '-DWITH_CYCLES_DEVICE_CUDA=ON', '-DWITH_CYCLES_DEVICE_OPTIX=ON', "-DOPTIX_ROOT_DIR:PATH=$optix")
}

$configureArguments = @('-S', $source, '-B', $BinaryDirectory, '-G', 'Ninja',
    "-DCMAKE_MAKE_PROGRAM=$ninjaPath", '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_TRY_COMPILE_CONFIGURATION=Release',
    "-DCMAKE_INSTALL_PREFIX=$package",
    "-DBLENDER_RUST_SCREEN_SPLIT_LIB=$rustLibrary", '-DWITH_BLENDER_RUST_EDIT_MESH=ON',
    '-DWITH_PYTHON=ON', '-DWITH_PYTHON_INSTALL=ON',
    '-DWITH_TBB=ON', '-DWITH_TBB_MALLOC_PROXY=OFF', '-DWITH_CODEC_FFMPEG=ON', '-DWITH_OPENGL_BACKEND=ON',
    '-DWITH_CYCLES=ON', '-DWITH_CYCLES_OSL=OFF', '-DWITH_CYCLES_PATH_GUIDING=OFF',
    '-DWITH_CYCLES_DEVICE_HIP=OFF',
    '-DWITH_CYCLES_DEVICE_ONEAPI=OFF', '-DWITH_GTESTS=OFF', '-DWITH_XR_OPENXR=OFF',
    '-DWITH_WINDOWS_BUNDLE_CRT=OFF', '-DWITH_WINDOWS_RELEASE_PDB=OFF') + $crtPath + $gpu
Invoke-Checked $cmakePath $configureArguments
Invoke-Checked $cmakePath @('--build', $BinaryDirectory, '--target', 'install', '--parallel', "$Jobs")
Complete-Package
