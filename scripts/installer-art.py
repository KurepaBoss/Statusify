"""Regenerates the installer's side and header pictures (needs Pillow).

    python scripts/installer-art.py

Writes src-tauri/windows/sidebar.bmp (164x314, the Welcome and Finish pages) and
header.bmp (150x57, the other pages) from src-tauri/icons/icon.png, in the app's
dark theme. NSIS wants plain 24-bit BMP files without transparency. The results
are committed; this only exists so they can be redone.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src-tauri" / "windows"
TOP, BOTTOM = (0x12, 0x15, 0x1A), (0x0B, 0x0D, 0x10)   # --bg2 -> --bg of the app
ACCENT, TEXT, DIM = (0x1E, 0xD7, 0x60), (0xF2, 0xF4, 0xF7), (0xA8, 0xB0, 0xBB)
FONTS = Path("C:/Windows/Fonts")


def gradient(w: int, h: int) -> Image.Image:
    im = Image.new("RGB", (w, h))
    for y in range(h):
        t = y / (h - 1)
        c = tuple(round(TOP[i] * (1 - t) + BOTTOM[i] * t) for i in range(3))
        im.paste(c, (0, y, w, y + 1))
    return im


def centred(d: ImageDraw.ImageDraw, width: int, y: int, text: str, font, fill) -> None:
    d.text(((width - d.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)


def main() -> None:
    icon = Image.open(ROOT / "src-tauri" / "icons" / "icon.png").convert("RGBA")
    bold = ImageFont.truetype(str(FONTS / "segoeuib.ttf"), 21)
    plain = ImageFont.truetype(str(FONTS / "segoeui.ttf"), 11)

    side = gradient(164, 314)
    big = icon.resize((92, 92), Image.LANCZOS)
    side.paste(big, ((164 - 92) // 2, 46), big)
    d = ImageDraw.Draw(side)
    centred(d, 164, 152, "Statusify", bold, TEXT)
    centred(d, 164, 184, "Spotify lyrics on your", plain, DIM)
    centred(d, 164, 200, "Discord profile", plain, DIM)
    d.rectangle([62, 224, 102, 226], fill=ACCENT)
    side.save(OUT / "sidebar.bmp", format="BMP")

    head = gradient(150, 57)
    small = icon.resize((40, 40), Image.LANCZOS)
    head.paste(small, (150 - 40 - 12, 8), small)
    head.save(OUT / "header.bmp", format="BMP")


if __name__ == "__main__":
    main()
