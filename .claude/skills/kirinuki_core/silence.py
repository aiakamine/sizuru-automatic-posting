# -*- coding: utf-8 -*-
"""ffmpeg silencedetect の出力の読み取り・生成まわりの共通ロジック
（mitchaku-kirinuki/scripts/cutpoints.py から移設）。
telops.txt読み取り（load_telop_rows）は各スキルの書式に依存するため各スキルに残す。
"""
import json
import os
import re
import subprocess

# 終点前後、この秒数以内に無音区間があれば「無音の谷あり」とする
# （telopsの境界時刻はOCRベースでカット点ほど正確ではないため、pauses.py(taidan)の
#   0.02秒より広めに取る）
SILENCE_TOLERANCE_SEC = 0.05

_SILENCE_TOKEN_RE = re.compile(r"silence_(start|end):\s*([-\d.]+)")


def parse_silences(text):
    """ffmpeg silencedetect の出力テキストから [(start, end), ...] を返す。
    frame=... 等の進捗ログが同じ物理行に混ざっていても(実データで確認済み)、
    トークンを行単位ではなくテキスト全体から正規表現で拾うことで壊れない。
    """
    intervals = []
    pending_start = None
    for m in _SILENCE_TOKEN_RE.finditer(text):
        kind, value = m.group(1), float(m.group(2))
        if kind == "start":
            pending_start = value
        elif pending_start is not None:
            intervals.append((pending_start, value))
            pending_start = None
    return intervals


def _silence_meta_path(path):
    return path + ".meta.json"


def _read_silence_meta(meta_path):
    """meta.jsonを読む。存在しない・壊れている（JSONとして読めない）・中身がdictで
    ないときはNone（＝無効）を返す。他の無効条件（src不一致・noise_db/min_duration
    不一致）と同じく、黙って再生成させるため（Codexレビューで検出：以前は
    json.load()がJSONDecodeErrorを送出してensure_silence_fileごと落ちていた）。
    """
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def _silence_meta_matches(meta, src, noise_db, min_duration):
    """既存のsilence30.txtが、今回の呼び出しと同じ条件（src・その内容・noise_db・
    min_duration）で作られたかを見る。srcの絶対パスの一致に加えmtimeも見る（同じ
    パスのsrc.mp4を再ダウンロード・再書き出ししても気づけるようにするため。
    speech.pyのWhisperキャッシュ検証と同じ考え方）。
    """
    if meta is None:
        return False
    try:
        src_mtime = os.path.getmtime(src)
    except OSError:
        src_mtime = None
    return (meta.get("src") == os.path.abspath(src)
            and meta.get("src_mtime") == src_mtime
            and meta.get("noise_db") == noise_db
            and meta.get("min_duration") == min_duration)


def ensure_silence_file(path, src, noise_db=-30, min_duration=0.08):
    """silence30.txt が無い、または隣の<path>.meta.jsonが今回の条件と一致しなければ
    ffmpeg silencedetect で生成し直す（SKILL.mdの既存コマンドと同じ規則）。

    既存ファイルを無条件に再利用すると、noise_db・min_durationを変えて再実行しても
    古い無音一覧を使い回してしまう。またmetaが無い既存ファイル（第3段階以前に
    作られたもの）も再生成する（Codexレビューで検出）。
    ffmpegが失敗した場合はファイルを書かずに例外を送出する（Codexレビューで検出：
    以前はreturncodeを見ておらず、失敗しても空/不完全なファイルを書いてしまい、
    「生成済み」として次回以降スキップされ、無音検出が壊れたまま気づけなかった）。
    """
    meta_path = _silence_meta_path(path)
    if os.path.exists(path) and _silence_meta_matches(
            _read_silence_meta(meta_path), src, noise_db, min_duration):
        return path
    r = subprocess.run(
        ["ffmpeg", "-i", src, "-af", "silencedetect=noise=%ddB:d=%.2f" % (noise_db, min_duration),
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("無音検出に失敗しました(ffmpeg exit=%d): %s" % (r.returncode, src))
    lines = [line for line in r.stderr.splitlines() if "silence" in line]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    try:
        src_mtime = os.path.getmtime(src)
    except OSError:
        src_mtime = None
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"src": os.path.abspath(src), "src_mtime": src_mtime,
                   "noise_db": noise_db, "min_duration": min_duration}, f)
    return path


def find_silence_near(silences, t, tolerance=SILENCE_TOLERANCE_SEC):
    """時刻tの前後tolerance秒以内にかかる無音区間があれば (start, end, duration) を返す。"""
    for s, e in silences:
        if s - tolerance <= t <= e + tolerance:
            return (s, e, e - s)
    return None


def find_silence_interior(silences, t, margin=0.015):
    """時刻tが無音区間の内側（両端からmargin秒以上離れている）に完全に収まっていれば
    (start, end, duration) を返す。align_check.pyのis_silent_at用：find_silence_near
    の「区間の外側でも近ければ無音扱いにする」許容とは逆に、区間の端に近すぎる
    （＝実際には発話にかかっている疑いがある）場合は無音と言い切らない。
    """
    for s, e in silences:
        if s + margin <= t <= e - margin:
            return (s, e, e - s)
    return None
