# -*- coding: utf-8 -*-
"""カット定義の各IN/OUTが「音の切れ目」に載っているかを判定する（局所中央値方式。参考程度。
最終判定はchop.pyの絶対レベル判定を使う。SKILL.mdつまずきどころ参照）。
使い方: python3 pauses.py <src.mp4> <workdir> <clips.py> [--slug 01_xxx]

第3段階での変更点: カット区間の計算をcommon.cut_rangeに一本化（fps固定をやめ、
bounds.jsonのfpsを使う）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("workdir")
    ap.add_argument("clips")
    ap.add_argument("--slug", default=None)
    return ap.parse_args(argv)


def check_boundary(src, t, silences_fn=C.silences, margin=0.02, window=2.0):
    """時刻t±windowの無音区間を見て、tを含む無音があれば
    (True, その無音区間) を、無ければ (False, 近傍の無音候補) を返す。
    """
    rs = silences_fn(src, t - window, t + window)
    inside = [r for r in rs if r[0] - margin <= t <= r[1] + margin]
    if inside:
        return True, inside[0]
    nxt = [r for r in rs if r[0] > t]
    prv = [r for r in rs if r[1] < t]
    return False, (nxt[0] if nxt else None, prv[-1] if prv else None)


def format_result(label, t, ok, extra, note):
    if ok:
        s, e = extra
        return "  %s %8.3f  OK（無音 %.2f-%.2f の中）" % (label, t, s, e)
    nxt, prv = extra
    msg = []
    if nxt:
        msg.append("次の無音 +%.2fs" % (nxt[0] - t))
    if prv:
        msg.append("前の無音 -%.2fs" % (t - prv[1]))
    return "  %s %8.3f  要確認（%s）%s" % (label, t, " / ".join(msg) or "近傍に無音なし",
                                        "  " + note if note else "")


def main(argv):
    args = parse_args(argv)
    M = C.load_clips(args.clips)
    B = C.load_bounds(args.workdir)

    for clip in M.CLIPS:
        if args.slug and clip["slug"] != args.slug:
            continue
        print("=== %s" % clip["slug"])
        for i, cut in enumerate(clip["cuts"], 1):
            S, E = C.cut_range(cut, B)
            for lab, t in (("IN ", S), ("OUT", E)):
                ok, extra = check_boundary(args.src, t)
                label = "C%-2d %s" % (i, lab)
                print(format_result(label, t, ok, extra, cut.get("note", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
