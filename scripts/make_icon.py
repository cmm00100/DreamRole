"""生成 exe 图标。

做法是先在 1024×1024 上画，再缩到各个尺寸 —— 直接在小尺寸上画，
边缘会全是锯齿，在任务栏里看着像没做完的东西。

    python scripts/make_icon.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "dreamrole.ico"
PREVIEW = ROOT / "assets" / "dreamrole.png"

SIZE = 1024
BG_TOP = (24, 28, 38)
BG_BOTTOM = (14, 16, 21)
ACCENT = (107, 138, 253)
ACCENT_LIGHT = (168, 190, 255)


def star_points(cx: float, cy: float, outer: float, inner: float, points: int = 5,
                rotation: float = -math.pi / 2) -> list[tuple[float, float]]:
    result = []
    for index in range(points * 2):
        radius = outer if index % 2 == 0 else inner
        angle = rotation + index * math.pi / points
        result.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return result


def build() -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 背景：圆角方块 + 竖向微渐变
    gradient = Image.new("RGBA", (SIZE, SIZE))
    draw = ImageDraw.Draw(gradient)
    for y in range(SIZE):
        ratio = y / SIZE
        color = tuple(
            int(BG_TOP[channel] + (BG_BOTTOM[channel] - BG_TOP[channel]) * ratio)
            for channel in range(3)
        )
        draw.line([(0, y), (SIZE, y)], fill=color + (255,))

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, SIZE - 1, SIZE - 1], radius=int(SIZE * 0.22), fill=255
    )
    canvas.paste(gradient, (0, 0), mask)

    # 主星：先画一层模糊的，做出一点光晕
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.polygon(
        star_points(SIZE * 0.455, SIZE * 0.455, SIZE * 0.325, SIZE * 0.13), fill=ACCENT + (150,)
    )
    glow = glow.filter(ImageFilter.GaussianBlur(SIZE * 0.045))
    canvas.alpha_composite(glow)

    star = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    star_draw = ImageDraw.Draw(star)
    star_draw.polygon(
        star_points(SIZE * 0.455, SIZE * 0.455, SIZE * 0.315, SIZE * 0.126), fill=ACCENT + (255,)
    )
    # 左上角打一点高光，让平面图形有一点体积
    highlight = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).polygon(
        star_points(SIZE * 0.455, SIZE * 0.455, SIZE * 0.235, SIZE * 0.092), fill=ACCENT_LIGHT + (70,)
    )
    highlight = highlight.filter(ImageFilter.GaussianBlur(SIZE * 0.02))
    star.alpha_composite(highlight)
    canvas.alpha_composite(star)

    # 右上角一颗小星：暗示「这是 agent，不是一堆文件」
    small = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(small).polygon(
        star_points(SIZE * 0.775, SIZE * 0.775, SIZE * 0.078, SIZE * 0.029), fill=ACCENT_LIGHT + (240,)
    )
    canvas.alpha_composite(small)

    return canvas


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    master = build()
    master.save(PREVIEW)

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(OUT, format="ICO", sizes=sizes)
    print(f"图标已生成：{OUT}")
    print(f"预览图：{PREVIEW}")


if __name__ == "__main__":
    main()
