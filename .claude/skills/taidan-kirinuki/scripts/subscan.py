# -*- coding: utf-8 -*-
"""元データの焼き込みテロップ帯を全フレーム走査し、切り替わりフレームを検出する（第3段階で関数化）。
使い方: python3 subscan.py <src.mp4> <workdir> [--y 900] [--h 170] [--th 8]
出力: <workdir>/bounds.json   {"fps":.., "fps_str":.., "bound":[切替完了フレーム], "gstart":{完了:開始}}

第3段階での変更点:
  ・fpsをffprobe(r_frame_rate)から取得する（固定値30000/1001をやめる。30fps・25fps素材にも対応）。
    有理数表記(fps_str、例"30000/1001")もbounds.jsonに書く（common.cut_range等が使う）
  ・可変フレームレート素材（r_frame_rateとavg_frame_rateが食い違う）は警告して終了コード1
    （タイムベースと実測平均が食い違うとカット点がずれるため）
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _core  # noqa: E402
_core.ensure_kirinuki_core_on_path()

from kirinuki_core.video import get_stream_rates, is_variable_frame_rate, parse_frame_rate  # noqa: E402

W, H = 240, 21


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("workdir")
    ap.add_argument("--y", type=int, default=900, help="テロップ帯の上端（1080基準）")
    ap.add_argument("--h", type=int, default=170)
    ap.add_argument("--th", type=float, default=8.0)
    return ap.parse_args(argv)


def frame_diff(frame, prev, frame_size, sample_stride=3):
    """隣接フレーム間の平均輝度差（sample_stride間隔で間引いて計算。既存の実装通り）。"""
    return (sum(abs(frame[k] - prev[k]) for k in range(0, frame_size, sample_stride))
            / (frame_size / float(sample_stride)))


def group_changepoints(raw_indices, max_gap=3):
    """しきい値を超えたフレーム番号のリストを、max_gap以内で連続するグループにまとめ、
    各グループの (開始フレーム, 終了フレーム) のリストを返す。
    """
    groups = []
    for k in raw_indices:
        if groups and k - groups[-1][-1] <= max_gap:
            groups[-1].append(k)
        else:
            groups.append([k])
    return [(g[0], g[-1]) for g in groups]


def check_variable_frame_rate(src):
    """VFR素材なら警告メッセージを返す（固定フレームレートならNone）。"""
    r_rate, avg_rate = get_stream_rates(src)
    if is_variable_frame_rate(r_rate, avg_rate):
        return ("エラー: 可変フレームレート素材です（r_frame_rate=%s, avg_frame_rate=%s）。"
                 "タイムベースと実測平均が食い違うためカット点がずれます。"
                 "固定フレームレートに変換してから使ってください" % (r_rate, avg_rate)), r_rate
    return None, r_rate


def scan_band_diffs(src, y, h, threshold, w=W, hh=H):
    """帯領域を縮小グレイ画像として全フレーム走査し、しきい値を超えたフレーム番号の
    リストと総フレーム数を返す（メモリに全フレームを持たず、直前フレームとの差分だけ保持する）。

    ffmpegの終了コードを見ずbounds.jsonを上書きしていた回帰の修正（Codexレビューで
    検出）：走査が失敗して終わった場合、または1フレームも読めなかった場合は
    RuntimeErrorを送出する（呼び出し側が空/不完全なbounds.jsonを書かないようにするため）。
    """
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", src,
         "-vf", "crop=1920:%d:0:%d,scale=%d:%d,format=gray" % (h, y, w, hh),
         "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    n = w * hh
    prev, raw, i = None, [], 0
    while True:
        buf = p.stdout.read(n)
        if len(buf) < n:
            break
        if prev is not None and frame_diff(buf, prev, n) > threshold:
            raw.append(i)
        prev = buf
        i += 1
    p.stdout.close()
    returncode = p.wait()
    if returncode != 0:
        stderr = p.stderr.read() if p.stderr else b""
        raise RuntimeError("テロップ帯の走査に失敗しました(ffmpeg exit=%d): %s\n%s"
                            % (returncode, src,
                               stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr))
    if i == 0:
        raise RuntimeError("テロップ帯の走査でフレームが1件も読めませんでした: %s" % src)
    return raw, i


def main(argv):
    args = parse_args(argv)
    os.makedirs(args.workdir, exist_ok=True)

    vfr_warning, fps_str = check_variable_frame_rate(args.src)
    if vfr_warning:
        print(vfr_warning, file=sys.stderr)
        return 1
    fps = parse_frame_rate(fps_str)

    raw, n_frames = scan_band_diffs(args.src, args.y, args.h, args.th)
    groups = group_changepoints(raw)
    # "frames"（総フレーム数）は、subtext.build_segmentsが最後の境界から動画の終わりまでを
    # 最後の区間として作るのに使う（無いと、最後の境界より後ろのテロップがsegs.json/
    # telops.txtに一切含まれず、そこにカット点を置くとalign_check.pyが誤NGになる）。
    with open(os.path.join(args.workdir, "bounds.json"), "w") as f:
        json.dump({"fps": fps, "fps_str": fps_str, "frames": n_frames,
                   "bound": [g[1] for g in groups],
                   "gstart": {str(g[1]): g[0] for g in groups}}, f)
    print("frames=%d  boundaries=%d" % (n_frames, len(groups)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
