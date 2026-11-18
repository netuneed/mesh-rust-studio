# Mesh Rust Studio

[Русская инструкция](README.ru.md)

Mesh Rust Studio is an independent project based on Blender 5.3 for Windows x64 with Rust implementations for selected operations and extra modeling tools. It uses Blender's `.blend` format, interface and Python API.

## What has improved

Work includes Auto UV, QuadriFlow and mesh simplification. First-run setup imports selected settings and links existing asset and extension folders. Work and Dev use separate profiles.

| Benchmark | Before → After | Change |
| --- | ---: | --- |
| First Vulkan frame, warm launch | 1.46 s → 1.15 s (−21%) | Direct launcher removes an extra process |
| Smart UV Project, 81,920 faces | 466 ms → 154 ms (−67%) | Reuses projection comparisons |
| Enter Edit Mode, 1.57M faces | 1,350 ms → 820 ms (−39%) | Faster mesh conversion and pool setup |
| Undo, same mesh | 1,306 ms → 605 ms (−54%) | Faster restoration of edit geometry |

Local Windows benchmark medians. Launch and Smart UV compare earlier project builds. Edit Mode and Undo compare with Blender 5.3.0 under the same test conditions.

**Next**. Cut pauses when editing Subdivision and shared Boolean stacks. Rebuild and test the Work and Dev downloads after updating embedded dependencies.

**Security status**. The public release contains sources. Windows builds are pending fixes for embedded FFmpeg 8.1 (CVE-2026-8461) and updates to Python and HTTP dependencies. [Security policy](SECURITY.md) · [FFmpeg advisory list](https://ffmpeg.org/security.html)

## Download

**Release status**. Windows builds are pending updates to bundled dependencies.

Open [GitHub Releases](https://github.com/netuneed/mesh-rust-studio/releases). A channel is ready to install when its ZIP and checksums appear under **Assets** on a published release.

| Edition | ZIP | Choose it for |
| --- | --- | --- |
| Work | `MeshRustStudio-Work-Windows-x64.zip` | Daily modeling, Auto UV, QuadriFlow and mesh simplification |
| Dev | `MeshRustStudio-Dev-Windows-x64.zip` | Trying development builds in a separate profile |

Use a 64-bit Windows PC with an x64 processor and a GPU driver supported by Blender. See the release notes for tested versions and enabled GPU features. [Blender system requirements](https://www.blender.org/download/requirements/)

To verify the download, run this command in PowerShell from the download folder and compare the result with the SHA-256 shown on that release. For Dev, substitute its ZIP name.

```powershell
Get-FileHash -Algorithm SHA256 .\MeshRustStudio-Work-Windows-x64.zip
```

## First launch

1. Extract the entire ZIP to a folder you plan to keep. Give Work and Dev their own folders.
2. Run **Mesh Rust Studio.exe** in the extracted folder. This is a per-user portable installation.
3. Choose **Mesh Rust Studio Setup** on the welcome screen. To reopen it, press **F3** and search for **Mesh Rust Studio Setup**. Choose English or Russian in the setup window.
4. Select a detected Blender 4.2 to 5.3 profile or choose a profile folder. Review the settings to import. Supported settings include UI scale, mouse navigation, Undo and file saving preferences.
5. Attach the local asset libraries you want to use. You can also opt in to reusing local Blender extension repositories. Both features reference existing folders.
6. To open `.blend` and `.blend1` in Work by default, choose **Choose default app** in Work Setup. Then pick **Mesh Rust Studio** for each extension in Windows Settings. [Windows default apps](https://support.microsoft.com/en-us/windows/apps/change-default-apps-in-windows)

Work and Dev keep separate profiles under `%LOCALAPPDATA%\MeshRustStudio`. Enable trusted, compatible extensions individually in Blender. Updates or removals in a shared extension repository affect every Blender installation that uses it. Set up startup scenes, keymaps, themes and legacy add-ons separately.

To update, close Mesh Rust Studio, extract the replacement ZIP to a new folder and run its launcher. Your channel profile is stored separately. If Work was the default app, register it again from the new folder.

## Modeling tools in Work

- **Add Modifier → Mesh Rust Studio → Auto UV** unwraps the mesh produced by earlier modifiers. It writes a separate UV map named `Auto UV` by default. Select that map in the material's UV Map node to use it.
- **N → Mesh Rust Studio → Topology** runs QuadriFlow, Reduce Polygons or Simplify Flat Areas on the evaluated mesh. The tool creates a separate object. **Update Topology** recalculates the selected result from its source. Attribute reprojection, including UVs, is approximate.

Try Dev on a copy of an important project. Check the release notes for any Blender features or add-ons your workflow depends on.

## Source and support

The code is based on [Blender 5.3.0](https://github.com/blender/blender/tree/02f0899d78e374b63e7181d9061e1f7ab03419cc). The release page will carry the matching source and build instructions alongside the binaries. Blender and bundled components retain their licenses. [Blender license](https://docs.blender.org/manual/en/5.3/getting_started/about/license.html)

See [Build the source preview](BUILDING.md) for the pinned upstream base, native patches and portable packaging.

[Report a bug](https://github.com/netuneed/mesh-rust-studio/issues) · [Report a security issue](SECURITY.md)
