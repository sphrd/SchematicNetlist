"""Draw the app icon and the start-up splash image in the GUI's own theme.

    .venv\\Scripts\\python.exe src\\make_assets.py

Writes src/schematic_netlist/assets/{icon.ico, icon.png, splash.png}; build_exe.py runs this
first so the exe always carries the current artwork.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parent / "schematic_netlist" / "assets"
BG, SURFACE, ACCENT, FG, MUTED = "#222831", "#2d333d", "#4da3ff", "#ffffff", "#aab4c0"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_icon(size: int = 256) -> Image.Image:
    """Dark rounded tile with an accent-blue net: a bus, three branches, junction and pin dots.
    Deliberately NOT a blue tile with a document, so it is told apart from similar tools at 16 px."""
    s = 4  # supersample for smooth edges at small sizes
    n = size * s
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=n * 0.22, fill=BG, outline=ACCENT, width=max(1, int(n * 0.03)))

    line_w = max(1, int(n * 0.07))
    bus_x = n * 0.33
    ys = (0.30, 0.50, 0.70)
    d.line((bus_x, n * ys[0], bus_x, n * ys[-1]), fill=ACCENT, width=line_w)
    for y in ys:
        yy = n * y
        xb = n * 0.70
        d.line((bus_x, yy, xb, yy), fill=ACCENT, width=line_w)
        r = n * 0.065
        d.ellipse((xb - r, yy - r, xb + r, yy + r), fill=FG)
    r = n * 0.06
    d.ellipse((bus_x - r, n * 0.50 - r, bus_x + r, n * 0.50 + r), fill=ACCENT)  # T-junction dot
    return img.resize((size, size), Image.LANCZOS)


def draw_splash(width: int = 520, height: int = 300) -> Image.Image:
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, width - 1, height - 1), outline=SURFACE, width=2)
    icon = draw_icon(96)
    img.paste(icon, (36, 48), icon)
    d.text((156, 54), "SchematicNetlist", font=_font(30, bold=True), fill=FG)
    d.text((158, 100), "Schematic PDF  \u2192  netlist, HSPICE, Excel", font=_font(14), fill=MUTED)
    d.line((36, 186, width - 36, 186), fill=SURFACE, width=2)
    d.text((36, 200), "Starting up \u2014 unpacking the bundled Python runtime,", font=_font(12), fill=MUTED)
    d.text((36, 220), "PDF engine and OCR. This takes a few seconds.", font=_font(12), fill=MUTED)
    # the bootloader writes its progress text at SPLASH_TEXT_POS (see build_exe.py)
    return img


SPLASH_TEXT_POS = (36, 262)


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    big = draw_icon(256)
    big.save(ASSETS / "icon.png")
    big.save(ASSETS / "icon.ico", sizes=[(w, w) for w in ICON_SIZES])
    draw_splash().save(ASSETS / "splash.png")
    print(f"-> {ASSETS}")


if __name__ == "__main__":
    main()
