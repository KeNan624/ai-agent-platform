from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


W, H = 1448, 1086
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
SRC_DIR = Path("/root/.codex/generated_images/019e2b1c-dee2-7293-b33f-55136f25ea2f")
OUT_DIR = Path("/root/ai-agent-platform/tmp/course-covers/generated")


COURSES = [
    (
        "dd",
        "ig_0ea047ec3fb2a0c9016a06f258186c8191adbc217ecc91f116.png",
        ["绿幕直播", "运营项目"],
        "#9BFF6A",
    ),
    (
        "ss",
        "ig_0ea047ec3fb2a0c9016a06f29e5b048191bdae301af6579063.png",
        ["小红书", "IP运营"],
        "#FFD0D8",
    ),
    (
        "dsj",
        "ig_0ea047ec3fb2a0c9016a06f2cfc020819180861c17dde7a217.png",
        ["短视频", "剪辑课程"],
        "#1FE8FF",
    ),
    (
        "dytw",
        "ig_0ea047ec3fb2a0c9016a06f31d08dc8191ac5ebdff43fc0e1e.png",
        ["抖音图文", "训练营"],
        "#19E8FF",
    ),
    (
        "gzh",
        "ig_0ea047ec3fb2a0c9016a06f3a6e96c8191a1af0db6de575ee2.png",
        ["公众号", "IP运营"],
        "#8AFFC1",
    ),
    (
        "jd",
        "ig_0ea047ec3fb2a0c9016a06f3f5e6e88191b3595da24ad411a9.png",
        ["京东CPS", "训练营"],
        "#FFE066",
    ),
    (
        "sph",
        "ig_0ea047ec3fb2a0c9016a06f4b2e4608191b49afc74ccdd66bd.png",
        ["视频号", "带货项目"],
        "#72F5FF",
    ),
]


def cover_crop_resize(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    sw, sh = img.size
    target = W / H
    src = sw / sh
    if src > target:
        nw = int(sh * target)
        left = (sw - nw) // 2
        img = img.crop((left, 0, left + nw, sh))
    elif src < target:
        nh = int(sw / target)
        top = (sh - nh) // 2
        img = img.crop((0, top, sw, top + nh))
    return img.resize((W, H), Image.Resampling.LANCZOS)


def fit_font(lines: list[str], max_width: int, start_size: int = 120) -> ImageFont.FreeTypeFont:
    size = start_size
    while size >= 72:
        font = ImageFont.truetype(FONT, size=size, index=0)
        widths = [font.getbbox(line)[2] - font.getbbox(line)[0] for line in lines]
        if max(widths) <= max_width:
            return font
        size -= 4
    return ImageFont.truetype(FONT, size=72, index=0)


def draw_title(img: Image.Image, lines: list[str], accent: str) -> Image.Image:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = fit_font(lines, max_width=520)

    x = 178
    y = 385
    line_gap = 18
    boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_h = max(b[3] - b[1] for b in boxes)

    for idx, line in enumerate(lines):
        yy = y + idx * (line_h + line_gap)
        # Multi-pass shadow gives the flat white type a subtle 3D lift like the existing covers.
        for ox, oy, fill in [
            (7, 9, (0, 0, 0, 150)),
            (4, 5, (0, 0, 0, 95)),
            (-2, -2, (255, 255, 255, 45)),
        ]:
            draw.text((x + ox, yy + oy), line, font=font, fill=fill)
        draw.text((x, yy), line, font=font, fill=(255, 255, 255, 255))

    underline_y = y + len(lines) * (line_h + line_gap) + 46
    draw.rounded_rectangle(
        (x + 4, underline_y, x + 240, underline_y + 7),
        radius=4,
        fill=accent,
    )
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def erase_low_underline(img: Image.Image) -> Image.Image:
    # Some generated backgrounds include a second low underline. Interpolate it away
    # so the final cover only has the title underline used by the existing style.
    img = img.copy()
    pix = img.load()
    x1, y1, x2, y2 = 70, 790, 410, 835
    for x in range(x1, x2):
        top = pix[x, y1 - 1]
        bottom = pix[x, y2 + 1]
        for y in range(y1, y2):
            t = (y - y1) / max(1, y2 - y1 - 1)
            pix[x, y] = tuple(
                int(top[i] * (1 - t) + bottom[i] * t)
                for i in range(3)
            )
    return img


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for slug, filename, lines, accent in COURSES:
        img = Image.open(SRC_DIR / filename)
        final = draw_title(cover_crop_resize(img), lines, accent)
        out = OUT_DIR / f"{slug}-cover.png"
        final.save(out)
        print(out)


if __name__ == "__main__":
    main()
