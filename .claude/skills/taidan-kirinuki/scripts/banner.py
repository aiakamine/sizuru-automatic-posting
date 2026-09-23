# -*- coding: utf-8 -*-
"""対談・解説系 切り抜き用バナー（公開済みショートのデザインに合わせた版）
   ・左端は垂直、右端は下にいくほど内側に入る平行四辺形状
   ・横グラデーション シアン→マゼンタ、縦は上下が明るく中央が暗い
   ・細い回路パターンが右へ流れる4秒ループ"""
import math, os, random, sys
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PAD = 46                      # グロー用の余白
BODY_W_TOP = 1014             # x=30..1044
BODY_H = 213                  # y=241..454
SLANT = 0.2424                # 右端の傾き（下にいくほど内側）
LEFT = 30
RAD = 26
CANVAS_W = 1080
CANVAS_H = BODY_H + PAD * 2

C_L = (0, 170, 192)           # 左端（明るいところ）
C_R = (172, 10, 148)          # 右端
V_FLOOR, V_POW = 0.53, 1.7    # 縦シェーディング

FPS, SECONDS = 30, 4
NFRAMES = FPS * SECONDS
FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc"


def right_x(y):
    """本体上端からの相対yに対する右端x"""
    return LEFT + BODY_W_TOP - SLANT * y


def shape_mask(w, h, feather=0):
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    # 平行四辺形（角丸は後段でぼかし＋再2値化ではなく、行ごとに角丸半径ぶん詰める）
    for y in range(h):
        r = 0
        if y < RAD:
            r = RAD - math.sqrt(max(0.0, RAD * RAD - (RAD - y) ** 2))
        elif y > h - 1 - RAD:
            dy = y - (h - 1 - RAD)
            r = RAD - math.sqrt(max(0.0, RAD * RAD - dy ** 2))
        x0 = LEFT + r
        x1 = right_x(y) - r
        d.line([(x0, y), (x1, y)], fill=255)
    if feather:
        m = m.filter(ImageFilter.GaussianBlur(feather))
    return m


def gradient(w, h):
    g = Image.new("RGB", (w, h))
    px = g.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        v = V_FLOOR + (1 - V_FLOOR) * (abs(2 * t - 1) ** V_POW)
        for x in range(w):
            u = min(1.0, max(0.0, (x - LEFT) / float(BODY_W_TOP)))
            px[x, y] = tuple(int((C_L[i] + (C_R[i] - C_L[i]) * u) * v) for i in range(3))
    return g


def circuit(w, h, seed=11):
    """細い回路パターン（横につないでスクロール）"""
    rnd = random.Random(seed)
    t = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(t)
    line = (255, 255, 255, 34)
    dot = (255, 255, 255, 60)
    for _ in range(9):
        y = rnd.randrange(14, h - 14)
        x0 = rnd.choice([rnd.randrange(20, 260), rnd.randrange(760, w - 40)])
        seg = rnd.randrange(70, 210)
        d.line([x0, y, x0 + seg, y], fill=line, width=2)
        y2 = max(10, min(h - 10, y + rnd.choice([-1, 1]) * rnd.randrange(20, 52)))
        d.line([x0 + seg, y, x0 + seg, y2], fill=line, width=2)
        d.line([x0 + seg, y2, x0 + seg + rnd.randrange(40, 130), y2], fill=line, width=2)
    for _ in range(14):
        x = rnd.choice([rnd.randrange(20, 300), rnd.randrange(740, w - 30)])
        y = rnd.randrange(8, h - 8)
        s = rnd.choice([3, 4])
        d.rectangle([x, y, x + s, y + s], fill=dot)
    t.paste(t.crop((0, 0, 220, h)), (w - 220, 0), t.crop((0, 0, 220, h)))
    return t


def fit_font(lines, max_w, max_h):
    size = 76
    while size > 34:
        f = ImageFont.truetype(FONT, size)
        lh = int(size * 1.16)
        if all(f.getbbox(l)[2] - f.getbbox(l)[0] <= max_w for l in lines) and lh * len(lines) <= max_h:
            return f, size
        size -= 2
    return ImageFont.truetype(FONT, 34), 34


def build(lines, outdir):
    os.makedirs(outdir, exist_ok=True)
    body_w, body_h = CANVAS_W, BODY_H
    mask = shape_mask(body_w, body_h)
    base = gradient(body_w, body_h)

    tile = circuit(body_w, body_h)
    strip = Image.new("RGBA", (body_w * 2, body_h), (0, 0, 0, 0))
    strip.paste(tile, (0, 0)); strip.paste(tile, (body_w, 0))

    # 文字
    font, size = fit_font(lines, 830, body_h - 48)
    txt = Image.new("RGBA", (body_w, body_h), (0, 0, 0, 0))
    dt = ImageDraw.Draw(txt)
    lh = int(size * 1.16)
    y = (body_h - lh * len(lines)) // 2
    cx = LEFT + BODY_W_TOP / 2.0
    for ln in lines:
        bb = font.getbbox(ln)
        x = int(cx - (bb[2] - bb[0]) / 2) - bb[0]
        sh = Image.new("RGBA", (body_w, body_h), (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((x + 3, y + 5), ln, font=font, fill=(0, 0, 0, 165))
        txt.alpha_composite(sh.filter(ImageFilter.GaussianBlur(3)))
        dt.text((x, y), ln, font=font, fill=(255, 255, 255, 255))
        y += lh

    # 外側グロー：本体形状をぼかして色を乗せる
    glow_src = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    tmp = Image.new("RGBA", (body_w, body_h), (0, 0, 0, 0))
    tmp.paste(base.convert("RGBA"), (0, 0), mask)
    glow_src.paste(tmp, (0, PAD))
    glow = glow_src.filter(ImageFilter.GaussianBlur(12))
    a = glow.split()[3].point(lambda v: min(255, int(v * 1.55)))
    glow.putalpha(a)

    # 内側の明るいフチ
    edge = mask.filter(ImageFilter.GaussianBlur(1.2))
    inner = Image.new("L", (body_w, body_h), 0)
    ImageDraw.Draw(inner).rectangle([0, 0, body_w - 1, body_h - 1], fill=0)
    shrunk = mask.filter(ImageFilter.MinFilter(7))
    border = Image.new("RGBA", (body_w, body_h), (0, 0, 0, 0))
    border.putalpha(Image.eval(edge, lambda v: v).point(lambda v: v))
    ring = Image.new("L", (body_w, body_h))
    ring.paste(edge, (0, 0))
    ring = Image.composite(Image.new("L", (body_w, body_h), 0), ring, shrunk)
    border = Image.new("RGBA", (body_w, body_h), (255, 255, 255, 0))
    border.putalpha(ring.point(lambda v: int(v * 0.82)))

    for i in range(NFRAMES):
        off = int(body_w * i / NFRAMES)
        pat = strip.crop((off, 0, off + body_w, body_h))
        body = base.convert("RGBA")
        body.alpha_composite(pat)
        body.alpha_composite(border)
        body.alpha_composite(txt)
        card = Image.new("RGBA", (body_w, body_h), (0, 0, 0, 0))
        card.paste(body, (0, 0), mask)
        out = glow.copy()
        out.alpha_composite(card, (0, PAD))
        out.save(os.path.join(outdir, "b_%03d.png" % i))
    print(outdir, "frames:", NFRAMES, "font:", size, "canvas:", out.size)


if __name__ == "__main__":
    build(sys.argv[2:], sys.argv[1])
