# -*- coding: utf-8 -*-
"""telops.txt の各行について、終点の語尾分類・終点の無音判定・（任意）次の発話までの間を
機械的に出す候補表（第3段階・対談スキルへの展開）。
使い方: python3 cutpoints.py <src.mp4> <workdir> [--from 秒] [--to 秒] [--whisper]
        [--model small] [--whisper-bin <path>] [--cache cutpoints_cache]

無音の判定は密着スキルの silencedetect -30dB ではなく、chop.py と同じ絶対レベル
（終点の直前60ms/直後60msが両方-26dBFS超なら谷なし＝要注意）を使う。対談はBGMが
-30dB前後あり、-30dBの無音検出では谷がほとんど取れないため。音声は全編を1回だけ
common.load_pcmで読み込み、行ごとにffmpegを呼ばない（本編30分超で重すぎるため）。
終点はgstartスナップ後の時刻（＝そこで切ったときに実際に切れる位置）で見る。
--from/--toで区間を絞れる（--whisperを全行に掛けると重いため）。

語尾分類は kirinuki_core.endings に共通化されている（密着スキルと共有するため）。
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import _core  # noqa: E402
_core.ensure_kirinuki_core_on_path()

from kirinuki_core.endings import classify_ending, caution_reasons  # noqa: E402

# align_check.pyと音声解析まわりの実装を共有する（同じ第3段階で新設した兄弟モジュール）
import align_check as AC  # noqa: E402

RATE = 16000
_TELOP_LINE_RE = re.compile(r"^\s*(\d+)\s+([\d.]+)-\s*([\d.]+)\s+\(\s*([\d.]+)s\)\s?(.*)$")


def load_telop_rows(path):
    """telops.txt（subtext.pyの書式）を [{"index","start","end","text"}, ...] で返す。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _TELOP_LINE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            idx, start, end, _dur, text = m.groups()
            rows.append({"index": int(idx), "start": float(start), "end": float(end),
                         "text": text.strip()})
    rows.sort(key=lambda r: r["start"])
    return rows


def whisper_gap_after(src, line_end, cache_dir, model=AC.DEFAULT_MODEL, whisper_bin=None,
                       radius=AC.DEFAULT_RADIUS_SEC):
    """line_end 以降の最初の発話までの間隔（秒）をWhisperで求める。align_check.pyと
    同じヘルパー（音声切り出し・Whisper実行・キャッシュ）を再利用する。
    """
    wav_name = "line_%d" % round(line_end * 1000)
    words = AC.load_words_for_boundary(src, line_end, radius, cache_dir, wav_name,
                                        model=model, whisper_bin=whisper_bin)
    return AC.trailing_gap(words, line_end)


def build_row(telop_row, snapped_end, samples, whisper_gap=None):
    """1行分の候補表データを組み立てる。無音判定はcommon.has_silence_gap（絶対レベル、
    終点の直前60ms/直後60msのdBFSを見るだけで、実際の無音区間の長さは測っていない）。
    候補表にはその実測dB値をそのまま出す（谷の秒数を断定しない。Codexレビューで検出：
    以前は「谷0.12s」等、測っていない固定長の無音区間があるかのように表示していた）。
    """
    ending_class = classify_ending(telop_row["text"])
    before_db, after_db = C.silence_levels(samples, RATE, snapped_end)
    has_gap = C.has_silence_gap(samples, RATE, snapped_end)
    # caution_reasonsは「無音の谷があるか(None以外か)」だけを見るため、実測値そのものを渡す
    silence_hit = (before_db, after_db) if has_gap else None
    reasons = caution_reasons(ending_class, silence_hit, whisper_gap)
    return {
        "start": telop_row["start"], "end": telop_row["end"], "snapped_end": snapped_end,
        "text": telop_row["text"], "ending_class": ending_class,
        "has_silence_gap": has_gap, "before_db": before_db, "after_db": after_db,
        "gap": whisper_gap, "caution": bool(reasons), "reasons": reasons,
    }


def format_row(row):
    silence_desc = ("谷あり" if row["has_silence_gap"] else "谷なし") + \
        "(直前%.1fdB/直後%.1fdB)" % (row["before_db"], row["after_db"])
    gap_desc = "%.2fs" % row["gap"] if row["gap"] is not None else "?"
    mark = "要注意" if row["caution"] else "OK"
    reason_desc = "（%s）" % "・".join(row["reasons"]) if row["reasons"] else ""
    return "%8.3f-%8.3f(実際の切れ目%8.3f) %-6s 無音:%-8s 次発話:%-6s %-4s%s %s" % (
        row["start"], row["end"], row["snapped_end"],
        row["ending_class"], silence_desc, gap_desc, mark, reason_desc, row["text"])


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("workdir")
    ap.add_argument("--from", dest="t0", type=float, default=None)
    ap.add_argument("--to", dest="t1", type=float, default=None)
    ap.add_argument("--whisper", action="store_true", help="次の発話までの間をWhisperで実測する")
    ap.add_argument("--model", default=AC.DEFAULT_MODEL)
    ap.add_argument("--whisper-bin", default=None)
    ap.add_argument("--cache", default=None)
    return ap.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    bounds = C.load_bounds(args.workdir)
    telop_rows = load_telop_rows(os.path.join(args.workdir, "telops.txt"))
    samples = C.load_pcm(args.src, rate=RATE)
    cache_dir = args.cache or os.path.join(args.workdir, "cutpoints_cache")

    shown = 0
    caution_count = 0
    for telop_row in telop_rows:
        if not telop_row["text"]:
            continue
        if args.t0 is not None and telop_row["start"] < args.t0:
            continue
        if args.t1 is not None and telop_row["start"] > args.t1:
            continue

        frame_end = C.frames(telop_row["end"], bounds["fps"])
        snapped_end = C.snap_tail_time(frame_end, bounds)

        gap = None
        if args.whisper:
            gap = whisper_gap_after(args.src, snapped_end, cache_dir,
                                     model=args.model, whisper_bin=args.whisper_bin)
        row = build_row(telop_row, snapped_end, samples, whisper_gap=gap)
        print(format_row(row))
        shown += 1
        if row["caution"]:
            caution_count += 1
    print("---")
    print("%d行中 要注意 %d行" % (shown, caution_count))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
