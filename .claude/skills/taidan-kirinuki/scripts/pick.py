# -*- coding: utf-8 -*-
"""区間内の「切っていい点」を一覧にする。
   ・テロップ境界（SAFE=無音に載っている）
   ・無テロップ区間の中の無音（テロップが無いのでどこで切ってもテロップずれは起きない）"""
import json, sys, re
a, b = float(sys.argv[1]), float(sys.argv[2])
sil = json.load(open("silences.json"))
segs = json.load(open("segs.json"))
txt = {}
for line in open("telops.txt", encoding="utf-8"):
    m = re.match(r'\s*(\d+)\s+([\d.]+)-\s*([\d.]+)\s+\(\s*([\d.]+)s\)\s?(.*)', line.rstrip("\n"))
    if m: txt[int(m.group(1))] = (float(m.group(2)), float(m.group(3)), m.group(5))
rows = []
for line in open("safe.txt", encoding="utf-8"):
    p = line.split(None, 3)
    t = float(p[1])
    if a <= t <= b:
        rows.append((t, p[2], (p[3].rstrip() if len(p) > 3 else "")))
blank = []
for i, (s, e) in enumerate(segs):
    if not (txt.get(i, ("", "", ""))[2] or "").strip() and e > a and s < b:
        blank.append((s, e))
print("── テロップ境界 ──")
for t, f, body in rows:
    print("  %9.3f %s %s" % (t, f, body[:46]))
if blank:
    print("── 無テロップ区間（この中の無音はどこで切ってもテロップずれ無し）──")
    for s, e in blank:
        print("  %9.3f-%9.3f (%.1fs)" % (s, e, e - s))
        for x, y in sil:
            if x >= s - 0.1 and y <= e + 0.1:
                print("      無音 %9.3f-%9.3f (%.2fs)" % (x, y, y - x))
