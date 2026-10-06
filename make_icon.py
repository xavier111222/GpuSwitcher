# -*- coding: utf-8 -*-
"""生成程序图标 assets/icon.ico（需要 Pillow，仅构建时使用）"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "icon.ico")
SIZE = 256


def lerp(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def build(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角矩形渐变背景
    r = int(size * 0.22)
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    for y in range(size):
        t = y / (size - 1)
        bd.line([(0, y), (size, y)], fill=lerp((47, 111, 235), (23, 61, 140), t) + (255,))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)

    # 闪电（节能/切换的意象）
    s = size / 256.0
    bolt = [
        (140 * s, 34 * s), (72 * s, 140 * s), (112 * s, 140 * s),
        (96 * s, 222 * s), (186 * s, 108 * s), (138 * s, 108 * s),
        (176 * s, 34 * s),
    ]
    d.polygon(bolt, fill=(126, 231, 135, 255))
    d.polygon([(p[0], p[1] + 3 * s) for p in bolt], outline=(12, 45, 92, 90))

    # 底部小方块：代表集显 / 节能
    bw = size * 0.34
    bx = (size - bw) / 2
    d.rounded_rectangle([bx, size * 0.80, bx + bw, size * 0.88],
                        radius=int(size * 0.04), fill=(255, 255, 255, 220))
    return img


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    base = build(SIZE)
    base.save(OUT, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon ->", OUT)
