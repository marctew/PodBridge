"""Generate PodBridge's app icons (home screen, favicon, web manifest). Dev-only: needs Pillow.

The icon is an amber bridge with a play button under the arch, on PodBridge's near-black
background. It's drawn at 1024 px and scaled down for each size. The background fills the
whole square because iOS and Android round the corners themselves.

Usage:  python scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "podbridge" / "static" / "icons"
BG = (20, 21, 22, 255)        # --bg
AMBER = (229, 160, 13, 255)   # --accent
SIZE = 1024


def draw(size: int = SIZE, padding: float = 0.0) -> Image.Image:
    """padding shrinks the artwork towards the centre (for 'maskable' icons, whose edges may be cropped)."""
    img = Image.new("RGBA", (size, size), BG)
    d = ImageDraw.Draw(img)
    scale = size / SIZE * (1 - 2 * padding)
    off = size * padding

    def p(x: float, y: float) -> tuple[float, float]:
        y -= 70  # optical centring: the artwork spans y 330..830
        return off + x * scale, off + y * scale

    def box(x0, y0, x1, y1):
        return [*p(x0, y0), *p(x1, y1)]

    w = lambda v: max(1, round(v * scale))  # noqa: E731
    # Arch
    d.arc(box(170, 330, 854, 950), start=180, end=360, fill=AMBER, width=w(58))
    # Deck
    d.rounded_rectangle(box(150, 628, 874, 690), radius=w(31), fill=AMBER)
    # Pillars
    d.rounded_rectangle(box(232, 640, 292, 830), radius=w(14), fill=AMBER)
    d.rounded_rectangle(box(732, 640, 792, 830), radius=w(14), fill=AMBER)
    # Hangers between arch and deck
    for x in (330, 694):
        d.rounded_rectangle(box(x - 13, 450, x + 13, 640), radius=w(13), fill=AMBER)
    # Play button under the arch
    d.polygon([p(468, 430), p(468, 590), p(590, 510)], fill=AMBER)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw()
    for name, px in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512),
                     ("favicon-32.png", 32)):
        big.resize((px, px), Image.LANCZOS).save(OUT / name, optimize=True)
    draw(padding=0.12).resize((512, 512), Image.LANCZOS).save(OUT / "icon-maskable-512.png", optimize=True)
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
