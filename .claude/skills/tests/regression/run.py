# -*- coding: utf-8 -*-
"""mitchaku-kirinuki の回帰テスト。render.py → qc_cuts.py を実データの縮小版
（fixture/clip.mp4、社内検証用の抜粋。出典・再配布については README.md を参照）に対して実行し、
expected.json と数値で比較する。ffmpeg の実行を伴うため重く、`unittest discover` には含めない。

使い方:
    python3 tests/regression/run.py                 # 検証（1つでも外れたら exit 1）
    python3 tests/regression/run.py --update-expected  # 現在の結果で expected.json を作り直す
    python3 tests/regression/run.py --ocrcli /path/to/ocrcli  # OCRが要る検査(qc_cuts)も行う
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO

REGRESSION_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(REGRESSION_DIR))
FIXTURE_DIR = os.path.join(REGRESSION_DIR, "fixture")
EXPECTED_PATH = os.path.join(REGRESSION_DIR, "expected.json")
SKILL_SCRIPTS_DIR = os.path.join(REPO_ROOT, "mitchaku-kirinuki", "scripts")

sys.path.insert(0, SKILL_SCRIPTS_DIR)
import render as R  # noqa: E402
import qc_cuts as Q  # noqa: E402

CLIP_FILE = "01_regression.mp4"
BAND_CROP = (Q.BAND_W, Q.BAND_H, Q.BAND_X, Q.BAND_Y)  # crop=1080:606:0:552（映像バンド）
DUP_WINDOW_FRAMES = 90
DUP_MARGIN_SECONDS = 2.0  # フラッシュ直後のブレンド区間を避けて2秒(≈60フレーム)入った位置から測る
DUP_MAX_SLACK = 2  # 元素材の自然な重複数に対して許容する上乗せ
HASH_THUMB_SIZE = (54, 96)  # 1080:1920 と同じアスペクト比の縮小グレイ画像
LOUDNESS_TARGET_I = R.LOUDNESS_TARGET_I
LOUDNESS_TOLERANCE = R.LOUDNESS_TOLERANCE
AV_DIFF_MAX_MS = 1000.0 / R.FPSF  # 1フレーム以内(≈33.4ms)
EMPTY_FRAME_MD5 = hashlib.md5(b"").hexdigest()  # フレーム抽出に失敗した(=空データの)MD5
SOURCE_MATCH_SIZE = (64, 36)  # 元素材との対応チェック用の縮小グレイ画像サイズ
# 再エンコード(libx264)による劣化を許容しつつ、別フレームとの取り違えは検出できる程度の値。
# gray値0-255での平均絶対差（2026-09-17、実データで別フレームとの差は数十以上出ることを確認）。
SOURCE_MATCH_MAX_DIFF = 12.0
SOURCE_MATCH_NEIGHBOR_MARGIN = 1.0  # 隣接フレームとの差がこの値未満なら「静止していて判定不能」
# clip.mp4(1920x1080の横向き元素材)は、出力(1080x1920の縦動画)に scale=1080:606 で
# 縮小されてから y=552 に重ねられる（render.pyのbuild_ffmpeg_cmdと同じ変換）。
# 出力側の帯(BAND_CROP)と同じ絵を元素材から取り出すには、元素材も同じ縮小をしてから
# overlay分のyオフセットを引いた位置でcropする必要がある。
SOURCE_PRE_SCALE = (R.VIDEO_BAND_WIDTH, R.VIDEO_BAND_HEIGHT)
SOURCE_BAND_CROP = (Q.BAND_W, Q.BAND_H, Q.BAND_X, Q.BAND_Y - R.VIDEO_BAND_Y)


# --- 計測ユーティリティ ---

def _ffprobe_stream_value(path, stream, entry):
    cmd = ["ffprobe", "-v", "error", "-select_streams", stream,
           "-show_entries", "stream=%s" % entry, "-of", "csv=p=0", path]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def probe_av_duration_diff_ms(path):
    """音声duration - 映像duration をミリ秒で返す（符号付き。絶対値で許容幅と比較する）。"""
    vdur = float(_ffprobe_stream_value(path, "v:0", "duration"))
    adur = float(_ffprobe_stream_value(path, "a:0", "duration"))
    return (adur - vdur) * 1000.0


def measure_lufs(path):
    """render.py本体と同じebur128実測に統一する（loudnormのprint_format=jsonは
    同じ素材でもebur128実測より最大0.3LU程度低く出る系統的バイアスがあるため使わない）。
    """
    return R.measure_ebur128(path)["integrated"]


def _extract_gray_raw(cmd, size):
    r = subprocess.run(cmd, capture_output=True, check=True)
    data = r.stdout
    fsize = size[0] * size[1]
    return [data[i * fsize:(i + 1) * fsize] for i in range(len(data) // fsize)]


def extract_gray_frames_by_time(path, start_seconds, n_frames, crop, size=(64, 36), pre_scale=None):
    """秒単位のシークでNフレーム抜き出す（生の素材clip.mp4向け。フレーム番号を管理しない）。
    pre_scale=(w,h): cropの前に本編と同じ縮小をかける（extract_gray_frame_by_frame_numberと
    同じ理由。SOURCE_BAND_CROPの説明を参照）。
    """
    cw, ch, cx, cy = crop
    pre = "scale=%d:%d:flags=lanczos," % pre_scale if pre_scale else ""
    vf = "%scrop=%d:%d:%d:%d,scale=%d:%d,format=gray" % (pre, cw, ch, cx, cy, size[0], size[1])
    cmd = ["ffmpeg", "-v", "error", "-ss", "%.3f" % start_seconds, "-i", path,
           "-frames:v", str(n_frames), "-vf", vf,
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    return _extract_gray_raw(cmd, size)


def extract_gray_frames_by_frame_range(path, start_frame, n_frames, crop, size=(64, 36)):
    """出力フレーム番号(select=between)でNフレーム抜き出す（render.pyの出力向け。qc_cutsと同じ座標系）。"""
    cw, ch, cx, cy = crop
    vf = ("select=between(n\\,%d\\,%d),crop=%d:%d:%d:%d,scale=%d:%d,format=gray"
          % (start_frame, start_frame + n_frames - 1, cw, ch, cx, cy, size[0], size[1]))
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-vsync", "0",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    return _extract_gray_raw(cmd, size)


def count_consecutive_duplicates(frames):
    return sum(1 for i in range(1, len(frames)) if frames[i] == frames[i - 1])


def frame_hash(path, frame_number, size=HASH_THUMB_SIZE):
    """指定フレーム番号を縮小グレイ画像にしてMD5を返す（I-11のようなフレーム重複/欠落を検出する）。
    フレームが存在しない（select がヒットしない）場合、ffmpegの出力は空になり
    EMPTY_FRAME_MD5になる。呼び出し側でフレーム欠落として扱う。
    """
    vf = "select=eq(n\\,%d),scale=%d:%d,format=gray" % (frame_number, size[0], size[1])
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-vsync", "0",
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    r = subprocess.run(cmd, capture_output=True, check=True)
    return hashlib.md5(r.stdout).hexdigest()


def extract_gray_frame_by_frame_number(path, frame_number, crop, size=SOURCE_MATCH_SIZE,
                                        pre_scale=None):
    """フレーム番号(select=eq(n,K))を直接指定して1枚を縮小グレイ画像(bytes)で抜き出す。
    出力・元素材(clip.mp4)どちらにも使える。抽出できなければ空bytesを返す。
    pre_scale=(w,h): cropの前に本編と同じ縮小をかける（元素材から出力と同じ絵の帯を
    取り出すため。SOURCE_BAND_CROPの説明を参照）。
    """
    cw, ch, cx, cy = crop
    pre = "scale=%d:%d:flags=lanczos," % pre_scale if pre_scale else ""
    vf = ("select=eq(n\\,%d),%scrop=%d:%d:%d:%d,scale=%d:%d,format=gray"
          % (frame_number, pre, cw, ch, cx, cy, size[0], size[1]))
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-vsync", "0",
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    r = subprocess.run(cmd, capture_output=True, check=True)
    return r.stdout


def mean_abs_diff(a, b):
    """2枚の同サイズグレイ画像(bytes)の平均絶対差を返す（純粋関数）。
    サイズ不一致・空データは比較できないのでNoneを返す。
    """
    if not a or not b or len(a) != len(b):
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def judge_source_correspondence(out_diff, neighbor_diffs,
                                 max_diff=SOURCE_MATCH_MAX_DIFF,
                                 neighbor_margin=SOURCE_MATCH_NEIGHBOR_MARGIN):
    """出力フレームが元素材の対応フレームと同じ絵かどうかを判定する（純粋関数）。
    neighbor_diffs: 両隣（src-1とsrc+1）との差のリスト。頭尻どちらの1フレームずれも
    検出できるよう、方向を決め打ちせず両方と比較する（要素はNoneでもよい＝比較不可）。
    戻り値: (合格か(True/False/None), 理由文字列)。Noneは「静止に近く判定不能」の警告。
    """
    if out_diff is None:
        return False, "NG（フレーム抽出に失敗し比較できません）"
    if out_diff > max_diff:
        return False, "NG（元素材の対応フレームと絵が一致しません: 差%.2f）" % out_diff

    valid_neighbors = [d for d in neighbor_diffs if d is not None]
    if not valid_neighbors:
        return None, "警告（隣接フレームと比較できず判定不能: 対応差%.2f）" % out_diff

    closest_neighbor = min(valid_neighbors)
    if closest_neighbor + neighbor_margin < out_diff:
        # 隣接フレームのほうが明確に近い＝対応フレームがそもそも間違っている(頭尻ずれ)
        return False, ("NG（隣接フレームのほうが元素材に近く、頭尻がずれている可能性があります: "
                       "対応差%.2f/隣接最小差%.2f）" % (out_diff, closest_neighbor))
    if abs(out_diff - closest_neighbor) < neighbor_margin:
        return None, ("警告（静止に近く頭尻ずれを判定できません: 対応差%.2f/隣接最小差%.2f）"
                      % (out_diff, closest_neighbor))
    return True, "OK（対応差%.2f/隣接最小差%.2f）" % (out_diff, closest_neighbor)


def count_qc_ng(clips_plan_path, out_dir, telops_path, ocrcli):
    """qc_cuts.main()を呼び、(NG行数, 総行数, returncode)を返す。"""
    buf = StringIO()
    argv = [clips_plan_path, "--out", out_dir, "--telops", telops_path, "--ocrcli", ocrcli]
    with redirect_stdout(buf):
        rc = Q.main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    ng = sum(1 for line in lines if "NG" in line)
    return ng, len(lines), rc


def check_qc_process_ok(ng, rc):
    """qc_cutsの実行状態を検査する（純粋関数）。qc_cuts.main()はTimelineMissingError等の
    内部エラーでもNG行を1行も出さずにrc=1を返すことがあり、NG数だけを見ると
    「NG 0件で合格」と誤読みしてしまう。NG行が無いのにrc!=0なら内部エラーとして扱う。
    戻り値: (問題無いか, 理由文字列 or None)
    """
    if rc != 0 and ng == 0:
        return False, "qc_cutsの実行がNG行の記録無しに失敗しました（返り値=%d）" % rc
    return True, None


# --- 測定の組み立て ---

def require_frame_count(frames, expected_n, context):
    """抽出できたフレーム数が要求数に届いているかを確認する（純粋関数）。
    途中で映像が終わった・シーク位置が範囲外だった等で無言のまま少ないフレーム数で
    計測を続けると、重複数の基準値が実態と無関係な値になる。
    """
    if len(frames) < expected_n:
        raise RuntimeError(
            "%sから%d枚のフレームを取得できませんでした（%d枚のみ抽出）"
            % (context, expected_n, len(frames)))


def measure_source_baseline(cut_local_start_seconds):
    """元素材(clip.mp4)における自然な重複フレーム数を、出力側と同じ座標系
    (SOURCE_PRE_SCALE/SOURCE_BAND_CROP)で測る。以前は出力側のBAND_CROPをそのまま
    元素材(1920x1080の横向き)に当てていたが、出力(1080x1920の縦動画)とは
    座標系が異なり、意図した映像帯とズレた領域を見ていた（2026-09-17に発覚）。
    """
    clip_path = os.path.join(FIXTURE_DIR, "clip.mp4")
    start = cut_local_start_seconds + DUP_MARGIN_SECONDS
    frames = extract_gray_frames_by_time(clip_path, start, DUP_WINDOW_FRAMES,
                                          SOURCE_BAND_CROP, pre_scale=SOURCE_PRE_SCALE)
    require_frame_count(frames, DUP_WINDOW_FRAMES, "元素材(clip.mp4)")
    return count_consecutive_duplicates(frames)


def load_fixture_clips_plan():
    spec = importlib.util.spec_from_file_location(
        "regression_clips_plan", os.path.join(FIXTURE_DIR, "clips_plan.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _boundary_point(out_path, clip_path, label, out_frame, src_frame):
    """1点（頭 or 尻）の元素材との対応チェックを行う。頭尻どちらも両隣(src-1, src+1)と
    比較する（片側だけだと逆方向の1フレームずれを検出できない）。
    """
    out_bytes = extract_gray_frame_by_frame_number(out_path, out_frame, BAND_CROP)
    expected_bytes = extract_gray_frame_by_frame_number(
        clip_path, src_frame, SOURCE_BAND_CROP, pre_scale=SOURCE_PRE_SCALE)
    prev_bytes = extract_gray_frame_by_frame_number(
        clip_path, src_frame - 1, SOURCE_BAND_CROP, pre_scale=SOURCE_PRE_SCALE)
    next_bytes = extract_gray_frame_by_frame_number(
        clip_path, src_frame + 1, SOURCE_BAND_CROP, pre_scale=SOURCE_PRE_SCALE)
    out_diff = mean_abs_diff(out_bytes, expected_bytes)
    neighbor_diffs = [mean_abs_diff(out_bytes, prev_bytes), mean_abs_diff(out_bytes, next_bytes)]
    ok, reason = judge_source_correspondence(out_diff, neighbor_diffs)
    return {"label": label, "out_frame": out_frame, "src_frame": src_frame,
            "ok": ok, "reason": reason}


def check_source_correspondence(out_path, timeline):
    """各カットの頭・尻フレームが元素材(clip.mp4)の対応フレームと同じ絵かどうかを調べる。
    自己ベースラインのハッシュ比較だけでは「最初から壊れた値」を記録してしまう
    （フレーム欠落で空データのMD5を期待値にしてしまう、等）ため、元素材との突き合わせを
    別に行う。フラッシュにかかる境界（カット0の尻・カット1の頭）は白フェードのブレンドで
    絵が変わるため、フラッシュ外の最寄りフレームで比較する。
    戻り値: [{"cut", "label", "out_frame", "src_frame", "ok"(True/False/None), "reason"}, ...]
    """
    clip_path = os.path.join(FIXTURE_DIR, "clip.mp4")
    cuts = timeline["cuts"]
    flash_after = timeline.get("flash_after")
    flash_frames = timeline.get("flash_frames", 0)
    n = len(cuts)
    has_flash = flash_after is not None and 0 <= flash_after < n - 1

    results = []
    for cut in cuts:
        i = cut["index"]
        nf = cut["n_frames"]
        in_flash_blend_tail = has_flash and i == flash_after
        in_flash_blend_head = has_flash and i == flash_after + 1

        head_out, head_src = cut["out_start_frame"], cut["src_start_frame"]
        if in_flash_blend_head:
            head_out += flash_frames
            head_src += flash_frames
        point = _boundary_point(out_path, clip_path, "head", head_out, head_src)
        point["cut"] = i
        results.append(point)

        tail_out = cut["out_start_frame"] + nf - 1
        tail_src = cut["src_end_frame"] - 1
        if in_flash_blend_tail:
            tail_out -= flash_frames
            tail_src -= flash_frames
        point = _boundary_point(out_path, clip_path, "tail", tail_out, tail_src)
        point["cut"] = i
        results.append(point)
    return results


# --- 比較・レポート ---

def _find_duplicate_indices(cuts):
    """cutsのindexに重複があれば、その集合を返す（純粋関数）。"""
    seen, dupes = set(), set()
    for c in cuts:
        i = c["index"]
        if i in seen:
            dupes.add(i)
        seen.add(i)
    return dupes


def compare(actual, expected):
    """(合格か, [差分の説明, ...]) を返す。"""
    problems = []

    actual_dupes = _find_duplicate_indices(actual["cuts"])
    if actual_dupes:
        problems.append("actualのcutsにインデックス重複があります: %s" % sorted(actual_dupes))
    expected_dupes = _find_duplicate_indices(expected["cuts"])
    if expected_dupes:
        problems.append("expectedのcutsにインデックス重複があります: %s" % sorted(expected_dupes))

    actual_indices = {c["index"] for c in actual["cuts"]}
    expected_indices = {c["index"] for c in expected["cuts"]}
    missing_in_actual = expected_indices - actual_indices
    if missing_in_actual:
        problems.append("期待値にあるカット%sが実際の出力に存在しません" % sorted(missing_in_actual))

    if abs(actual["av_duration_diff_ms"]) > expected["av_duration_diff_ms_max"]:
        problems.append("A/V長さ差 %.2fms が許容%.2fmsを超えています"
                         % (actual["av_duration_diff_ms"], expected["av_duration_diff_ms_max"]))

    lufs_diff = abs(actual["lufs"] - expected["lufs_target"])
    if lufs_diff > expected["lufs_tolerance"]:
        problems.append("LUFS %.2f が目標%.1f±%.1fから外れています"
                         % (actual["lufs"], expected["lufs_target"], expected["lufs_tolerance"]))

    if actual.get("qc_ng_count") is not None and expected.get("qc_ng_count") is not None:
        if actual["qc_ng_count"] != expected["qc_ng_count"]:
            problems.append("qc_cutsのNG数 %d が期待値 %d と異なります"
                             % (actual["qc_ng_count"], expected["qc_ng_count"]))

    if actual["dup_frame_count_output"] > expected["dup_frame_max"]:
        problems.append("重複フレーム数 %d が許容上限 %d(基準%d+余裕%d)を超えています"
                         % (actual["dup_frame_count_output"], expected["dup_frame_max"],
                            expected["dup_frame_baseline"], expected["dup_frame_margin"]))

    if actual["dup_frame_baseline"] != expected["dup_frame_baseline"]:
        problems.append("元素材の基準重複数 %d が記録値 %d と異なります（fixtureが変わった可能性）"
                         % (actual["dup_frame_baseline"], expected["dup_frame_baseline"]))

    exp_cuts = {c["index"]: c for c in expected["cuts"]}
    for cut in actual["cuts"]:
        exp_cut = exp_cuts.get(cut["index"])
        if exp_cut is None:
            problems.append("カット%d: 期待値に存在しません" % cut["index"])
            continue
        if cut["n_frames"] != exp_cut["n_frames"]:
            problems.append("カット%d: n_frames %d が期待値 %d と異なります"
                             % (cut["index"], cut["n_frames"], exp_cut["n_frames"]))
        if cut["head_hash"] != exp_cut["head_hash"]:
            problems.append("カット%d: 頭フレームのハッシュが一致しません" % cut["index"])
        if cut["tail_hash"] != exp_cut["tail_hash"]:
            problems.append("カット%d: 尻フレームのハッシュが一致しません" % cut["index"])

    return (len(problems) == 0), problems


def check_invariants(actual_frame_count, timeline, cuts_actual, correspondence):
    """スナップショット比較(expected.json)とは独立した不変条件を検査する（純粋関数）。
    自己ベースラインのハッシュ比較だけでは「最初から壊れた値」をそのまま期待値にして
    しまう（フレーム欠落で空データのMD5を記録する、等）ため、ここで機械的に弾く。
    """
    problems = []
    if actual_frame_count != timeline["total_frames"]:
        problems.append("出力フレーム数 %d が timeline.total_frames %d と異なります"
                         % (actual_frame_count, timeline["total_frames"]))

    for cut in cuts_actual:
        if cut["head_hash"] == EMPTY_FRAME_MD5:
            problems.append("カット%d: 頭フレームの抽出に失敗しています（空データのMD5）"
                             % cut["index"])
        if cut["tail_hash"] == EMPTY_FRAME_MD5:
            problems.append("カット%d: 尻フレームの抽出に失敗しています（空データのMD5）"
                             % cut["index"])

    for point in correspondence:
        if point["ok"] is False:
            problems.append("カット%d %s: %s" % (point["cut"], point["label"], point["reason"]))

    return problems


def print_report(actual, expected_exists):
    print("A/V長さ差: %.2fms（許容%.2fms以内）" % (actual["av_duration_diff_ms"], AV_DIFF_MAX_MS))
    print("LUFS: %.2f（目標%.1f±%.1f）" % (actual["lufs"], LOUDNESS_TARGET_I, LOUDNESS_TOLERANCE))
    if actual.get("qc_ng_count") is not None:
        print("qc_cuts: NG %d / 全%d件" % (actual["qc_ng_count"], actual["qc_total_count"]))
    else:
        print("qc_cuts: ocrcli が無いためスキップしました")
    print("重複フレーム数: 出力%d件 / 元素材基準%d件" % (
        actual["dup_frame_count_output"], actual["dup_frame_baseline"]))
    for cut in actual["cuts"]:
        print("カット%d: n_frames=%d head_hash=%s tail_hash=%s"
              % (cut["index"], cut["n_frames"], cut["head_hash"][:8], cut["tail_hash"][:8]))


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-expected", action="store_true",
                     help="現在の結果を expected.json として保存し直す")
    ap.add_argument("--ocrcli", default=None,
                     help="OCR実行ファイル（省略時はqc_cutsの検査をスキップ）")
    ap.add_argument("--keep-workdir", action="store_true",
                     help="デバッグ用に一時作業フォルダを消さずに残す")
    return ap.parse_args(argv)


def resolve_ocrcli_path(ocrcli_arg):
    """--ocrcli引数を絶対パス化する（純粋にpathだけを扱う。存在チェックのみ副作用）。
    render.main()はworkdirへchdirする（呼び出し後には戻すが、念のため相対パスの
    解釈に依存しないよう、実行前に絶対パス化しておく）。未指定(None)ならOCR検査を
    スキップする合図としてNoneを返す。明示指定されたのに存在しなければエラーにする
    （以前は黙ってスキップしていたため、typoに気づけなかった）。
    戻り値: (絶対パス or None, エラー文字列 or None)
    """
    if not ocrcli_arg:
        return None, None
    abs_path = os.path.abspath(ocrcli_arg)
    if not os.path.exists(abs_path):
        return None, "--ocrcli に指定されたパスが見つかりません: %s" % abs_path
    return abs_path, None


def main(argv):
    args = parse_args(argv)
    ocrcli_path, ocrcli_error = resolve_ocrcli_path(args.ocrcli)
    if ocrcli_error:
        print("ERROR: %s" % ocrcli_error, file=sys.stderr)
        return 1
    workdir = tempfile.mkdtemp(prefix="mitchaku_regression_")
    try:
        shutil.copyfile(os.path.join(FIXTURE_DIR, "clip.mp4"), os.path.join(workdir, "src.mp4"))

        out_dir = os.path.join(workdir, "out")
        clips_plan_path = os.path.join(FIXTURE_DIR, "clips_plan.py")
        telops_path = os.path.join(FIXTURE_DIR, "telops.txt")

        rc = R.main([workdir, clips_plan_path, "--out", "out"])
        if rc != 0:
            print("ERROR: render.py が失敗しました（exit=%d）" % rc, file=sys.stderr)
            return 1

        out_path = os.path.join(out_dir, CLIP_FILE)
        timeline = Q.load_timeline(out_dir, CLIP_FILE)
        cuts = timeline["cuts"]

        actual = {
            "av_duration_diff_ms": probe_av_duration_diff_ms(out_path),
            "lufs": measure_lufs(out_path),
            "cuts": [],
        }
        for cut in cuts:
            head_frame = cut["out_start_frame"]
            tail_frame = cut["out_start_frame"] + cut["n_frames"] - 1
            actual["cuts"].append({
                "index": cut["index"],
                "n_frames": cut["n_frames"],
                "head_hash": frame_hash(out_path, head_frame),
                "tail_hash": frame_hash(out_path, tail_frame),
            })

        main_cut = cuts[1]
        margin_frames = round(DUP_MARGIN_SECONDS * R.FPSF)
        out_start = main_cut["out_start_frame"] + margin_frames
        out_frames = extract_gray_frames_by_frame_range(out_path, out_start, DUP_WINDOW_FRAMES, BAND_CROP)
        actual["dup_frame_count_output"] = count_consecutive_duplicates(out_frames)

        M = load_fixture_clips_plan()
        cut1_local_start = M.CLIPS[0]["cuts"][1][0]  # cut[1](主本編カット)のfixtureローカル開始秒
        try:
            actual["dup_frame_baseline"] = measure_source_baseline(cut1_local_start)
        except RuntimeError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 1

        # 自己ベースラインのハッシュ比較だけでは「最初から壊れた値」を記録してしまう
        # （フレーム欠落による空データのMD5等）ため、元素材との対応・フレーム総数を
        # スナップショット比較とは別に必ず検査する（--update-expected でもスキップしない）。
        actual_frame_count = R.probe_video_frame_count(out_path)
        correspondence = check_source_correspondence(out_path, timeline)
        for point in correspondence:
            if point["ok"] is None:
                print("WARNING: カット%d %s: %s" % (point["cut"], point["label"], point["reason"]))
        invariant_problems = check_invariants(
            actual_frame_count, timeline, actual["cuts"], correspondence)

        if ocrcli_path:
            ng, total, qc_rc = count_qc_ng(clips_plan_path, out_dir, telops_path, ocrcli_path)
            actual["qc_ng_count"] = ng
            actual["qc_total_count"] = total
            qc_ok, qc_problem = check_qc_process_ok(ng, qc_rc)
            if not qc_ok:
                invariant_problems.append(qc_problem)
        else:
            print("NOTICE: --ocrcli が指定されていないため、qc_cutsのOCR検査はスキップします")
            actual["qc_ng_count"] = None
            actual["qc_total_count"] = None

        print_report(actual, os.path.exists(EXPECTED_PATH))
        print("出力フレーム数: %d（timeline.total_frames=%d）"
              % (actual_frame_count, timeline["total_frames"]))

        if invariant_problems:
            # 不変条件が壊れている場合、--update-expected でも壊れた値を期待値として
            # 記録させない（そのまま保存すると次回以降ずっと「壊れた基準」と比較し続ける）。
            print("FAIL:")
            for p in invariant_problems:
                print("  - %s" % p)
            return 1

        if args.update_expected:
            expected = {
                "clip_file": CLIP_FILE,
                "av_duration_diff_ms_max": AV_DIFF_MAX_MS,
                "lufs_target": LOUDNESS_TARGET_I,
                "lufs_tolerance": LOUDNESS_TOLERANCE,
                "qc_ng_count": actual["qc_ng_count"] if actual["qc_ng_count"] is not None else 0,
                "dup_frame_window": DUP_WINDOW_FRAMES,
                "dup_frame_margin": DUP_MAX_SLACK,
                "dup_frame_baseline": actual["dup_frame_baseline"],
                "dup_frame_max": actual["dup_frame_baseline"] + DUP_MAX_SLACK,
                "cuts": [{"index": c["index"], "n_frames": c["n_frames"],
                          "head_hash": c["head_hash"], "tail_hash": c["tail_hash"]}
                         for c in actual["cuts"]],
            }
            with open(EXPECTED_PATH, "w", encoding="utf-8") as f:
                json.dump(expected, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print("expected.json を更新しました: %s" % EXPECTED_PATH)
            return 0

        if not os.path.exists(EXPECTED_PATH):
            print("ERROR: %s がありません。--update-expected で作成してください" % EXPECTED_PATH,
                  file=sys.stderr)
            return 1
        with open(EXPECTED_PATH, encoding="utf-8") as f:
            expected = json.load(f)

        if actual["qc_ng_count"] is None:
            expected = dict(expected)
            expected["qc_ng_count"] = None  # OCRスキップ時はNG数の比較自体を行わない

        ok, problems = compare(actual, expected)
        if not ok:
            print("FAIL:")
            for p in problems:
                print("  - %s" % p)
            return 1
        print("PASS")
        return 0
    finally:
        if args.keep_workdir:
            print("workdir を残しました: %s" % workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
