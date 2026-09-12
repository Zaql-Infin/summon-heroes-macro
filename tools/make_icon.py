"""
make_icon.py — generates a nicer app icon (purple/gold "summon portal"
badge, matching the control panel's bubbly purple theme) to replace the
plain default-looking icon.ico. Run once from the project root:
    python tools/make_icon.py
"""
import math

from PIL import Image, ImageDraw, ImageFilter

SIZE = 512


def star_points(cx, cy, r_outer, r_inner, n=5, rot=-90):
    pts = []
    for i in range(n * 2):
        ang = math.radians(rot + i * 180 / n)
        r = r_outer if i % 2 == 0 else r_inner
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def make_icon() -> Image.Image:
    # Rounded-square badge mask (modern app-icon shape, not a plain circle).
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, SIZE - 1, SIZE - 1], radius=int(SIZE * 0.22), fill=255
    )

    # Diagonal purple gradient background (bright top-left -> deep bottom-right),
    # same two shades as the control panel's --purple-bright / --panel-solid.
    top = (197, 139, 255)
    bottom = (30, 14, 46)
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    px = bg.load()
    for y in range(SIZE):
        for x in range(SIZE):
            t = ((x + y) / (2 * SIZE))
            r = round(top[0] * (1 - t) + bottom[0] * t)
            g = round(top[1] * (1 - t) + bottom[1] * t)
            b = round(top[2] * (1 - t) + bottom[2] * t)
            px[x, y] = (r, g, b, 255)
    bg.putalpha(mask)

    # Soft radial highlight (glossy, bubbly feel) in the upper-left.
    highlight = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hd.ellipse([-SIZE * 0.35, -SIZE * 0.35, SIZE * 0.75, SIZE * 0.75], fill=(255, 255, 255, 60))
    highlight = highlight.filter(ImageFilter.GaussianBlur(SIZE * 0.12))
    highlight.putalpha(Image.composite(highlight.split()[3], Image.new("L", (SIZE, SIZE), 0), mask))
    bg = Image.alpha_composite(bg, highlight)

    # Glowing summon-portal rings + a gold star, centered.
    glyph = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glyph)
    cx, cy = SIZE // 2, SIZE // 2 - 6
    for rad, width, color in (
        (168, 9, (230, 209, 255, 160)),
        (120, 16, (255, 255, 255, 230)),
    ):
        gd.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=color, width=width)

    star_outer = star_points(cx, cy, 76, 32)
    gd.polygon(star_outer, fill=(255, 209, 102, 255), outline=(255, 244, 214, 255))

    glow = glyph.filter(ImageFilter.GaussianBlur(14))
    combined = Image.alpha_composite(bg, glow)
    combined = Image.alpha_composite(combined, glyph)
    return combined


if __name__ == "__main__":
    icon = make_icon()
    icon.save("icon_preview.png")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon.save("icon.ico", sizes=sizes)
    print("Saved icon.ico and icon_preview.png")
