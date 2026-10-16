# -*- coding: utf-8 -*-
"""カット頭尻のOCR検査。render.py で書き出した出力mp4と、同時に書き出される
<out>/<file>.timeline.json（唯一の座標系＝各カットのフレーム番号）を突き合わせて
テロップの混入・欠落を検出する。
使い方: python3 qc_cuts.py <clips_plan.py> [--out out] [--telops telops.txt]
        [--ocrcli ./ocrcli]
clips_plan.py: CLIPS=[{file, l1, l2, quote, cuts:[(start,end),...]}, ...]
timeline.json が無いクリップは「先に render.py を実行してください」と案内して
終了コード1にする（render.py のフレーム量子化とずれた座標で検査を続けない）。
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from difflib import SequenceMatcher

FPSF = 30000.0 / 1001.0

# 出力側のテロップ帯クロップ（映像バンド1080x606@y=552内、元テロップ帯y=840-1044(1080基準)
# → 出力y≈1010-1150 に相当）
BAND_X, BAND_Y, BAND_W, BAND_H = 0, 1010, 1080, 140


class TimelineMissingError(Exception):
    """<out>/<file>.timeline.json が無い（render.py が未実行）ことを表す。"""


# --- timeline.json の読み取りと検査点の組み立て ---

def timeline_path(out_dir, filename):
    return os.path.join(out_dir, filename + ".timeline.json")


def load_timeline(out_dir, filename):
    """render.py が書き出した timeline.json を読む。無ければ TimelineMissingError を送出する。"""
    path = timeline_path(out_dir, filename)
    if not os.path.exists(path):
        raise TimelineMissingError(path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _checkpoint(cut, cut_index, label, offset_in_cut):
    src_frame = cut["src_start_frame"] + offset_in_cut
    return {
        "cut": cut_index,
        "label": label,
        "out_frame": cut["out_start_frame"] + offset_in_cut,
        "src_frame": src_frame,
        "src_time": src_frame / FPSF,
    }


def build_checkpoints(timeline):
    """timeline.json から各カットの検査点（頭+2f・尻2f）を組み立てる。
    フラッシュ区間（先頭カットの尻とその直後のカットの頭）はクロスフェードで
    テロップが混ざって読めない区間なので、従来どおり検査対象から外す。
    """
    cuts = timeline["cuts"]
    flash_after = timeline.get("flash_after")
    n = len(cuts)
    has_flash = flash_after is not None and 0 <= flash_after < n - 1

    points = []
    for cut in cuts:
        i = cut["index"]
        nf = cut["n_frames"]
        skip_head = has_flash and i == flash_after + 1
        skip_tail = has_flash and i == flash_after
        if not skip_head:
            points.append(_checkpoint(cut, i, "head", 0))
            if nf >= 2:
                points.append(_checkpoint(cut, i, "head2", 1))
        if not skip_tail:
            points.append(_checkpoint(cut, i, "tail", nf - 1))
            if nf >= 2:
                points.append(_checkpoint(cut, i, "tail2", nf - 2))
    return points


# --- フレーム抽出コマンド ---

def extract_frame_cmd(video_path, out_frame, out_png):
    """出力側のフレーム番号 out_frame を select=eq(n,K) で直接指定して1枚抜き出す。
    render.py 側でフレーム格子にスナップ済みの座標系なので、時刻の再計算によるずれが無い。
    """
    vf = "select=eq(n\\,%d),crop=%d:%d:%d:%d" % (out_frame, BAND_W, BAND_H, BAND_X, BAND_Y)
    return ["ffmpeg", "-v", "error", "-y", "-i", video_path,
            "-vf", vf, "-vsync", "0", "-frames:v", "1", out_png]


# --- telops.txt の読み取り ---

# subscan.py の書式: "{start:8.3f} {end:8.3f} f{a:6d}-{b:6d} {text}"
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


def expected_telop_at(rows, t):
    """時刻 t に表示されているはずのテロップ本文を返す（該当が無ければ空文字列）。
    行は隙間なく連続するので半開区間 start <= t < end で引く（境界ちょうどは次の行）。"""
    for start, end, text in rows:
        if start <= t < end:
            return text
    return ""


# subscan.py の書式からフレーム番号を読む: "{start:8.3f} {end:8.3f} f{a:6d}-{b:6d} {text}"
_TELOP_FRAME_RE = re.compile(r"^\s*\S+\s+\S+\s+f\s*(\d+)-\s*(\d+)\s(.*)$")


def load_telop_frames(path):
    """telops.txt を [(開始フレーム, 終了フレーム, text), ...]（開始フレーム昇順）で返す。
    秒は小数3桁に丸められているため、境界の判定は丸めの無いフレーム番号で行う。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            match = _TELOP_FRAME_RE.match(line.rstrip("\n"))
            if not match:
                continue
            a, b, text = match.groups()
            rows.append((int(a), int(b), text.strip()))
    rows.sort(key=lambda r: r[0])
    return rows


def expected_telop_at_frame(rows, frame):
    """元データのフレーム番号 frame に表示されているはずのテロップ本文を返す。
    半開区間 開始 <= frame < 終了 で引く。カット頭（新しいテロップの最初のフレーム）を
    直前の行として扱う誤判定を防ぐ（2026-10-13 凪さん密着で毎回8〜11件出ていた）。"""
    for a, b, text in rows:
        if a <= frame < b:
            return text
    return ""


# --- OCR読み揺れの正規化と判定（scan_qc.py と同じ規則。共通化はしない） ---

_CONFUSABLE_TO_EQUALS = {"三": "=", "＝": "="}


def _fullwidth_alnum_table():
    table = {}
    for i in range(10):
        table[0xFF10 + i] = str(i)
    for i in range(26):
        table[0xFF21 + i] = chr(ord("A") + i)
        table[0xFF41 + i] = chr(ord("a") + i)
    return table


_FULLWIDTH_ALNUM_TABLE = _fullwidth_alnum_table()
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text):
    for src, dst in _CONFUSABLE_TO_EQUALS.items():
        text = text.replace(src, dst)
    text = text.translate(_FULLWIDTH_ALNUM_TABLE)
    return _WHITESPACE_RE.sub("", text)


# 前方一致で「実際が期待の途中で切れている」場合、極端に短い一致（例: 期待"Hello world"に
# 実際"H"）まで合格にしてしまわないよう、実際が期待の最低このぶんの長さは無いと認めない。
_PREFIX_MATCH_MIN_LENGTH_RATIO = 0.5


def judge(expected, actual):
    """(合格か, 理由文字列) を返す。
    期待が空なら実際も空でOK。期待があれば、正規化後に前方一致 or 類似度0.6以上でOK。
    """
    exp_norm = normalize_text(expected)
    act_norm = normalize_text(actual)
    if not exp_norm:
        if not act_norm:
            return True, "OK（無テロップ）"
        return False, "NG（無テロップのはずが検出: %s）" % actual
    if not act_norm:
        return False, "NG（期待: %s / 実際: 空）" % expected
    if act_norm.startswith(exp_norm):
        return True, "OK（前方一致）"
    if exp_norm.startswith(act_norm) and len(act_norm) >= _PREFIX_MATCH_MIN_LENGTH_RATIO * len(exp_norm):
        return True, "OK（前方一致）"
    ratio = SequenceMatcher(None, exp_norm, act_norm).ratio()
    if ratio >= 0.6:
        return True, "OK（類似度%.2f）" % ratio
    return False, "NG（期待: %s / 実際: %s）" % (expected, actual)


def judge_point(expected, png, ocr, ocr_failed):
    """1検査点の合否と理由を返す。OCRプロセス失敗・結果欠落・フレーム抽出失敗は、
    たとえ期待テロップが空でも「空テロップとして合格」にはしない（明確にNGとして報告する）。
    """
    if png is None:
        return False, "NG（フレームの抽出に失敗）"
    if ocr_failed:
        return False, "NG（OCRプロセスが失敗）"
    if png not in ocr:
        return False, "NG（OCR結果が欠落）"
    return judge(expected, ocr[png])


def run_ocr(ocrcli, paths):
    """(結果dict, プロセス自体が失敗したか) を返す。プロセスが失敗した場合、
    その回の対象ファイルは全て結果に含めない（呼び出し側でNG扱いにする）。
    """
    if not paths:
        return {}, False
    r = subprocess.run([ocrcli] + paths, capture_output=True, text=True)
    if r.returncode != 0:
        return {}, True
    result = {}
    for line in r.stdout.split("\n"):
        if "\t" not in line:
            continue
        p, t = line.split("\t", 1)
        result[p] = t.strip()
    return result, False


# --- メイン処理 ---

def load_clips_plan(path):
    spec = importlib.util.spec_from_file_location("clips_plan", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("clips_plan")
    ap.add_argument("--out", default="out")
    ap.add_argument("--telops", default="telops.txt")
    ap.add_argument("--ocrcli", default="./ocrcli")
    return ap.parse_args(argv)


def clip_identifier(index, filename):
    """一時ファイル名の衝突を避けるため、連番＋basename（ディレクトリ成分を除く）を使う。"""
    base = os.path.splitext(os.path.basename(filename))[0]
    return "%02d_%s" % (index, base)


def _extract_frames(video_path, points, tmp_dir, ident):
    """各 point に対応する抽出済みPNGパスのリストを、points と同じ順序で返す。
    抽出できなかった（select がヒットしなかった等）場合は None を入れる。
    """
    frame_paths = []
    for idx, point in enumerate(points):
        png = os.path.join(tmp_dir, "%s_%02d.png" % (ident, idx))
        subprocess.run(extract_frame_cmd(video_path, point["out_frame"], png), check=True)
        frame_paths.append(png if os.path.exists(png) else None)
    return frame_paths


def check_clip(clip_index, clip, args, telop_rows, tmp_dir):
    """1本のクリップを検査し、[(合格か, 表示行文字列), ...] を返す。
    timeline.json が無ければ TimelineMissingError を送出する（呼び出し側で案内する）。
    """
    video_path = os.path.join(args.out, clip["file"])
    timeline = load_timeline(args.out, clip["file"])
    points = build_checkpoints(timeline)
    ident = clip_identifier(clip_index, clip["file"])

    frame_paths = _extract_frames(video_path, points, tmp_dir, ident)

    ocr_targets = [p for p in frame_paths if p is not None]
    ocr, ocr_failed = run_ocr(args.ocrcli, ocr_targets)

    results = []
    for png, point in zip(frame_paths, points):
        expected = expected_telop_at_frame(telop_rows, point["src_frame"])
        ok, reason = judge_point(expected, png, ocr, ocr_failed)
        row = "%s c%d %-6s out_frame=%6d src=%8.3fs %s" % (
            clip["file"], point["cut"], point["label"],
            point["out_frame"], point["src_time"], reason)
        results.append((ok, row))
    return results


def main(argv):
    args = parse_args(argv)
    M = load_clips_plan(args.clips_plan)
    telop_rows = load_telop_frames(args.telops)

    all_ok = True
    with tempfile.TemporaryDirectory() as tmp_dir:
        for clip_index, clip in enumerate(M.CLIPS):
            try:
                clip_results = check_clip(clip_index, clip, args, telop_rows, tmp_dir)
            except TimelineMissingError as exc:
                print("ERROR: %s: %s がありません。先に render.py を実行してください"
                      % (clip.get("file", "?"), exc), file=sys.stderr)
                return 1
            except ValueError as exc:
                print("ERROR: %s: %s" % (clip.get("file", "?"), exc), file=sys.stderr)
                return 1
            for ok, row in clip_results:
                print(row)
                if not ok:
                    all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
