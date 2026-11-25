# SPDX-License-Identifier: GPL-2.0-or-later
"""Generate the Work application icon and startup artwork. Requires Pillow."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


output = Path(__file__).resolve().parent
bg, orange, white = "#191c22", "#ff914d", "#f7f1e9"
icon = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(icon)
draw.rounded_rectangle((3, 3, 253, 253), radius=51, fill=bg)
# A folded sheet of four quads forms the M.
for polygon, color in (
    ([(40, 77), (80, 54), (128, 91), (128, 134)], orange),
    ([(128, 91), (176, 54), (216, 77), (128, 145)], "#ffc29e"),
    ([(40, 77), (81, 108), (81, 174), (40, 200)], "#e96f36"),
    ([(175, 108), (216, 77), (216, 200), (175, 174)], orange),
):
    draw.polygon(polygon, fill=color)
icon.save(
    output / "MeshRustStudio.ico",
    sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)],
)
splash = Image.new("RGB", (960, 480), bg)
mark = icon.resize((206, 206), Image.Resampling.LANCZOS)
splash.paste(mark, (44, 130), mark)
draw = ImageDraw.Draw(splash)
draw.text(
    (270, 160), "Mesh Rust Studio", font=ImageFont.load_default(size=66), fill=white
)
draw.text(
    (274, 252),
    "Work 0.12   /   Windows x64",
    font=ImageFont.load_default(size=27),
    fill=orange,
)
splash.save(output / "MeshRustStudio-splash.png", optimize=True)
