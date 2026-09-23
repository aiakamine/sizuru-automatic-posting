# -*- coding: utf-8 -*-
"""全編の音声を1回読んで、テロップ境界のうち「音の切れ目に載っているもの」＝
安全なカット点を一覧にする。カット点は必ずこの中から選ぶ。
使い方: python3 safe.py [src.mp4]
出力: safe.txt  （境界番号 / 時刻 / 安全か / その境界から始まるテロップ）
カレントディレクトリの bounds.json / segs.json / telops.txt を読む（書式は変えない）。

第3段階での変更点: 固定fps(29.97)をやめ、bounds.jsonのfpsを使う（30fps・25fps素材にも対応）。
"""
import argparse
import json
import math
import re
import struct
import subprocess
import sys

RATE = 16000
STEP = 160                      # 10ms
LOCAL_MEDIAN_BLOCK = 1000       # 10秒ブロックごとの中央値（BGMや場面で音量が変わるため）
QUIET_RATIO = 0.55
MIN_SILENCE_SEC = 0.10
BOUNDARY_TOLERANCE_SEC = 0.03
TELOP_MATCH_TOLERANCE_SEC = 0.06


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default="source.mp4")
    return ap.parse_args(argv)


def load_telop_text_by_time(path):
    """telops.txt（開始時刻→本文）を返す。行の書式はsubtext.pyのものと同じ。"""
    txt = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r'\s*(\d+)\s+([\d.]+)-\s*([\d.]+)\s+\(\s*([\d.]+)s\)\s?(.*)', line.rstrip("\n"))
            if m:
                txt[float(m.group(2))] = m.group(5)
    return txt


def read_envelope(src, rate=RATE, step=STEP):
    """srcの全編を読み、step サンプルごとの実効値(RMS)の列を返す。"""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", str(rate),
                          "-f", "s16le", "-"], capture_output=True).stdout
    n = len(raw) // 2
    v = struct.unpack("<%dh" % n, raw[:n * 2]) if n else ()
    return [math.sqrt(sum(x * x for x in v[i:i + step]) / step) for i in range(0, n - step, step)]


def block_medians(env, block=LOCAL_MEDIAN_BLOCK):
    """envをblockサンプルごとに区切った各ブロックの中央値。"""
    meds = []
    for k in range(0, len(env), block):
        b = sorted(env[k:k + block])
        meds.append(b[len(b) // 2] if b else 0.0)
    return meds


def local_median_at(meds, i, block=LOCAL_MEDIAN_BLOCK):
    """envのインデックスiにおける局所中央値（隣接ブロック間を線形補間する）。"""
    k = i / float(block)
    a, b = int(k), min(int(k) + 1, len(meds) - 1)
    f = k - a
    return meds[min(a, len(meds) - 1)] * (1 - f) + meds[b] * f


def compute_silences(env, meds, rate=RATE, step=STEP, ratio=QUIET_RATIO, minlen=MIN_SILENCE_SEC):
    """局所中央値のratio倍を下回る状態がminlen秒以上続く区間を [(start,end), ...] で返す。"""
    quiet = [env[i] < local_median_at(meds, i) * ratio for i in range(len(env))]
    sil, cur = [], None
    for i, q in enumerate(quiet):
        t = i * step / float(rate)
        if q:
            cur = [t, t] if cur is None else [cur[0], t]
        else:
            if cur and cur[1] - cur[0] >= minlen:
                sil.append(tuple(cur))
            cur = None
    if cur and cur[1] - cur[0] >= minlen:
        sil.append(tuple(cur))
    return sil


def compute_safe_rows(bound, fps, silences, telop_text_by_time,
                       boundary_tolerance=BOUNDARY_TOLERANCE_SEC,
                       telop_tolerance=TELOP_MATCH_TOLERANCE_SEC):
    """境界フレームごとに (フレーム, 時刻, 安全か, その境界のテロップ本文) を返す。"""
    si = 0
    out = []
    for f in bound:
        t = (f - 0.5) / fps
        while si + 1 < len(silences) and silences[si][1] < t - 0.05:
            si += 1
        ok = any(s <= t + boundary_tolerance and t <= e + boundary_tolerance
                 for s, e in silences[max(0, si - 2):si + 3])
        key = min(telop_text_by_time.keys(), key=lambda x: abs(x - t)) if telop_text_by_time else None
        body = telop_text_by_time.get(key, "") if key is not None and abs(key - t) < telop_tolerance else ""
        out.append((f, t, ok, body))
    return out


def format_row(f, t, ok, body):
    return "%6d %9.3f %s %s\n" % (f, t, "SAFE" if ok else "----", body)


def main(argv):
    args = parse_args(argv)
    B = json.load(open("bounds.json"))
    bound = B["bound"]
    fps = B.get("fps")
    if fps is None:
        fps = 30000.0 / 1001
    json.load(open("segs.json"))  # 存在確認（telops.txt生成の前提が揃っているかのチェック）
    telop_text_by_time = load_telop_text_by_time("telops.txt")

    env = read_envelope(args.src)
    print("envelope frames:", len(env))
    meds = block_medians(env)
    sil = compute_silences(env, meds)
    print("silences:", len(sil))
    json.dump(sil, open("silences.json", "w"))

    rows = compute_safe_rows(bound, fps, sil, telop_text_by_time)
    with open("safe.txt", "w", encoding="utf-8") as fh:
        for f, t, ok, body in rows:
            fh.write(format_row(f, t, ok, body))
    print("safe boundaries: %d / %d" % (sum(1 for r in rows if r[2]), len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
