# -*- coding: utf-8 -*-
"""対談・解説系 切り抜きレンダラ
  ・テロップは元データの焼き込みをそのまま使う（自前テロップは足さない）
  ・カット尻はテロップが変わり始める手前にスナップして表示ずれを防ぐ
  ・C1（ハイライト）→C2（本編）は白フラッシュのトランジション
  ・映像と音声は trim/atrim＋concat フィルタで一括処理し、A/Vのズレを出さない
使い方:
  python3 render.py <src.mp4> <workdir> <clips.py> --cta <cta.mp4> [--out DIR] [--only slug ...]

第3段階での変更点: 出力fpsを固定値(29.97)から bounds.json の fps に変える（30fps・25fps
素材にも対応する）。banner.pyの30fps連番→-framerate 30はバナー素材自体のfpsなので変えない
（overlayは-stream_loopで出力fpsに合わせる）。カット区間の計算はcommon.cut_rangeに一本化。
フィルタ文字列・コマンド組み立てを関数化し、boundsのfpsが実際に反映されることをテストする。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import banner as B


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("workdir")
    ap.add_argument("clips")
    ap.add_argument("--cta", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", nargs="*", default=[], help="対象スラッグ（既定＝全部）")
    return ap.parse_args(argv)


def xf_of(clip, default_xfade):
    return clip.get("xfade", default_xfade)


def build_banner(clip, build_dir, run_fn=C.run):
    d = os.path.join(build_dir, clip["slug"], "banner")
    os.makedirs(os.path.join(build_dir, clip["slug"]), exist_ok=True)
    if not os.path.exists(os.path.join(d, "b_119.png")):
        B.build(clip["banner"], d)
    mov = os.path.join(build_dir, clip["slug"], "banner.mov")
    if not os.path.exists(mov):
        run_fn(["ffmpeg", "-y", "-v", "error", "-framerate", "30",
                "-i", os.path.join(d, "b_%03d.png"), "-c:v", "qtrle", "-pix_fmt", "argb", mov])
    return mov


def timeline(clip, bounds, default_xfade):
    """(素材IN, 素材OUT, 尺, 出力上の開始秒, cut) のリストと合計尺。C1→C2はXF秒重なる。"""
    XF = xf_of(clip, default_xfade)
    tl, t0 = [], 0.0
    for i, cut in enumerate(clip["cuts"]):
        S, E = C.cut_range(cut, bounds)
        tl.append((S, E, E - S, t0, cut))
        t0 += E - S - (XF if i == 0 else 0.0)
    return tl, t0


def build_filter_complex(tl, total, clip, bounds, layout, default_crop, xfade):
    """filter_complexの各フィルタ文字列のリストを返す（ffmpegは呼ばない・純粋関数）。
    出力fpsは bounds["fps_str"]（例 "30000/1001"）を使う＝素材のfpsに合わせて出力する。
    """
    n = len(tl)
    fc = ["[0:v]split=%d%s" % (n, "".join("[sv%d]" % i for i in range(n))),
          "[0:a]asplit=%d%s" % (n, "".join("[sa%d]" % i for i in range(n)))]
    for i, (S, E, D, t0, cut) in enumerate(tl):
        cw, ch, cx, cy = cut.get("crop") or default_crop
        fc.append("[sv%d]trim=start=%.5f:end=%.5f,setpts=PTS-STARTPTS,"
                  "crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos,fps=%s,setsar=1,format=yuv420p[v%d]"
                  % (i, S, E, cw, ch, cx, cy, layout["band_w"], layout["band_h"], bounds["fps_str"], i))
        af = [] if (xfade > 0 and i == 1) else ["afade=t=in:st=0:d=0.08"]
        if not (xfade > 0 and i == 0):
            af.append("afade=t=out:st=%.3f:d=0.10" % max(0.0, D - 0.10))
        fc.append("[sa%d]atrim=start=%.5f:end=%.5f,asetpts=PTS-STARTPTS%s[a%d]"
                  % (i, S, E, ("," + ",".join(af)) if af else "", i))
    if xfade > 0:
        fc.append("[v0][v1]xfade=transition=fadewhite:duration=%.3f:offset=%.3f[vx]" % (xfade, tl[0][2] - xfade))
        fc.append("[a0][a1]acrossfade=d=%.3f:c1=tri:c2=tri[ax]" % xfade)
        pairs = "[vx][ax]" + "".join("[v%d][a%d]" % (i, i) for i in range(2, n))
        fc.append("%sconcat=n=%d:v=1:a=1[bv][ba]" % (pairs, n - 1))
    else:
        pairs = "".join("[v%d][a%d]" % (i, i) for i in range(n))
        fc.append("%sconcat=n=%d:v=1:a=1[bv][ba]" % (pairs, n))
    fc.append("[1:v][bv]overlay=0:%d:shortest=1[c1]" % layout["band_y"])
    fc.append("[c1][2:v]overlay=%d:%d[c2]" % (layout["banner_x"], layout["banner_y"]))
    fc.append("[c2][3:v]overlay=%d:%d[c3]" % (layout["cta_x"], layout["cta_y"]))
    of = clip.get("outfade", 0.30)
    fc.append("[c3]fade=t=out:st=%.3f:d=%.3f,format=yuv420p[vout]" % (max(0.0, total - of), of))
    fc.append("[ba]afade=t=out:st=%.3f:d=%.3f[aout]" % (max(0.0, total - of - 0.05), of + 0.05))
    return fc


def build_render_command(src, cta, banner_mov, fc, total, bounds, outp):
    """ffmpeg実行コマンド（argvリスト）を組み立てる（純粋関数。実行はしない）。
    -r・出力fpsに bounds["fps_str"] を使う（固定29.97をやめ、素材のfpsに合わせる）。
    """
    return ["ffmpeg", "-y", "-v", "error", "-i", src,
            "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=%s" % bounds["fps_str"],
            "-stream_loop", "-1", "-i", banner_mov,
            "-stream_loop", "-1", "-i", cta,
            "-filter_complex", ";".join(fc),
            "-map", "[vout]", "-map", "[aout]", "-t", "%.3f" % total,
            "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-r", bounds["fps_str"],
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", outp]


def render(clip, args, bounds, layout, default_crop, default_xfade, out_dir, build_dir, run_fn=C.run):
    os.makedirs(out_dir, exist_ok=True)
    banner_mov = build_banner(clip, build_dir, run_fn=run_fn)
    xfade = xf_of(clip, default_xfade)
    tl, total = timeline(clip, bounds, default_xfade)
    fc = build_filter_complex(tl, total, clip, bounds, layout, default_crop, xfade)
    outp = os.path.join(out_dir, clip["file"])
    cmd = build_render_command(args.src, args.cta, banner_mov, fc, total, bounds, outp)
    run_fn(cmd)
    return outp, total


def main(argv):
    args = parse_args(argv)
    M = C.load_clips(args.clips)
    BD = C.load_bounds(args.workdir)
    build_dir = os.path.join(args.workdir, "build")
    out_dir = args.out or os.path.join(args.workdir, "out")

    which = set(args.only) or {c["slug"] for c in M.CLIPS}
    for clip in M.CLIPS:
        if clip["slug"] not in which:
            continue
        p, t = render(clip, args, BD, M.LAYOUT, M.DEFAULT_CROP, M.XFADE, out_dir, build_dir)
        print("OK", os.path.basename(p), "%.2f秒" % t)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
