# -*- coding: utf-8 -*-
"""カット頭尻の発話とテロップの一致を Whisper で検査する（第3段階・必須工程）。
使い方: python3 align_check.py <clips_plan.py> [--telops telops.txt] [--src src.mp4]
        [--cache align_cache] [--model small] [--whisper <path>] [--radius 1.5]

判定：
  ① 頭：カット開始後に最初に発話される語が、カット開始時点のテロップ本文の冒頭と
     一致するか（表記揺れを許容した比較）。カット開始時刻をまたいで続く語があれば
     頭切れとして NG
  ② 尻：カット終点をまたいで続く語が無いこと（最後の語が終点前に終わっている）
  ③ 尻：終点後 0.3 秒未満で次の語が始まっていないか（文中切りの疑い）。
     無音の谷の有無は cutpoints.py が machine的に別途出すため、ここでは
     「発話が詰まっている」ことだけを警告として出す（NGにはしない＝終了コードは
     変えない）。文脈次第で正しい切り方もあり得るため、最終判断は人に委ねる
NG（①②）が1つでもあれば終了コード1。

判定ロジック・Whisper呼び出しの実装は kirinuki_core.speech に共通化されている
（対談スキルと共有するため。第3段階の共通パッケージ化）。ここには
clips_plan.py/telops.txtの読み込みとカット単位の判定オーケストレーション（check_cut）、
main/argparseだけを残す。
"""
import argparse
import importlib.util
import re
import sys

import _core  # noqa: E402
_core.ensure_kirinuki_core_on_path()

from kirinuki_core.speech import (  # noqa: E402
    DEFAULT_RADIUS_SEC,
    DEFAULT_MODEL,
    FALLBACK_WHISPER_BIN,
    HEAD_MATCH_MIN_CHARS,
    HEAD_MATCH_RATIO_THRESHOLD,
    HEAD_COMPARE_MAX_CHARS,
    WORD_OVERLAP_MIN_SEC,
    TRAILING_GAP_WARN_SEC,
    WhisperNotFoundError,
    normalize_speech,
    find_whisper_cli,
    extract_audio_segment,
    run_whisper,
    parse_whisper_words,
    head_word_overlaps_cut,
    tail_word_overlaps_cut,
    head_actual_text,
    match_head,
    check_head,
    check_tail,
    trailing_gap,
    check_trailing_gap,
)
from kirinuki_core import speech as _speech  # noqa: E402
from kirinuki_core.silence import ensure_silence_file, parse_silences, find_silence_interior  # noqa: E402
import subprocess  # noqa: E402,F401  mitchaku-kirinuki/tests が A.subprocess.run を差し替えるため

# subprocess は shutil 同様 mitchaku-kirinuki/tests が `A.shutil.which` /
# `A.os.path.exists` を差し替えるため、モジュール自体をここでも import しておく
# （シングルトンなので、どちらのモジュールでimportしても同じsubprocess/shutil/os
# オブジェクトを指す。差し替えは呼び出し元に依らず有効になる）
import os  # noqa: E402
import shutil  # noqa: E402


def load_words_for_boundary(src, boundary_time, radius, cache_dir, wav_name,
                             model=DEFAULT_MODEL, whisper_bin=None):
    """kirinuki_core.speech の実装を、このモジュール内の（テストで差し替え可能な）
    extract_audio_segment/run_whisper 経由で呼ぶ薄いラッパー
    （mitchaku-kirinuki/tests/test_align_check.py が `A.extract_audio_segment` を
    monkeypatchして検証しているため、その差し替えが効くようにしている）。
    """
    return _speech.load_words_for_boundary(
        src, boundary_time, radius, cache_dir, wav_name, model=model, whisper_bin=whisper_bin,
        extract_fn=extract_audio_segment, run_whisper_fn=run_whisper)


# --- clips_plan.py / telops.txt の読み取り（qc_cuts.py と同じ書式。独立モジュールとして
#     自己完結させるため、あえて import せずここに小さく再実装する） ---

def load_clips_plan(path):
    spec = importlib.util.spec_from_file_location("clips_plan", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_TELOP_LINE_RE = re.compile(r"^\s*(\S+)\s+(\S+)\s+f\s*\d+-\s*\d+\s(.*)$")


def load_telops(path):
    """telops.txt を [(start, end, text), ...]（開始時刻昇順）で返す。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            match = _TELOP_LINE_RE.match(line.rstrip("\n"))
            if not match:
                continue
            start, end, text = match.groups()
            rows.append((float(start), float(end), text.strip()))
    rows.sort(key=lambda r: r[0])
    return rows


def telop_at(rows, t):
    """時刻 t に表示されているはずのテロップ本文を返す。
    telops.txtの行は隣接しており、前の行のendと次の行のstartが同じ値になる。
    カット頭はテロップの最初のフレームにスナップされる（SKILL.md絶対ルール1）ため、
    ちょうど境界の時刻は新しい方のテロップとして扱う（両端を含めると古い方が先に
    マッチしてしまい、新テロップへの切り替わり地点を誤って検査してしまう）。
    ただし最後の行のendちょうどは、後に続く行が無いのでそのまま含める。
    戻り値: 該当する行があればその本文（無テロップの行なら空文字列）。
    どの行にも含まれない時刻（telops.txtが空・壊れている・範囲外）は None を返す。
    空文字列（正規に無テロップ）と None（そもそも確認できていない）を混同すると、
    telops.txtが壊れていても頭チェックが黙って全てOKになってしまう
    （Codexレビューで検出）。
    """
    for i, (start, end, text) in enumerate(rows):
        is_last = i == len(rows) - 1
        if start <= t < end or (is_last and t == end):
            return text
    return None


def boundary_wav_name(ident, label, boundary_time):
    """境界のキャッシュファイル名を作る。時刻を含めるのは、clips_plan.py でカット点を
    動かしたときに古いキャッシュを誤って使い回さないようにするため。
    """
    return "%s_%s_%d" % (ident, label, round(boundary_time * 1000))


def check_cut(src, telop_rows, cut_start, cut_end, cache_dir, ident,
              radius=DEFAULT_RADIUS_SEC, model=DEFAULT_MODEL, whisper_bin=None,
              is_silent_at=None):
    """1カットの頭・尻を判定する。[(NGか, 警告か, ラベル, 時刻, メッセージ), ...] を返す。

    is_silent_at: kirinuki_core.speech.check_head/check_tailにそのまま渡す
    （時刻→実際に無音かを返す関数）。Whisperの単語時刻は無音区間で最大0.75秒ほど
    ずれることが実測されており（対談の合成素材、2026-09-17）、既定のジッタ許容
    （WORD_OVERLAP_MIN_SEC=0.05）では吸収できず頭切れ・尻切れの誤NGになるため。
    """
    head_words = load_words_for_boundary(
        src, cut_start, radius, cache_dir, boundary_wav_name(ident, "head", cut_start),
        model=model, whisper_bin=whisper_bin)
    tail_words = load_words_for_boundary(
        src, cut_end, radius, cache_dir, boundary_wav_name(ident, "tail", cut_end),
        model=model, whisper_bin=whisper_bin)

    head_ok, head_msg = check_head(head_words, cut_start, telop_at(telop_rows, cut_start),
                                    is_silent_at=is_silent_at)
    tail_ok, tail_msg = check_tail(tail_words, cut_end, is_silent_at=is_silent_at)
    gap_warn, gap_msg = check_trailing_gap(tail_words, cut_end)

    return [
        (not head_ok, False, "head", cut_start, head_msg),
        (not tail_ok, False, "tail", cut_end, tail_msg),
        (False, gap_warn, "gap", cut_end, gap_msg),
    ]


# --- 実際の無音判定（silence30.txt） ---

IS_SILENT_INTERIOR_MARGIN_SEC = 0.015


def make_is_silent_at(silence_path, src):
    """silence30.txt（無ければensure_silence_fileで作る）を読み、時刻→無音かを返す
    is_silent_at(t)を作る。カット点が無音区間の内側（端から0.015秒以上離れている）に
    入っていれば無音とみなす（cutpoints.pyの候補表と同じsilencedetect -30dBの区間を
    流用する）。

    以前は区間の外側でも±0.03秒以内なら無音扱いにしていたが、これはゆるすぎて
    本物の頭切れ・尻切れを見逃す（team-leadの実測：発話が0.99秒から始まりカット点が
    1.00秒の例。2026-09-17）。find_silence_interiorで区間の端に近すぎる場合も
    除外する。
    """
    ensure_silence_file(silence_path, src)
    with open(silence_path, encoding="utf-8") as f:
        silences = parse_silences(f.read())

    def is_silent_at(t):
        return find_silence_interior(silences, t, margin=IS_SILENT_INTERIOR_MARGIN_SEC) is not None
    return is_silent_at


# --- メイン処理 ---

def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("clips_plan")
    ap.add_argument("--telops", default="telops.txt")
    ap.add_argument("--src", default="src.mp4")
    ap.add_argument("--cache", default="align_cache")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--whisper", default=None)
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_SEC)
    ap.add_argument("--silence", default="silence30.txt")
    return ap.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    M = load_clips_plan(args.clips_plan)
    telop_rows = load_telops(args.telops)
    is_silent_at = make_is_silent_at(args.silence, args.src)

    all_ok = True
    for clip_index, clip in enumerate(M.CLIPS):
        for cut_index, (start, end) in enumerate(clip["cuts"]):
            ident = "c%02d_cut%02d" % (clip_index, cut_index)
            rows = check_cut(args.src, telop_rows, start, end, args.cache, ident,
                              radius=args.radius, model=args.model, whisper_bin=args.whisper,
                              is_silent_at=is_silent_at)
            for is_ng, is_warn, label, t, msg in rows:
                print("%s c%d %-4s %8.3fs %s" % (clip["file"], cut_index, label, t, msg))
                if is_ng:
                    all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
