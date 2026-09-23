# -*- coding: utf-8 -*-
"""公開済みショート（完成版）1本からレイアウトを実測する。
使い方: python3 measure_layout.py <published_short.mp4> [--t 5]
バナー本体の上下端・左端・右端の傾き、映像バンドの上下端、CTAの上下端を出す。
新シーズンでデザインが変わったら必ずこれを回してから制作する。
"""
import argparse, subprocess, tempfile, os
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("mp4"); ap.add_argument("--t", type=float, default=5.0)
a = ap.parse_args()
png = os.path.join(tempfile.mkdtemp(), "f.png")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % a.t, "-i", a.mp4,
                "-frames:v", "1", png], check=True)
im = Image.open(png).convert("RGB"); W, H = im.size; px = im.load()
rows = []
for y in range(H):
    c = sum(1 for x in range(0, W, 2) if sum(px[x, y]) > 60)
    rows.append((y, c))
runs, cur = [], None
for y, c in rows:
    if c > 8:
        cur = [y, y] if cur is None else [cur[0], y]
    else:
        if cur: runs.append(tuple(cur)); cur = None
if cur: runs.append(tuple(cur))
print("画面サイズ %dx%d" % (W, H))
print("非黒ブロック（上から）:", runs)
if runs:
    y0, y1 = runs[0]
    print("\n■ バナー（グロー込み %d..%d）" % (y0, y1))
    for y in range(y0, y1 + 1, max(1, (y1 - y0) // 14)):
        xs = [x for x in range(W) if sum(px[x, y]) > 90]
        if xs:
            print("   y=%4d  x=%4d..%4d" % (y, min(xs), max(xs)))
    band = [r for r in runs if r[1] - r[0] > 300]
    if band:
        print("\n■ 映像バンド  天から%dpx  高さ%dpx" % (band[0][0], band[0][1] - band[0][0] + 1))
    cta = [r for r in runs if r[0] > (band[0][1] if band else 0)]
    if cta:
        print("■ CTA  天から%dpx  下端%dpx（%d..%d のブロック群）"
              % (cta[0][0], cta[-1][1], cta[0][0], cta[-1][1]))
