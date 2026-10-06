# -*- coding: utf-8 -*-
"""
生成程序图标 assets/icon.ico —— macOS 风格：
蓝色渐变圆角底 + 白色 GPU 芯片 + 深蓝内核，小尺寸自动简化细节。
（仅构建时需要 Pillow）
"""
import os

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "icon.ico")

# 渐变色（顶部亮蓝 → 底部深蓝）
TOP = (26, 142, 255)
BOT = (0, 82, 190)


def _vgrad(w, h, top, bot):
    col = Image.new("L", (1, h))
    px = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = int(t * 255)
    col = col.resize((w, h), Image.NEAREST)
    top_img = Image.new("RGB", (w, h), top)
    bot_img = Image.new("RGB", (w, h), bot)
    return Image.composite(bot_img, top_img, col)


def _squircle_mask(size, inset=0.0):
    """圆角方形遮罩（近似 macOS superellipse 的圆角矩形）"""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    i = size * inset
    d.rounded_rectangle([i, i, size - 1 - i, size - 1 - i],
                        radius=size * 0.225, fill=255)
    return m


def _draw_detail(size, chip=True, pins=True, core=True):
    """在 size×size 的超采样画布上绘制，返回 RGBA"""
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 底部投影
    sh = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sh).rounded_rectangle(
        [S * 0.075, S * 0.105, S * 0.935, S * 0.965],
        radius=S * 0.225, fill=90)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    shadow.putalpha(sh)
    img = Image.alpha_composite(img, shadow)

    # 渐变主体
    body = _vgrad(S, S, TOP, BOT).convert("RGBA")
    mask = _squircle_mask(S)
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base.paste(body, (0, 0), mask)
    img = Image.alpha_composite(img, base)

    # 顶部高光（白 → 透明，裁剪进圆角内）
    hg = Image.new("L", (1, S))
    px = hg.load()
    for y in range(S):
        t = 1.0 - y / (S * 0.52)
        px[0, y] = int(max(0.0, t) * 52)
    hg = hg.resize((S, S), Image.NEAREST)
    hg = ImageChops.darker(hg, mask)
    hl = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    hl.putalpha(hg)
    img = Image.alpha_composite(img, hl)

    d = ImageDraw.Draw(img)

    if chip:
        x0, y0, x1, y1 = S * 0.30, S * 0.32, S * 0.70, S * 0.68
        # 引脚（四边）
        if pins:
            pw = S * 0.028
            pl = S * 0.075
            for i in range(4):
                t = 0.36 + i * 0.093
                # 上下
                d.rounded_rectangle([S * t - pw / 2, y0 - pl, S * t + pw / 2, y0],
                                    radius=pw / 2, fill=(255, 255, 255, 235))
                d.rounded_rectangle([S * t - pw / 2, y1, S * t + pw / 2, y1 + pl],
                                    radius=pw / 2, fill=(255, 255, 255, 235))
            for i in range(3):
                t = 0.40 + i * 0.10
                d.rounded_rectangle([x0 - pl, S * t - pw / 2, x0, S * t + pw / 2],
                                    radius=pw / 2, fill=(255, 255, 255, 235))
                d.rounded_rectangle([x1, S * t - pw / 2, x1 + pl, S * t + pw / 2],
                                    radius=pw / 2, fill=(255, 255, 255, 235))

        # 芯片本体
        d.rounded_rectangle([x0, y0, x1, y1], radius=S * 0.055,
                            fill=(255, 255, 255, 255))
        # 内核
        if core:
            cx0, cy0, cx1, cy1 = S * 0.385, S * 0.405, S * 0.615, S * 0.595
            d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=S * 0.032,
                                fill=(10, 91, 211, 255))
            # 内核高光
            hl2 = Image.new("RGBA", (S, S), (255, 255, 255, 0))
            ImageDraw.Draw(hl2).rounded_rectangle(
                [cx0, cy0, cx1, (cy0 + cy1) / 2], radius=S * 0.032,
                fill=(255, 255, 255, 45))
            img = Image.alpha_composite(img, hl2)
    else:
        # 简化版：实心圆角方块
        d.rounded_rectangle([S * 0.30, S * 0.32, S * 0.70, S * 0.68],
                            radius=S * 0.06, fill=(255, 255, 255, 255))

    return img


def build(size, detail):
    src = _draw_detail(size * 4, chip=detail >= 1, pins=detail >= 2, core=detail >= 2)
    return src.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    frames = []
    for s in (256, 128, 64, 48, 40, 32, 24, 20, 16):
        detail = 2 if s >= 48 else (1 if s >= 24 else 0)
        frames.append((s, build(s, detail)))
        frames[-1][1].save("_icon_%d.png" % s)
    frames[0][1].save(
        OUT, format="ICO",
        sizes=[(f[0], f[0]) for f in frames],
        append_images=[f[1] for f in frames[1:]])
    print("icon ->", OUT)
