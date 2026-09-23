# -*- coding: utf-8 -*-
"""テロップ区間ごとに中央フレームを抜き、Vision OCRで本文を読んで一覧表にする（第3段階で関数化・改良）。
使い方: python3 subtext.py <src.mp4> <workdir> <ocrcli> [--from 0] [--to 1e9] [--jobs 8]
出力: <workdir>/segs.json（区間）, <workdir>/telops.txt（番号 開始-終了 本文）

第3段階での変更点:
  ① 区間先頭フレームもOCRし、中央と食い違う場合は二分探索で真の切替フレームを求める
     （テロップのフェードインで切替点を取り逃す対策。補正した開始時刻をsegs.json/telops.txtに反映）
  ② OCR結果の空行率が50%を超えたら警告、テロップ帯の推定位置が--y/--hと20px以上ずれたら警告
  ③ フレーム抽出の並列実行をxargs（シェル経由）からconcurrent.futuresに変更した。
     `cat cmds | xargs -P N -I{} sh -c '{}'` は、作業フォルダのパスが長いとxargsの-Iの
     置換後255バイト制限に達し「command line cannot be assembled, too long」で落ちる
     不具合があった（スクラッチパッド配下で実測）。シェル文字列を経由せず、
     concurrent.futures.ThreadPoolExecutorでsubprocess.runを直接呼ぶことでパス長に
     依らず動くようにする

フェードイン対策・空行率/帯位置の警告まわりの汎用ロジックは kirinuki_core.telop_scan に
共通化されている（密着スキルと共有するため）。ここには区間の組み立て・ffmpegでの画像抽出・
OCR呼び出しの並列化・main/argparseを残す。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _core  # noqa: E402
_core.ensure_kirinuki_core_on_path()

from kirinuki_core.video import get_frame_height  # noqa: E402
from kirinuki_core.telop_scan import (  # noqa: E402
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

BAND_SEARCH_MARGIN_PX = 150   # 帯位置推定：既定位置の上下にこれだけ余裕を持って探索する
BAND_PROBE_ROW_WIDTH = 64     # 帯位置推定で使う横方向の縮小幅


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("workdir")
    ap.add_argument("ocrcli")
    ap.add_argument("--from", dest="t0", type=float, default=0.0)
    ap.add_argument("--to", dest="t1", type=float, default=1e9)
    ap.add_argument("--y", type=int, default=900)
    ap.add_argument("--h", type=int, default=170)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--skip-band-check", action="store_true",
                     help="帯位置推定（数フレームの追加OCR）を省略する")
    return ap.parse_args(argv)


# --- 区間の組み立て（純粋関数） ---

def build_segments(bound, fps, t0, t1, min_duration=0.4, total_frames=None):
    """隣接する切替完了フレームの組から、min_duration秒以上かつ開始時刻が[t0,t1]に
    収まる区間だけを (開始フレーム, 終了フレーム) のリストで返す。

    total_frames（bounds.jsonの"frames"。subscan.pyが走査した総フレーム数）を渡すと、
    最初の境界より前(0, bound[0])・最後の境界から動画の終わりまで(bound[-1], total_frames)
    の区間も加える。これが無いと、動画の先頭や最後まで表示され続けるテロップが
    segs.json/telops.txtに一切含まれず、そこにカット点を置いたときalign_check.pyが
    「telops.txtにこの時刻を含む行が無い」と誤ってNGを出す（team-leadが合成素材で
    確認したバグ）。境界が1件も無い（テロップが全編通して同じ）場合は
    (0, total_frames) を1区間にする（先頭・末尾を別々に足して2区間にしない）。
    total_frames未指定（"frames"キーが無い古いbounds.json）では従来通り境界の間の
    区間だけで打ち切る。
    """
    def maybe_add(segs, fa, fb):
        s = fa / fps
        if (fb - fa) / fps >= min_duration and t0 <= s <= t1:
            segs.append((fa, fb))

    segs = []
    if total_frames is not None and not bound:
        maybe_add(segs, 0, total_frames)
        return segs

    if total_frames is not None and bound:
        maybe_add(segs, 0, bound[0])

    for i in range(len(bound) - 1):
        maybe_add(segs, bound[i], bound[i + 1])

    if total_frames is not None and bound:
        maybe_add(segs, bound[-1], total_frames)

    return segs


def band_shot_filename(prefix, frame, band_y, band_h):
    """区間の通し番号ではなく、中央フレーム番号と帯位置(band_y,band_h)を組み込んだ
    キャッシュファイル名を作る（拡張子.png込み）。通し番号（s%04d等）だと、--fromを
    変えて再実行したときに区間の対応が変わり、別の時刻の画像を古いキャッシュとして
    使い回してしまう（team-leadが確認したバグ）。--y/--hを変えた再実行でも同様に
    古い帯位置の画像を使い回さないよう、両方をファイル名に含める（mitchaku-kirinuki
    のsubscan.pyがフレーム番号をファイル名に使うのと同じ考え方）。
    """
    return "%s_%d_y%d_h%d.png" % (prefix, frame, band_y, band_h)


# --- ffmpegでの画像抽出（impure） ---

def extract_band_png(src, t, band_y, band_h, out_png):
    """時刻tの帯領域を960幅に縮小したPNGへ書き出す。既にあれば作り直さない。
    ffmpegが失敗したら黙って処理を続けず例外を送出する（空のテロップと混同しないため）。
    """
    if not os.path.exists(out_png):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", "%.3f" % t, "-i", src, "-frames:v", "1",
             "-vf", "crop=1920:%d:0:%d,scale=960:-1" % (band_h, band_y), out_png],
            check=True,
        )
    return out_png


def run_extractions_parallel(jobs, max_workers):
    """(src, t, band_y, band_h, out_png) のリストを並列に抽出する。失敗したジョブが
    あれば最初の例外をそのまま送出する（xargsの-Pと違い、1件失敗しても他は続けつつ、
    呼び出し元には必ず気づかせる）。
    """
    if not jobs:
        return
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(extract_band_png, *job) for job in jobs]
        for f in futures:
            f.result()


def sample_band_rows(src, times, band_y, band_h, tmp_dir, frame_height,
                      width=BAND_PROBE_ROW_WIDTH, margin=BAND_SEARCH_MARGIN_PX):
    """band_y推定用に、設定位置(band_y)の上下marginピクセルの探索窓を、指定した
    数フレーム分グレイ画素の2次元配列に足し合わせて返す（行=縦方向1px・元解像度のまま）。
    戻り値: (search_y0, rows)。search_y0は探索窓の絶対y座標（元1080基準）。
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
        with open(raw_path, "rb") as f:
            data = f.read()
        rows = [list(data[r * width:(r + 1) * width]) for r in range(search_h)]
        if all_rows is None:
            all_rows = rows
        else:
            all_rows = [[a + b for a, b in zip(ra, rb)] for ra, rb in zip(all_rows, rows)]
    return search_y0, all_rows


def make_ocr_text_at(src, ocrcli, band_y, band_h, fps, probe_dir):
    """kirinuki_core.telop_scan.refine_segment_start に渡す ocr_text_at(frame) を作る。
    OCRが失敗した場合は黙って空文字列にせず例外を送出する。

    抽出時刻はframe/fps（フレームの開始境界）を使う。kirinuki_core.video.frame_seek_time
    （フレーム中央time=(frame+0.5)/fps）は使わない。実測（tests/regression/fixtureの
    実素材、`-ss`のinput seek）で、frame+0.5秒でのシークが要求フレームの次のフレームを
    返すことがあり、二分探索の結果が実際より早いフレームにずれる可能性がある
    （Codexレビューで検出）。この探索は「中央のOCR結果と一致するようになる最初のフレーム」
    を大まかに求めるものであり厳密な1フレーム精度を必須としないため、境界時刻の方を使う。
    """
    def ocr_text_at(frame):
        probe_png = os.path.join(probe_dir, band_shot_filename("probe", frame, band_y, band_h))
        t = frame / fps
        extract_band_png(src, t, band_y, band_h, probe_png)
        probe_ocr, probe_failed = run_ocr(ocrcli, [probe_png])
        if probe_failed or missing_ocr_results([probe_png], probe_ocr):
            raise RuntimeError("フェードイン検査中にOCRが失敗しました: %s" % probe_png)
        return probe_ocr.get(probe_png, "")
    return ocr_text_at


def main(argv):
    args = parse_args(argv)

    with open(os.path.join(args.workdir, "bounds.json")) as f:
        B = json.load(f)
    fps, bound = B["fps"], B["bound"]

    frame_segs = build_segments(bound, fps, args.t0, args.t1, total_frames=B.get("frames"))

    shots = os.path.join(args.workdir, "subshots")
    os.makedirs(shots, exist_ok=True)

    mid_pngs = []
    mid_jobs = []
    for fa, fb in frame_segs:
        s, e = fa / fps, fb / fps
        mid_frame = (fa + fb) // 2
        png = os.path.join(shots, band_shot_filename("s", mid_frame, args.y, args.h))
        mid_jobs.append((args.src, (s + e) / 2, args.y, args.h, png))
        mid_pngs.append(png)

    run_extractions_parallel(mid_jobs, args.jobs)

    ocr, ocr_failed = run_ocr(args.ocrcli, mid_pngs)
    if ocr_failed:
        print("エラー: OCRプロセスが失敗しました。telops.txt/segs.jsonは書き出しません", file=sys.stderr)
        return 1
    missing = missing_ocr_results(mid_pngs, ocr)
    if missing:
        print("エラー: OCR結果が欠落しています(%d件)。telops.txt/segs.jsonは書き出しません: %s"
              % (len(missing), missing[:5]), file=sys.stderr)
        return 1

    # ① フェードイン対策：区間先頭フレームが中央と食い違えば真の開始フレームへ補正する
    probe_dir = os.path.join(args.workdir, "subshots_probe")
    os.makedirs(probe_dir, exist_ok=True)
    ocr_text_at = make_ocr_text_at(args.src, args.ocrcli, args.y, args.h, fps, probe_dir)

    refined = []
    corrected_count = 0
    for i, (fa, fb) in enumerate(frame_segs):
        center_text = ocr.get(mid_pngs[i], "")
        mid_frame = (fa + fb) // 2
        new_fa = refine_segment_start(ocr_text_at, fa, mid_frame, center_text) if mid_frame > fa else fa
        if new_fa != fa:
            # 注意: 補正後の開始フレームはtelops.txt/segs.jsonにのみ反映され、
            # bounds.json（safe.py/pick.pyの候補・render.py等のgstartスナップの元）は
            # 更新しない。safe.txtからこの区間の頭を選ぶ場合は補正前のフレームのままなので、
            # align_check.pyのNGで気づいたら、この区間がフェードイン補正されていないか確認する
            print("補正: セグメント%d の開始フレームを%d→%dに調整しました（フェードイン対策）"
                  % (i, fa, new_fa), file=sys.stderr)
            corrected_count += 1
        refined.append((new_fa, fb, center_text))

    segs = [(new_fa / fps, fb / fps) for new_fa, fb, _ in refined]
    texts = [t for _, _, t in refined]

    # ② 空行率・帯位置の警告
    warn = empty_rate_warning(empty_rate(texts))
    if warn:
        print(warn, file=sys.stderr)

    if not args.skip_band_check and frame_segs:
        sample_indices = list(range(0, len(frame_segs), max(1, len(frame_segs) // 5)))[:5]
        with tempfile.TemporaryDirectory() as tmp_dir:
            sample_times = [(segs[k][0] + segs[k][1]) / 2 for k in sample_indices]
            frame_height = get_frame_height(args.src)
            search_y0, rows = sample_band_rows(args.src, sample_times, args.y, args.h,
                                                tmp_dir, frame_height)
            profile = edge_row_profile(rows)
            estimated = search_y0 + best_band_start(profile, args.h)
            band_warn = band_offset_warning(estimated, args.y)
            if band_warn:
                print(band_warn, file=sys.stderr)

    with open(os.path.join(args.workdir, "segs.json"), "w") as f:
        json.dump(segs, f)

    with open(os.path.join(args.workdir, "telops.txt"), "w", encoding="utf-8") as f:
        for i, (s, e) in enumerate(segs):
            f.write("%3d %8.3f-%8.3f (%4.1fs) %s\n" % (i, s, e, e - s, texts[i]))
    print("segments=%d -> %s/telops.txt" % (len(segs), args.workdir))
    if corrected_count:
        print("フェードイン補正: %d件（safe.txtの候補は補正前のまま。"
              "align_check.pyのNGで気づいた場合はこの区間を確認）" % corrected_count,
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
