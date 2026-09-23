# -*- coding: utf-8 -*-
"""出力動画の全編テロップQC。5fpsでOCRし、0.5秒未満しか表示されないテロップ
（＝カット境界で切られたフリッカー）を検出する。
使い方: python3 scan_qc.py out/*.mp4
前提: カレントディレクトリに ocrcli（swiftc -O -o ocrcli ocr.swift でビルド）
帯クロップは 1080x1920 出力・映像バンド1080x606@y552 前提（テロップ帯→出力y=1040-1160）。
作業画像はカレントを汚さないよう tempfile.TemporaryDirectory に置く（前回実行の残骸や、
呼び出し側の scanf/ を誤って消す事故を避ける）。
"""
import glob
import os
import re
import subprocess
import sys
import tempfile
from difflib import SequenceMatcher

OCR_BATCH_SIZE = 200

# --- OCR読み揺れの正規化 ---

_CONFUSABLE_TO_EQUALS = {"三": "=", "＝": "="}


def _fullwidth_alnum_table():
    table = {}
    for i in range(10):
        table[0xFF10 + i] = str(i)             # ０-９ → 0-9
    for i in range(26):
        table[0xFF21 + i] = chr(ord("A") + i)  # Ａ-Ｚ → A-Z
        table[0xFF41 + i] = chr(ord("a") + i)  # ａ-ｚ → a-z
    return table


_FULLWIDTH_ALNUM_TABLE = _fullwidth_alnum_table()
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_ocr_text(text):
    """OCRの読み揺れを吸収する：紛らわしい記号を「=」に統一、全角英数を半角化、空白を除去する。"""
    for src, dst in _CONFUSABLE_TO_EQUALS.items():
        text = text.replace(src, dst)
    text = text.translate(_FULLWIDTH_ALNUM_TABLE)
    return _WHITESPACE_RE.sub("", text)


def sim(a, b):
    if not a and not b:
        return 1
    return SequenceMatcher(None, a, b).ratio()


def merge_segments(frames_for_video, expected_indices):
    """{フレーム番号: OCRテキスト} を正規化した類似度で結合する。
    expected_indices にあって frames_for_video に無い番号は「OCR結果が欠落」として報告し、
    欠落を挟んだ前後のフレームは連結しない（誤って連続表示扱いにしない）。
    戻り値: ([開始, 終了, 表示用テキスト] の列, 欠落したフレーム番号の昇順リスト)
    """
    segs = []
    missing = []
    prev_present = False
    for i in sorted(expected_indices):
        if i not in frames_for_video:
            missing.append(i)
            prev_present = False
            continue
        t = frames_for_video[i]
        if prev_present and segs and sim(normalize_ocr_text(t), normalize_ocr_text(segs[-1][2])) > 0.55:
            segs[-1][1] = i
            if len(t) > len(segs[-1][2]):
                segs[-1][2] = t
        else:
            segs.append([i, i, t])
        prev_present = True
    return segs, missing


# --- ファイル管理 ---

def video_identifier(index, path):
    """動画の識別子＝連番＋拡張子を除いたファイル名（ディレクトリ成分は含めない）。
    連番を含めるのは、異なるディレクトリの同名ファイルが衝突しないようにするため。
    """
    base = os.path.splitext(os.path.basename(path))[0]
    return "%02d_%s" % (index, base)


SCANF_FILENAME_RE = re.compile(r"^(.+)_(\d+)\.png$")


def run_ocr_batches(ocrcli, files, batch_size=OCR_BATCH_SIZE):
    """OCR結果を {path: text} で返す。batch_size件ずつに分けて呼び出し、
    バッチが失敗（非0終了）したら、そのバッチ内のファイルは結果に含めない（欠落として扱う）。
    """
    results = {}
    for i in range(0, len(files), batch_size):
        chunk = files[i:i + batch_size]
        r = subprocess.run([ocrcli] + chunk, capture_output=True, text=True)
        if r.returncode != 0:
            continue
        for line in r.stdout.split("\n"):
            if "\t" not in line:
                continue
            p, t = line.split("\t", 1)
            results[p] = t.strip()
    return results


# --- メイン処理 ---

def main(argv):
    ok = True
    with tempfile.TemporaryDirectory() as tmp_dir:
        for index, p in enumerate(argv):
            ident = video_identifier(index, p)
            subprocess.run(
                ["ffmpeg", "-v", "error", "-i", p,
                 "-vf", "fps=5,crop=1080:120:0:1040", "-q:v", "3",
                 os.path.join(tmp_dir, "%s_%%04d.png" % ident), "-y"],
                check=True,
            )

        files = sorted(glob.glob(os.path.join(tmp_dir, "*.png")))
        ocr = run_ocr_batches("./ocrcli", files)

        by_video = {}
        for path in files:
            m = SCANF_FILENAME_RE.match(os.path.basename(path))
            if not m:
                continue
            ident, seq = m.group(1), int(m.group(2))
            by_video.setdefault(ident, {})[seq] = path

        for ident in sorted(by_video):
            index_to_path = by_video[ident]
            frames_for_video = {
                i: ocr[path] for i, path in index_to_path.items() if path in ocr
            }
            segs, missing = merge_segments(frames_for_video, index_to_path.keys())
            if missing:
                print("⚠%s OCR結果が欠落したフレーム: %s" % (ident, missing))
                ok = False
            for a, b, t in segs:
                dur = (b - a + 1) * 0.2
                if t and dur < 0.5:
                    print("★%s %6.1fs %.1fs だけ表示: %s" % (ident, (a - 1) * 0.2, dur, t[:40]))
                    ok = False

    print("QC OK — フリッカーなし" if ok else "要修正あり（フラッシュ窓内なら許容の場合あり。目視確認）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
