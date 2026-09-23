# -*- coding: utf-8 -*-
"""書き出した本編から、各カットの1・2フレーム目と最終2フレーム目を抜いてOCRし、
期待するテロップと一致するか（＝前のテロップの残り／次のテロップの先出しがないか）を検査する
（第3段階で関数化）。
使い方: python3 qc.py <workdir> <clips.py> <ocrcli> [--out DIR] [--only slug ...]

第3段階での変更点: カット区間の計算をcommon.cut_rangeに一本化（fps固定をやめ、
bounds.jsonのfpsを使う）。
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("clips")
    ap.add_argument("ocrcli")
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", nargs="*", default=[], help="対象スラッグ（既定＝全部）")
    return ap.parse_args(argv)


def load_telop_text_by_index(telops_path):
    txt = {}
    with open(telops_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(") ", 1)
            if len(parts) == 2:
                txt[int(line[:3])] = parts[1].strip()
    return txt


def seg_at(segs, txt, t):
    for i, (s, e) in enumerate(segs):
        if s <= t < e:
            return i, txt.get(i, "")
    return None, ""


def norm(s):
    return "".join(ch for ch in s if not ch.isspace())


def timeline(clip, bounds, default_xfade):
    """(素材IN, 素材OUT, 尺, 出力上の開始秒, cut) のリスト。C1→C2はXF秒重なる。"""
    XF = clip.get("xfade", default_xfade)
    tl, t0 = [], 0.0
    for i, cut in enumerate(clip["cuts"]):
        S, E = C.cut_range(cut, bounds)
        tl.append((S, E, E - S, t0, cut))
        t0 += E - S - (XF if i == 0 else 0.0)
    return tl


def checks_for_cut(i, xfade):
    """1カットぶんの検査点 [(オフセットフレーム, 種類), ...] のうち、実際に検査する
    種類だけを返す（トランジションで混ざる箇所は除く）。
    """
    checks = [(1, "head"), (2, "head+"), (-2, "tail")]
    if xfade > 0 and i == 0:
        checks = [c for c in checks if c[1] != "tail"]      # トランジションで混ざる
    if xfade > 0 and i == 1:
        checks = [c for c in checks if c[1] == "tail"]
    return checks


def build_jobs(clip, bounds, default_xfade, out_dir, tmp_dir):
    """クリップの検査対象フレームのOCR抽出ジョブ [(ffmpegコマンド用の時刻, 出力png,
    slug, カット番号, 種類, 元動画上の時刻), ...] を返す。
    """
    XF = clip.get("xfade", default_xfade)
    fps = bounds["fps"]
    jobs = []
    for i, (S, E, D, t0, cut) in enumerate(timeline(clip, bounds, default_xfade)):
        for k, kind in checks_for_cut(i, XF):
            offset = k / fps if k > 0 else D + k / fps
            ot = t0 + offset
            p = os.path.join(tmp_dir, "%s_c%02d_%s.png" % (clip["slug"], i, kind))
            srct = S + offset
            jobs.append((ot, p, clip["slug"], i, kind, srct))
    return jobs


def extract_frame(src, out_dir, ot, out_png, crop_y, crop_h):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "%.4f" % ot, "-i", src,
                    "-frames:v", "1", "-vf", "crop=1080:%d:0:%d,scale=960:-1" % (crop_h, crop_y), out_png],
                   check=True)


def judge(expect, actual):
    """OCRの認識ゆれを許容した一致判定（先頭7文字がどちらかに含まれていればOK）。"""
    key = norm(expect)[:7]
    actual_n = norm(actual)
    return (not key) or (key in actual_n) or (actual_n[:7] and actual_n[:7] in norm(expect))


def main(argv):
    args = parse_args(argv)
    M = C.load_clips(args.clips)
    BD = C.load_bounds(args.workdir)
    L = M.LAYOUT
    OUT = args.out or os.path.join(args.workdir, "out")
    TMP = os.path.join(args.workdir, "qcshots")
    os.makedirs(TMP, exist_ok=True)
    segs = json.load(open(os.path.join(args.workdir, "segs.json")))
    txt = load_telop_text_by_index(os.path.join(args.workdir, "telops.txt"))

    # 元データ y=900..1070 が出力のどこに来るか
    scale = L["band_h"] / float(M.DEFAULT_CROP[1])
    oy = L["band_y"] + int((900 - M.DEFAULT_CROP[3]) * scale) - 12
    oh = int(170 * scale) + 24

    which = set(args.only) or {c["slug"] for c in M.CLIPS}
    jobs = []
    for clip in M.CLIPS:
        if clip["slug"] not in which:
            continue
        src = os.path.join(OUT, clip["file"])
        if not os.path.exists(src):
            continue
        for ot, p, slug, i, kind, srct in build_jobs(clip, BD, M.XFADE, OUT, TMP):
            extract_frame(src, OUT, ot, p, oy, oh)
            jobs.append((p, slug, i, kind, srct))

    res = subprocess.run([args.ocrcli] + [j[0] for j in jobs], capture_output=True, text=True).stdout
    got = {}
    for line in res.splitlines():
        p, _, t = line.partition("\t")
        got[p] = t.strip()

    bad = 0
    for path, slug, i, kind, srct in jobs:
        _si, expect = seg_at(segs, txt, srct)
        actual = got.get(path, "")
        if not judge(expect, actual):
            bad += 1
            print("NG %-12s c%02d %-5s 期待[%s] 実際[%s]" % (slug, i + 1, kind, expect[:30], actual[:30]))
    print("チェック%d件 / 不一致%d件（OCRの認識ゆれ以外が出たら作り直し）" % (len(jobs), bad))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
