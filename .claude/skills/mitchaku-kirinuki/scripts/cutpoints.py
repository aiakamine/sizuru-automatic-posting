# -*- coding: utf-8 -*-
"""telops.txt × 無音(-30dB) × Whisperの語(任意) を突き合わせ、行ごとの
「語尾分類・終点前後の無音・次の発話までの間」を機械的に出す候補表（SKILL.md手順3）。
使い方: python3 cutpoints.py [telops.txt] [--silence silence30.txt] [--src src.mp4]
        [--whisper] [--model small] [--whisper-bin <path>] [--cache cutpoints_cache]
silence30.txt が無ければ ffmpeg silencedetect=-30dB:d=0.08 で生成する。
--whisper を付けると、各行の終点付近をWhisperで解析し「次の発話までの間」を実測する
（付けない場合は「?（Whisperなし）」と表示し、無音の情報だけで判断する）。

語尾分類・無音判定は kirinuki_core（endings.py/silence.py）に共通化されている
（対談スキルと共有するため。第3段階の共通パッケージ化）。ここには telops.txt の
読み取り（書式がスキルごとに違う）とWhisper連携・main/argparseだけを残す。
"""
import argparse
import os
import re
import sys

import _core  # noqa: E402
_core.ensure_kirinuki_core_on_path()

# align_check.py と音声解析まわりの実装を共有する（同じタスクで新設した兄弟モジュール）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import align_check as AC  # noqa: E402

from kirinuki_core.endings import classify_ending, caution_reasons  # noqa: E402
from kirinuki_core.silence import (  # noqa: E402
    SILENCE_TOLERANCE_SEC,
    parse_silences,
    ensure_silence_file,
    find_silence_near,
)
# subprocess はシングルトンなので、CP.subprocess.run を差し替えるテスト
# （mitchaku-kirinuki/tests/test_cutpoints.py の EnsureSilenceFileTest）が
# kirinuki_core.silence 内の実際の呼び出しにもそのまま効く。import自体は
# このモジュール自身では使わなくなったが、`CP.subprocess` としての参照を保つため残す。
import subprocess  # noqa: E402,F401


# --- telops.txt の読み取り（frameとテキストの両方を保持する） ---

_TELOP_LINE_RE = re.compile(r"^\s*(\S+)\s+(\S+)\s+f\s*(\d+)-\s*(\d+)\s(.*)$")


def load_telop_rows(path):
    """telops.txt を [{"start","end","frame_start","frame_end","text"}, ...] で返す。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _TELOP_LINE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            start, end, fa, fb, text = m.groups()
            rows.append({
                "start": float(start), "end": float(end),
                "frame_start": int(fa), "frame_end": int(fb),
                "text": text.strip(),
            })
    rows.sort(key=lambda r: r["start"])
    return rows


def load_silences(path):
    with open(path, encoding="utf-8") as f:
        return parse_silences(f.read())


# --- Whisperによる「次の発話までの間」（任意） ---

def whisper_gap_after(src, line_end, cache_dir, model=AC.DEFAULT_MODEL, whisper_bin=None,
                       radius=AC.DEFAULT_RADIUS_SEC):
    """line_end 以降の最初の発話までの間隔（秒）をWhisperで求める。align_check.py と
    同じヘルパー（音声切り出し・Whisper実行・キャッシュ）を再利用する。
    """
    wav_name = "line_%d" % round(line_end * 1000)
    words = AC.load_words_for_boundary(src, line_end, radius, cache_dir, wav_name,
                                        model=model, whisper_bin=whisper_bin)
    return AC.trailing_gap(words, line_end)


# --- 行の組み立て ---

def build_row(telop_row, silences, whisper_gap=None):
    """1行分の候補表データを組み立てる。"""
    ending_class = classify_ending(telop_row["text"])
    silence_hit = find_silence_near(silences, telop_row["end"])
    reasons = caution_reasons(ending_class, silence_hit, whisper_gap)
    return {
        "start": telop_row["start"], "end": telop_row["end"],
        "frame_start": telop_row["frame_start"], "frame_end": telop_row["frame_end"],
        "text": telop_row["text"], "ending_class": ending_class,
        "silence": silence_hit, "gap": whisper_gap,
        "caution": bool(reasons), "reasons": reasons,
    }


def format_row(row):
    silence_desc = "谷%.2fs" % row["silence"][2] if row["silence"] else "谷なし"
    gap_desc = "%.2fs" % row["gap"] if row["gap"] is not None else "?"
    mark = "要注意" if row["caution"] else "OK"
    reason_desc = "（%s）" % "・".join(row["reasons"]) if row["reasons"] else ""
    return "f%6d-%6d %8.3f-%8.3f %-6s 無音:%-8s 次発話:%-6s %-4s%s %s" % (
        row["frame_start"], row["frame_end"], row["start"], row["end"],
        row["ending_class"], silence_desc, gap_desc, mark, reason_desc, row["text"])


# --- メイン処理 ---

def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("telops", nargs="?", default="telops.txt")
    ap.add_argument("--silence", default="silence30.txt")
    ap.add_argument("--src", default="src.mp4")
    ap.add_argument("--whisper", action="store_true", help="次の発話までの間をWhisperで実測する")
    ap.add_argument("--model", default=AC.DEFAULT_MODEL)
    ap.add_argument("--whisper-bin", default=None)
    ap.add_argument("--cache", default="cutpoints_cache")
    return ap.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    ensure_silence_file(args.silence, args.src)
    telop_rows = load_telop_rows(args.telops)
    silences = load_silences(args.silence)

    caution_count = 0
    for telop_row in telop_rows:
        if not telop_row["text"]:
            continue
        gap = None
        if args.whisper:
            gap = whisper_gap_after(args.src, telop_row["end"], args.cache,
                                     model=args.model, whisper_bin=args.whisper_bin)
        row = build_row(telop_row, silences, whisper_gap=gap)
        print(format_row(row))
        if row["caution"]:
            caution_count += 1
    print("---")
    print("%d行中 要注意 %d行" % (len(telop_rows), caution_count))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
