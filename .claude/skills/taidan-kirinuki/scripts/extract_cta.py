# -*- coding: utf-8 -*-
"""公開済みショートからCTA（本編はこちらから＋矢印）をループ素材として切り出す。
使い方: python3 extract_cta.py <published_short.mp4> <out.mp4> [--y 1225] [--h 360] [--ss 2] [--t 6]
"""
import argparse, subprocess
ap = argparse.ArgumentParser()
ap.add_argument("mp4"); ap.add_argument("out")
ap.add_argument("--y", type=int, default=1225); ap.add_argument("--h", type=int, default=360)
ap.add_argument("--ss", type=float, default=2.0); ap.add_argument("--t", type=float, default=6.0)
a = ap.parse_args()
subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % a.ss, "-t", "%.2f" % a.t,
                "-i", a.mp4, "-an", "-vf", "crop=1080:%d:0:%d" % (a.h, a.y),
                "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", a.out], check=True)
print("wrote", a.out)
