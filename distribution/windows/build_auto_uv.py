"""Write the bundled Auto UV node group to a fresh .blend library."""

from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[2]


def main():
    output = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
    if output.suffix.casefold() != ".blend" or output.exists():
        raise ValueError("Expected a fresh .blend asset file")
    sys.path.insert(0, str(ROOT / "lab"))
    from auto_uv import create_auto_uv

    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.data.libraries.write(str(output), {create_auto_uv()})


if __name__ == "__main__":
    main()
