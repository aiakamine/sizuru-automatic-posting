# -*- coding: utf-8 -*-
"""カット点の直前60ms/直後60msの実効レベル（dBFS）を見て、両側とも発話レベル
（-26dBFS超）なら「ぶつ切り」と判定する（第3段階で関数化）。
使い方: python3 chop.py <src.mp4> <workdir> <clips.py>
BGMは-30dB前後なので誤検知しにくい。

第3段階での変更点:
  ・db()の窓ごとの実効レベル計算をcommon.window_dbに一本化し、全編の音声を1回だけ読み込む
    （従来は境界ごとにffmpegを呼んでいた）
  ・カット区間の計算をcommon.cut_rangeに一本化（fps固定をやめ、bounds.jsonのfpsを使う）
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

THRESHOLD_DB = C.SILENCE_CHECK_THRESHOLD_DB
NEAR_THRESHOLD_MARGIN_DB = 3.0


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("workdir")
    ap.add_argument("clips")
    return ap.parse_args(argv)


def clip_timeline(clip, bounds):
    """(素材IN, 素材OUT, cut) のリストを返す。カット区間の計算はcommon.cut_rangeに
    一本化してある（render.py/qc.pyのtimelineと同じ規則）。
    """
    tl = []
    for cut in clip["cuts"]:
        S, E = C.cut_range(cut, bounds)
        tl.append((S, E, cut))
    return tl


def classify_boundary(before_db, after_db, threshold=THRESHOLD_DB, margin=NEAR_THRESHOLD_MARGIN_DB):
    """境界前後のdBFSから判定文字列を返す：
    両側とも閾値超なら「★ぶつ切り」、どちらかが閾値のmargin dB以内まで迫っていれば「△」、
    それ以外は「OK」。
    """
    if before_db > threshold and after_db > threshold:
        return "★ぶつ切り"
    if max(before_db, after_db) > threshold - margin:
        return "△"
    return "OK"


def check_clip(clip, bounds, db_fn):
    """1本のクリップの全カット境界を判定する。フラグがOKでない行だけ
    [(カット番号(0始まり), ラベル, 時刻, 直前dB, 直後dB, フラグ, 注記), ...] で返す。
    前後のカットと地続き（境界が実質切れていない）場合はスキップする。
    """
    tl = clip_timeline(clip, bounds)
    n = len(tl)
    rows = []
    for i, (S, E, cut) in enumerate(tl):
        for lab, t, skip in (("IN ", S, i > 0 and C.is_contiguous_join(tl[i - 1][1], S)),
                             ("OUT", E, i < n - 1 and C.is_contiguous_join(E, tl[i + 1][0]))):
            if skip:      # 前後が地続き＝実際には切っていない
                continue
            before_db = db_fn(t - 0.07, 0.06)
            after_db = db_fn(t + 0.01, 0.06)
            flag = classify_boundary(before_db, after_db)
            if flag != "OK":
                rows.append((i, lab, t, before_db, after_db, flag, cut.get("note", "")[:24]))
    return rows


def clip_total_seconds(clip, bounds, xfade):
    """クリップの尺（秒）。C1→C2はxfade秒だけ重なるので差し引く。"""
    tl = clip_timeline(clip, bounds)
    total = 0.0
    for i, (S, E, cut) in enumerate(tl):
        total += E - S - (xfade if i == 0 else 0)
    return total


def main(argv):
    args = parse_args(argv)
    M = C.load_clips(args.clips)
    BD = C.load_bounds(args.workdir)
    samples = C.load_pcm(args.src)

    def db_fn(t0, dur):
        return C.window_db(samples, 16000, t0, dur)

    bad = 0
    for clip in M.CLIPS:
        xf = clip.get("xfade", M.XFADE)
        print("=== %s (%.2f秒)" % (clip["slug"], clip_total_seconds(clip, BD, xf)))
        for i, lab, t, before_db, after_db, flag, note in check_clip(clip, BD, db_fn):
            print("  C%-2d %s %8.3f  直前%.1fdB 直後%.1fdB  %s  %s"
                  % (i + 1, lab, t, before_db, after_db, flag, note))
            if flag == "★ぶつ切り":
                bad += 1
    print("\nぶつ切り判定: %d件" % bad)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
