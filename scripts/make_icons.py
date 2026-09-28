#!/usr/bin/env python3
"""Generate PWA icons (192/512 + maskable) with the same PIL pipeline the
generated covers use: paper background, serif "B." wordmark in the accent.

Run: .venv/bin/python scripts/make_icons.py   (output committed under web/icons/)
"""
import pathlib

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "icons"
PAPER, INK, ACCENT = "#F6F3EC", "#26221C", "#7C2D2D"
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if pathlib.Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def draw_icon(size: int, maskable: bool) -> Image.Image:
    img = Image.new("RGB", (size, size), PAPER)
    d = ImageDraw.Draw(img)
    # maskable safe zone: keep art inside the central 80%
    pad = int(size * (0.16 if maskable else 0.06))
    d.rectangle([pad, pad, size - pad, size - pad], outline=INK, width=max(2, size // 64))
    f = font(int(size * 0.52))
    bbox = d.textbbox((0, 0), "B.", font=f)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]), "B.",
           font=f, fill=ACCENT)
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    draw_icon(192, False).save(OUT / "icon-192.png")
    draw_icon(512, False).save(OUT / "icon-512.png")
    draw_icon(512, True).save(OUT / "icon-maskable-512.png")
    print(f"wrote icons to {OUT}")


if __name__ == "__main__":
    main()
