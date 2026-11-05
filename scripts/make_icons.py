"""Generate PodBridge's app icons (favicon, home screen, web manifest). Dev-only: needs Pillow.

The icon follows Design Uniformity's app icon (DESIGN-SPEC 10): a 64 x 64 tile with a 16 px radius and a
diagonal violet-to-pink gradient (Media), and one white glyph on the inner 36 x 36, with depth from opacity
steps only. The glyph is PodBridge's bridge: an arch over the deck, two hangers, two pillars and a play
button. podbridge/static/icons/icon.svg is the same drawing; change both together.

The PNGs are drawn at 1024 px (16 x the 64-unit design) and scaled down:
- favicon-32, icon-192 and icon-512 are the rounded tile with transparent corners, like icon.svg;
- apple-touch-icon fills the square, because iOS rounds the corners itself;
- icon-maskable-512 fills the square with the glyph shrunk into Android's safe zone (the middle 80%).

Usage:  python scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "podbridge" / "static" / "icons"
SIZE = 1024
UNIT = SIZE / 64
VIOLET, PINK = (139, 92, 246), (236, 72, 153)   # --accent and --accent-2 (Media)

# The glyph, in the 64-unit space of icon.svg: (kind, geometry, opacity).
ARCH = (32, 34.4, 17, 14.1)                     # centre x, centre y (the deck's top), outer and inner radius
RECTS = [                                       # x, y, width, height, opacity
    (22.3, 23.7, 1.4, 10.7, 0.7), (40.3, 23.7, 1.4, 10.7, 0.7),   # hangers
    (14, 34.4, 36, 3.1, 1.0),                                      # deck
    (18.1, 37.5, 3, 7, 0.7), (42.9, 37.5, 3, 7, 0.7),              # pillars
]
PLAY = [(29.8, 24.6), (29.8, 32.4), (36.4, 28.5)]


def gradient(size: int) -> Image.Image:
    """The diagonal gradient, top left to bottom right, as in icon.svg."""
    ramp = Image.linear_gradient("L").resize((size, size))                 # 0 at the top, 255 at the bottom
    t = Image.blend(ramp, ramp.transpose(Image.Transpose.TRANSPOSE), 0.5)  # (x + y) / 2: the diagonal
    return Image.composite(Image.new("RGB", (size, size), PINK), Image.new("RGB", (size, size), VIOLET), t)


def glyph_layers(shrink: float = 1.0) -> list[tuple[Image.Image, float]]:
    """One mask per shape, scaled about the tile's centre by `shrink`."""
    def p(x: float, y: float) -> tuple[float, float]:
        return (32 + (x - 32) * shrink) * UNIT, (32 + (y - 32) * shrink) * UNIT

    def r(v: float) -> float:
        return v * shrink * UNIT

    layers = []
    cx, cy, outer, inner = ARCH
    m = Image.new("L", (SIZE, SIZE), 0)
    d = ImageDraw.Draw(m)
    d.ellipse([*p(cx - outer, cy - outer), *p(cx + outer, cy + outer)], fill=255)
    d.ellipse([*p(cx - inner, cy - inner), *p(cx + inner, cy + inner)], fill=0)
    d.rectangle([0, p(0, cy)[1], SIZE, SIZE], fill=0)                      # only the top half
    layers.append((m, 0.85))
    for x, y, w, h, opacity in RECTS:
        m = Image.new("L", (SIZE, SIZE), 0)
        ImageDraw.Draw(m).rounded_rectangle([*p(x, y), *p(x + w, y + h)], radius=r(min(w, h) / 2), fill=255)
        layers.append((m, opacity))
    m = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(m).polygon([p(x, y) for x, y in PLAY], fill=255)
    layers.append((m, 1.0))
    return layers


def draw(rounded: bool, shrink: float = 1.0) -> Image.Image:
    img = gradient(SIZE).convert("RGBA")
    white = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255))
    for mask, opacity in glyph_layers(shrink):
        img = Image.composite(white, img, mask.point(lambda v, o=opacity: round(v * o)))
    if rounded:
        corners = Image.new("L", (SIZE, SIZE), 0)
        ImageDraw.Draw(corners).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=round(16 * UNIT), fill=255)
        img.putalpha(corners)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tile, square = draw(rounded=True), draw(rounded=False)
    for name, px in (("favicon-32.png", 32), ("icon-192.png", 192), ("icon-512.png", 512)):
        tile.resize((px, px), Image.LANCZOS).save(OUT / name, optimize=True)
    square.resize((180, 180), Image.LANCZOS).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
    draw(rounded=False, shrink=0.8).resize((512, 512), Image.LANCZOS).convert("RGB").save(
        OUT / "icon-maskable-512.png", optimize=True)
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
