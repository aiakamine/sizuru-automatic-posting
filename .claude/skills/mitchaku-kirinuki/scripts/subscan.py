# -*- coding: utf-8 -*-
"""テロップ切替検出→Vision OCR→時刻表(telops.txt)を作る（第3段階で関数化・改良）。
使い方: python3 subscan.py [src.mp4] [--band-y 860] [--band-h 210] [--ocrcli ./ocrcli]
        [--out telops.txt]
前提: 同ディレクトリ（または--ocrcli指定先）にocrcli（swiftc -O -o ocrcli ocr.swift でビルド）

第3段階での変更点:
  ① fpsをffprobe(r_frame_rate)から取得する（固定値30000/1001をやめる）
  ② 各区間の中央フレームに加え区間先頭フレーム自体もOCRし、中央と食い違う場合は
     二分探索で真の切替フレームを求める（テロップのフェードインで切替点を取り逃す対策）
  ③ テロップ帯の位置を数フレームの文字エッジ分布から推定し、既定値と20px以上
     ずれたら警告する／OCR結果の空行率が50%を超えても警告する

fps取得・帯位置推定・二分探索まわりの汎用ロジックは kirinuki_core（video.py/telop_scan.py）
に共通化されている（対談スキルと共有するため。第3段階の共通パッケージ化）。ここには
帯の切替検出そのもの（compute_frame_diffs等）やffmpegでの画像抽出、main/argparseを残す。
"""
import argparse
import os
import subprocess
import sys

import _core  # noqa: E402
_core.ensure_kirinuki_core_on_path()

from kirinuki_core.video import (  # noqa: E402
    parse_frame_rate,
    get_fps,
    get_frame_height,
    frame_seek_time,
)
from kirinuki_core.telop_scan import (  # noqa: E402
    bisect_first_true,
    texts_similar,
    refine_segment_start,
    edge_row_profile,
    best_band_start,
    band_offset_warning,
    empty_rate,
    empty_rate_warning,
    clamp_search_window,
    run_ocr,
    missing_ocr_results,
)

# テロップ帯（1920x1080素材の実測・既定値。実際の帯位置は sample_band_rows/best_band_start
# で推定し、大きくずれていたら警告する。自動補正はしない＝安全側に倒す）
DEFAULT_BAND_Y, DEFAULT_BAND_H = 860, 210
SCAN_W, SCAN_H = 192, 21          # 切替検出用の縮小グレイ画像サイズ

CHANGE_THRESHOLD = 6.0            # フレーム間差分がこれを超えたら切替候補
CHANGE_DECAY_RATIO = 0.5          # 切替の終わり判定（threshold*この比率未満に落ちるまで）
MIN_SEGMENT_FRAMES = 4            # これ未満の区間はノイズとして無視する

BAND_SEARCH_MARGIN_PX = 150   # 帯位置推定：既定位置の上下にこれだけ余裕を持って探索する


# --- 帯の差分による切替検出（既存ロジックを純粋関数に分離） ---

def compute_frame_diffs(data, frame_size):
    """band.rawの生バイト列から、隣接フレーム間の平均輝度差のリストを返す。"""
    n = len(data) // frame_size
    diffs = []
    prev = None
    for i in range(n):
        frame = data[i * frame_size:(i + 1) * frame_size]
        if prev is not None:
            diffs.append(sum(abs(a - b) for a, b in zip(frame, prev)) / frame_size)
        prev = frame
    return diffs


def find_changepoints(diffs, threshold=CHANGE_THRESHOLD, decay_ratio=CHANGE_DECAY_RATIO):
    """差分のピークを切替フレーム番号のリストとして返す
    （decay_ratio*threshold未満に落ちるまでを1つの切替とみなし、飛び石で二重検出しない）。
    """
    cps = []
    i = 0
    while i < len(diffs):
        if diffs[i] > threshold:
            j = i
            while j + 1 < len(diffs) and diffs[j + 1] > threshold * decay_ratio:
                j += 1
            cps.append(i + 1)
            i = j + 1
        else:
            i += 1
    return cps


def build_segments(changepoints, n_frames, min_len=MIN_SEGMENT_FRAMES):
    """[0]+切替点+[n_frames] から、min_len未満の短い区間を除いた (a, b) の
    フレーム範囲リストを返す。
    """
    bounds = [0] + list(changepoints) + [n_frames]
    return [(bounds[k], bounds[k + 1]) for k in range(len(bounds) - 1)
            if bounds[k + 1] - bounds[k] >= min_len]


# --- 帯の縮小グレイraw書き出し・フレームOCR用PNG抽出（impure） ---

def extract_band_raw(src, band_y, band_h, path):
    if not os.path.exists(path):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", src,
             "-vf", "crop=1920:%d:0:%d,scale=%d:%d,format=gray" % (band_h, band_y, SCAN_W, SCAN_H),
             "-f", "rawvideo", "-pix_fmt", "gray", path, "-y"],
            check=True,
        )
    return path


def extract_band_png(src, frame, fps, band_y, band_h, out_png):
    """フレーム番号frameに対応する帯領域をPNGに書き出す。ffmpegが失敗したら
    黙って処理を続けず例外を送出する（失敗を「空のテロップ」と混同しないため。
    Codexレビューで検出：以前はcheck=Trueが無く、失敗してもPNGが無いまま
    素通りし、後段のrun_ocrで空文字列扱いになっていた）。
    """
    t = frame_seek_time(frame, fps)
    if not os.path.exists(out_png):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "%.6f" % t, "-i", src,
             "-frames:v", "1", "-vf", "crop=1920:%d:0:%d" % (band_h + 20, band_y - 10),
             out_png, "-y"],
            check=True,
        )
    return out_png


def sample_band_rows(src, times, band_y, band_h, tmp_dir, frame_height, width=64,
                      margin=BAND_SEARCH_MARGIN_PX):
    """band_y推定用に、設定位置(band_y)の上下marginピクセルの探索窓を、指定した
    数フレーム分グレイ画素の2次元配列に足し合わせて返す（行=縦方向1px・元解像度のまま
    ＝縮小しない。列=横方向のみwidthに縮小）。
    戻り値: (search_y0, rows)。search_y0は探索窓の絶対y座標（元1080基準）で、
    best_band_startの結果に加算して元座標に戻すために呼び出し側が使う。
    縦方向を縮小しないのは、縮小すると呼び出し側でスケール換算が必要になり
    座標を取り違えやすいため（実際にこの取り違えで誤検出したことがある）。
    frame_heightではみ出さないようにclamp_search_windowで収める（はみ出すと
    ffmpegのcropが暗黙にy0をずらし、この関数が返すsearch_y0と実際の切り出し位置が
    食い違ってしまうため）。
    """
    search_y0, search_h = clamp_search_window(
        max(0, band_y - margin), band_h + margin * 2, frame_height)
    all_rows = None
    for i, t in enumerate(times):
        raw_path = os.path.join(tmp_dir, "bandcheck_%d.raw" % i)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "%.3f" % t, "-i", src, "-frames:v", "1",
             "-vf", "crop=1920:%d:0:%d,scale=%d:%d,format=gray" % (search_h, search_y0, width, search_h),
             "-f", "rawvideo", "-pix_fmt", "gray", raw_path, "-y"],
            check=True,
        )
        data = open(raw_path, "rb").read()
        rows = [list(data[r * width:(r + 1) * width]) for r in range(search_h)]
        if all_rows is None:
            all_rows = rows
        else:
            all_rows = [[a + b for a, b in zip(ra, rb)] for ra, rb in zip(all_rows, rows)]
    return search_y0, all_rows


# --- メイン処理 ---

def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default="src.mp4")
    ap.add_argument("--band-y", type=int, default=DEFAULT_BAND_Y)
    ap.add_argument("--band-h", type=int, default=DEFAULT_BAND_H)
    ap.add_argument("--ocrcli", default="./ocrcli")
    ap.add_argument("--out", default="telops.txt")
    ap.add_argument("--band-raw", default=None,
                     help="既定: band_<band-y>_<band-h>.raw（帯位置を変えると自動的に別キャッシュになる）")
    ap.add_argument("--tel-dir", default=None,
                     help="既定: tel_<band-y>_<band-h>（帯位置を変えると自動的に別キャッシュになる）")
    ap.add_argument("--skip-band-check", action="store_true",
                     help="帯位置推定（数フレームの追加OCR）を省略する")
    return ap.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    fps = get_fps(args.src)

    # band-y/band-hを未指定の既定キャッシュ名に含める：値を変えて再実行したとき、
    # 古い帯位置で書き出したband.raw/PNGを誤って使い回さないため
    # （Codexレビューで検出。--band-raw/--tel-dirを明示すれば従来通りその名前を使う）
    band_raw = args.band_raw or "band_%d_%d.raw" % (args.band_y, args.band_h)
    tel_dir = args.tel_dir or "tel_%d_%d" % (args.band_y, args.band_h)

    extract_band_raw(args.src, args.band_y, args.band_h, band_raw)
    frame_size = SCAN_W * SCAN_H
    data = open(band_raw, "rb").read()
    n_frames = len(data) // frame_size
    diffs = compute_frame_diffs(data, frame_size)
    changepoints = find_changepoints(diffs)
    print("frames=%d changepoints=%d" % (n_frames, len(changepoints)))
    segments = build_segments(changepoints, n_frames)

    os.makedirs(tel_dir, exist_ok=True)
    rows = []
    for a, b in segments:
        mid_frame = (a + b) // 2
        # ファイル名をセグメントの通し番号ではなく実フレーム番号にするのは、
        # 変化点検出のパラメータを変えてセグメント区切りが変化したときに、
        # 同じ番号が別のフレームを指してしまう事故を避けるため（Codexレビューで検出）
        png = os.path.join(tel_dir, "f%d.png" % mid_frame)
        extract_band_png(args.src, mid_frame, fps, args.band_y, args.band_h, png)
        rows.append((a, b, png))

    center_pngs = [p for _, _, p in rows]
    ocr, ocr_failed = run_ocr(args.ocrcli, center_pngs)
    if ocr_failed:
        print("エラー: OCRプロセスが失敗しました。telops.txtは書き出しません", file=sys.stderr)
        return 1
    missing = missing_ocr_results(center_pngs, ocr)
    if missing:
        print("エラー: OCR結果が欠落しています(%d件)。telops.txtは書き出しません: %s"
              % (len(missing), missing[:5]), file=sys.stderr)
        return 1

    # ② フェードイン対策：区間先頭フレーム自体をOCRし、中央と食い違えば二分探索で
    # 真の開始フレームを求める（先頭が既に中央と一致していれば追加OCRは行わない。
    # refine_segment_start自身がseg_start＝aを直接検査するので、ここでa+2等を
    # 先読みして判定を代行しない。以前はa+2の一致だけで「フェードインなし」と
    # 誤判定し、a自体が未確定のまま素通りすることがあった＝Codexレビューで検出）。
    refined = []
    for a, b, png in rows:
        center_text = ocr.get(png, "")
        mid_frame = (a + b) // 2

        def ocr_text_at(frame, _band_y=args.band_y, _band_h=args.band_h):
            probe_png = os.path.join(tel_dir, "probe_%d.png" % frame)
            extract_band_png(args.src, frame, fps, _band_y, _band_h, probe_png)
            probe_ocr, probe_failed = run_ocr(args.ocrcli, [probe_png])
            if probe_failed or missing_ocr_results([probe_png], probe_ocr):
                raise RuntimeError("フェードイン検査中にOCRが失敗しました: %s" % probe_png)
            return probe_ocr.get(probe_png, "")

        new_a = refine_segment_start(ocr_text_at, a, mid_frame, center_text) if mid_frame > a else a
        refined.append((new_a, b, center_text))

    texts = [t for _, _, t in refined]
    rate = empty_rate(texts)
    warn = empty_rate_warning(rate)
    if warn:
        print(warn, file=sys.stderr)

    if not args.skip_band_check:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            sample_times = [(segments[k][0] + segments[k][1]) / 2 / fps
                             for k in range(0, len(segments), max(1, len(segments) // 5))][:5]
            if sample_times:
                frame_height = get_frame_height(args.src)
                search_y0, sample_rows = sample_band_rows(
                    args.src, sample_times, args.band_y, args.band_h, tmp_dir, frame_height)
                profile = edge_row_profile(sample_rows)
                estimated = search_y0 + best_band_start(profile, args.band_h)
                band_warn = band_offset_warning(estimated, args.band_y)
                if band_warn:
                    print(band_warn, file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as f:
        for a, b, text in refined:
            f.write("%8.3f %8.3f f%6d-%6d %s\n" % (a / fps, b / fps, a, b, text))
    print("telops.txt written:", len(refined), "rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
