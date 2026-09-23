# -*- coding: utf-8 -*-
"""カット頭尻の発話とテロップの一致を Whisper で検査する（第3段階・対談スキルへの展開）。
使い方: python3 align_check.py <src.mp4> <workdir> <clips.py> [--only slug ...]
        [--model small] [--whisper <path>] [--radius 1.5]

判定：
  ① 頭：カット開始後に最初に発話される語が、カット開始時点のテロップ本文の冒頭と
     一致するか（表記揺れを許容した比較）。カット開始時刻をまたいで続く語があれば
     頭切れとして NG
  ② 尻：カット終点をまたいで続く語が無いこと（最後の語が終点前に終わっている）
  ③ 尻：終点後 0.3 秒未満で次の語が始まっていないか（文中切りの疑い。警告のみ、
     NGにはしない。最終判断は人に委ねる）
NG（①②）が1つでもあれば終了コード1。

C1→C2の白フラッシュでつながるC1尻・C2頭も検査する（音声はacrossfadeだが、語が
切れていれば聞こえ方は同じく壊れるため。qc.pyのOCR検査とは異なりスキップしない）。
頭の期待テロップは「カット開始＋半フレーム」の時刻でtelops.txtから引く（カット開始は
common.cut_rangeで求めた(f0-0.5)/fpsで、境界ちょうどだと前のテロップを引いてしまうため）。

カット区間は common.cut_range（＝render.pyが実際に切る時刻）で求める。
判定ロジック・Whisper呼び出しの実装は kirinuki_core.speech に共通化されている
（密着スキルと共有するため。第3段階の共通パッケージ化）。
"""
import argparse
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
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
import subprocess  # noqa: E402,F401  tests が A.subprocess.run を差し替えるため
import shutil  # noqa: E402,F401  tests が A.shutil.which を差し替えるため


def load_words_for_boundary(src, boundary_time, radius, cache_dir, wav_name,
                             model=DEFAULT_MODEL, whisper_bin=None):
    """kirinuki_core.speech の実装を、このモジュール内の（テストで差し替え可能な）
    extract_audio_segment/run_whisper 経由で呼ぶ薄いラッパー。
    """
    return _speech.load_words_for_boundary(
        src, boundary_time, radius, cache_dir, wav_name, model=model, whisper_bin=whisper_bin,
        extract_fn=extract_audio_segment, run_whisper_fn=run_whisper)


# --- telops.txt の読み取り（subtext.pyの書式） ---

_TELOP_LINE_RE = re.compile(r"^\s*(\d+)\s+([\d.]+)-\s*([\d.]+)\s+\(\s*([\d.]+)s\)\s?(.*)$")


def load_telops(path):
    """telops.txt を [(start, end, text), ...]（開始時刻昇順）で返す。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            match = _TELOP_LINE_RE.match(line.rstrip("\n"))
            if not match:
                continue
            _index, start, end, _dur, text = match.groups()
            rows.append((float(start), float(end), text.strip()))
    rows.sort(key=lambda r: r[0])
    return rows


def telop_at(rows, t):
    """時刻 t に表示されているはずのテロップ本文を返す（境界ちょうどは新しい方を優先。
    どの行にも含まれない時刻はNone＝「無テロップ」と区別する。mitchaku-kirinukiと同じ規約）。

    tはsubtext.pyが telops.txt に書き出したのと同じ "%.3f" 丸めを通してから比較する。
    check_cut が渡す t（カット開始+半フレーム）は区間境界と数学的には一致するが、
    telops.txt の時刻はミリ秒単位に丸めて書かれているため、丸めていない生の浮動小数の
    ままだと丸め方向次第で新テロップの行の start をわずかに下回り、1つ前の行に
    誤って一致してしまうことがある（Codexレビューで検出：frame101@30fpsで再現）。
    """
    t = float("%.3f" % t)
    for i, (start, end, text) in enumerate(rows):
        is_last = i == len(rows) - 1
        if start <= t < end or (is_last and t == end):
            return text
    return None


def boundary_wav_name(ident, label, boundary_time):
    return "%s_%s_%d" % (ident, label, round(boundary_time * 1000))


def head_query_time(cut_start, fps):
    """telop_atに渡す「カット開始+半フレーム」の時刻を、telops.txtの書き出しと
    同じ丸め誤差になるように計算し直す。cut_start(=(f0-0.5)/fps)に0.5/fpsを
    足し戻す多段の浮動小数演算は、subtext.pyがnew_fa/fpsを1回の除算で書き出す
    のとわずかに違う値になることがあり、30000/1001のような比の悪いfpsでは
    "%.3f"丸めのバケット自体がずれてしまう（Codexレビューで検出：frame135@29.97fps
    で cut_start+half_frame=4.504 だが telops.txt側は4.505）。
    そのためcut_startから整数フレームf0を復元し、f0/fpsを1回の除算で計算し直す。
    """
    f0 = int(round(cut_start * fps + 0.5))
    return f0 / fps


def check_cut(src, telop_rows, cut_start, cut_end, cache_dir, ident, fps,
              radius=DEFAULT_RADIUS_SEC, model=DEFAULT_MODEL, whisper_bin=None,
              skip_head=False, skip_tail=False, is_silent_at=None):
    """1カットの頭・尻を判定する。[(NGか, 警告か, ラベル, 時刻, メッセージ), ...] を返す。
    頭の期待テロップは cut_start より半フレーム先の時刻で引く（境界ちょうどで
    前のテロップを引かないようにするため）。

    skip_head/skip_tail: 前後のカットと地続き（実際には切っていない）場合、その側の
    判定をスキップする（chop.check_clipと同じ規約。地続きの境界では、同じ発話が
    両方のカットにまたがって存在するのが正常であり、頭切れ・尻切れとして誤検出する
    ため。Codexレビューで検出：地続きのカット境界を独立に検査して誤ってNGにしていた）。

    is_silent_at: kirinuki_core.speech.check_head/check_tailにそのまま渡す
    （時刻→実際に無音かを返す関数）。Whisperの単語時刻は無音区間で最大0.75秒ほど
    ずれることが実測されており（対談の合成素材、2026-09-17）、既定のジッタ許容
    （WORD_OVERLAP_MIN_SEC=0.05）では吸収できず頭切れ・尻切れの誤NGになるため。
    """
    rows = []
    if not skip_head:
        head_words = load_words_for_boundary(
            src, cut_start, radius, cache_dir, boundary_wav_name(ident, "head", cut_start),
            model=model, whisper_bin=whisper_bin)
        head_ok, head_msg = check_head(head_words, cut_start,
                                        telop_at(telop_rows, head_query_time(cut_start, fps)),
                                        is_silent_at=is_silent_at)
        rows.append((not head_ok, False, "head", cut_start, head_msg))
    if not skip_tail:
        tail_words = load_words_for_boundary(
            src, cut_end, radius, cache_dir, boundary_wav_name(ident, "tail", cut_end),
            model=model, whisper_bin=whisper_bin)
        tail_ok, tail_msg = check_tail(tail_words, cut_end, is_silent_at=is_silent_at)
        gap_warn, gap_msg = check_trailing_gap(tail_words, cut_end)
        rows.append((not tail_ok, False, "tail", cut_end, tail_msg))
        rows.append((False, gap_warn, "gap", cut_end, gap_msg))
    return rows


SILENCE_CHECK_RATE = 16000


def make_is_silent_at(samples):
    """common.is_silent_point（カット点を中心にした±15msの窓自体のレベルを見る）を
    kirinuki_core.speechに渡すis_silent_at(t)として作る。音声は呼び出し側で
    common.load_pcmを使い全編を1回だけ読み込む（境界ごとにffmpegを呼ばない）。

    common.has_silence_gap（chop.pyの★ぶつ切り判定と同じ、前後どちらかが静かならOKと
    いう緩い判定）は使わない。発話がカット点そのものにかかっている場合（例：発話が
    0.99秒から始まりカット点が1.00秒）でも、前側の窓だけを見て無音と誤判定し、
    本物の頭切れ・尻切れを見逃してしまうため（team-leadの実測、2026-09-17）。
    chop.pyの判定（has_silence_gap）自体は変えない。
    """
    def is_silent_at(t):
        return C.is_silent_point(samples, SILENCE_CHECK_RATE, t)
    return is_silent_at


def contiguity_skips(cut_index, n, tl, xfade):
    """cut_indexの頭・尻それぞれについて、前後のカットと地続き（実際には切っていない）
    ため検査を除外してよいかを返す (skip_head, skip_tail)。

    C1→C2のフラッシュ境界（cut0の尻・cut1の頭。xfade>0のときrender.pyがacrossfadeで
    実際に音声を混ぜ合わせる）は、源の時刻がたまたま地続きであっても除外しない
    （Codexレビューで検出：地続き判定だけで見ると、フラッシュで実際には音声が
    重ね合わされる境界まで誤って検査対象から外してしまっていた）。
    """
    skip_head = (cut_index > 0
                 and C.is_contiguous_join(tl[cut_index - 1][1], tl[cut_index][0])
                 and not (xfade > 0 and cut_index == 1))
    skip_tail = (cut_index < n - 1
                 and C.is_contiguous_join(tl[cut_index][1], tl[cut_index + 1][0])
                 and not (xfade > 0 and cut_index == 0))
    return skip_head, skip_tail


# --- メイン処理 ---

def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("workdir")
    ap.add_argument("clips")
    ap.add_argument("--only", nargs="*", default=[], help="対象スラッグ（既定＝全部）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--whisper", default=None)
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_SEC)
    ap.add_argument("--cache", default=None)
    return ap.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    M = C.load_clips(args.clips)
    bounds = C.load_bounds(args.workdir)
    telop_rows = load_telops(os.path.join(args.workdir, "telops.txt"))
    cache_dir = args.cache or os.path.join(args.workdir, "align_cache")
    is_silent_at = make_is_silent_at(C.load_pcm(args.src, rate=SILENCE_CHECK_RATE))

    which = set(args.only) or {c["slug"] for c in M.CLIPS}
    all_ok = True
    for clip in M.CLIPS:
        if clip["slug"] not in which:
            continue
        cuts = clip["cuts"]
        xfade = clip.get("xfade", M.XFADE)
        tl = [C.cut_range(cut, bounds) for cut in cuts]
        n = len(tl)
        for cut_index, (cut, (start, end)) in enumerate(zip(cuts, tl)):
            skip_head, skip_tail = contiguity_skips(cut_index, n, tl, xfade)
            ident = "%s_cut%02d" % (clip["slug"], cut_index)
            rows = check_cut(args.src, telop_rows, start, end, cache_dir, ident, bounds["fps"],
                              radius=args.radius, model=args.model, whisper_bin=args.whisper,
                              skip_head=skip_head, skip_tail=skip_tail, is_silent_at=is_silent_at)
            for is_ng, is_warn, label, t, msg in rows:
                print("%s c%d %-4s %8.3fs %s" % (clip["slug"], cut_index, label, t, msg))
                if is_ng:
                    all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
