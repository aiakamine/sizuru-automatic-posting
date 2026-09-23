# -*- coding: utf-8 -*-
"""編集指示テキスト（カットリスト＋各カットの元テロップ全文）を生成する（第3段階で関数化）。
使い方: python3 gen_docs.py <workdir> <clips.py> <dest_dir> [--out DIR]

第3段階での変更点: カット区間の計算をcommon.cut_rangeに一本化（fps固定をやめ、
bounds.jsonのfpsを使う）。表示するfpsも実際の素材のfpsにする（29.97固定表記をやめる）。
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

NOISE = ("apos", "apnpn", "smdecode", "www.", "Www.", "ClaudeはAI", "回答内容は必ず")


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("clips")
    ap.add_argument("dest")
    ap.add_argument("--out", default=None)
    return ap.parse_args(argv)


def clean(t):
    for w in NOISE:
        t = t.replace(w, " ")
    return " ".join(t.split())


def lines_between(segs, txt, s, e):
    out = []
    for i, (x, y) in enumerate(segs):
        if x >= s - 0.05 and x <= e - 0.15:
            t = clean(txt.get(i, ""))
            if len(t) >= 2 and (not out or out[-1][1] != t):
                out.append((x, t))
    return out


def tc(x):
    return "%02d:%05.2f" % (int(x // 60), x % 60)


def timeline(clip, bounds, default_xfade):
    XF = clip.get("xfade", default_xfade)
    tl, t0 = [], 0.0
    for i, cut in enumerate(clip["cuts"]):
        S, E = C.cut_range(cut, bounds)
        tl.append((S, E, E - S, t0, cut))
        t0 += E - S - (XF if i == 0 else 0.0)
    return tl, t0


def fps_label(bounds):
    """本編集への申し送りに表示するfps表記（例 "29.97fps"）。"""
    return "%.2ffps" % bounds["fps"]


def measure_lufs(mp4_path):
    if not os.path.exists(mp4_path):
        return ""
    r = subprocess.run(["ffmpeg", "-nostats", "-i", mp4_path, "-filter_complex", "ebur128",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    v = [line.split(":")[1].strip() for line in r.splitlines() if line.strip().startswith("I:")]
    return v[-1] if v else ""


def build_doc_body(n, clip, tl, total, segs, txt, layout, source, bounds, default_xfade):
    XF = clip.get("xfade", default_xfade)
    body = ["切り抜き%02d「%s」" % (n, clip["file"].rsplit(".", 1)[0]),
            "素材：%s" % source,
            "尺：%.2f秒／1080x1920／%s" % (total, fps_label(bounds)), "",
            "■ レイアウト（実測）",
            "バナー　　　天から%dpx（本体は天から%dpx・高さ%dpx）"
            % (layout["banner_y"], layout["banner_y"] + 40, layout["banner_body_h"]),
            "映像バンド　天から%dpx、%dx%dpx" % (layout["band_y"], layout["band_w"], layout["band_h"]),
            "CTA　　　　天から%dpx" % layout["cta_y"],
            "テロップ　　元データの焼き込みをそのまま使用（追加テロップなし）",
            "トランジション　%s" % ("C1→C2のみ白フラッシュ%.2f秒。以降はカット" % XF if XF > 0 else "なし（元データのアイキャッチが兼ねる）"),
            "", "■ カットリスト（元データのタイムコード／秒）",
            "　頭＝元テロップが切り替わったフレーム、尻＝次のテロップに変わる直前のフレーム。",
            "　各カットの下は、そのカットに入っている元テロップの全文（OCR。画面録画のカットでは",
            "　背景の文字が少し混ざる行があります）。"]
    for i, (S, E, D, t0, cut) in enumerate(tl, 1):
        body.append("")
        body.append("C%-2d %s-%s（%.2f秒）  ←  %s-%s（%.3f-%.3f）%s"
                    % (i, tc(t0), tc(t0 + D), D, tc(S), tc(E), S, E,
                       "　※この後に白フラッシュ%.2f秒" % XF if (XF > 0 and i == 1) else ""))
        body.append("    %s" % cut.get("note", ""))
        if cut.get("crop"):
            cw, ch, cx, cy = cut["crop"]
            body.append("    寄せ：crop %dx%d @ x=%d,y=%d（元データ比%.2f倍）"
                        % (cw, ch, cx, cy, 1080.0 / cw / 0.5625))
        for s, t in lines_between(segs, txt, S, E):
            body.append("      %s  %s" % (tc(s), t))
    return body


def main(argv):
    args = parse_args(argv)
    M = C.load_clips(args.clips)
    BD = C.load_bounds(args.workdir)
    L = M.LAYOUT
    OUT = args.out or os.path.join(args.workdir, "out")
    segs = json.load(open(os.path.join(args.workdir, "segs.json")))
    txt = {}
    with open(os.path.join(args.workdir, "telops.txt"), encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(") ", 1)
            if len(parts) == 2:
                txt[int(line[:3])] = parts[1].strip()

    os.makedirs(os.path.join(args.dest, "編集指示"), exist_ok=True)
    for n, clip in enumerate(M.CLIPS, 1):
        tl, total = timeline(clip, BD, M.XFADE)
        body = build_doc_body(n, clip, tl, total, segs, txt, L, M.SOURCE, BD, M.XFADE)
        lufs = measure_lufs(os.path.join(OUT, clip["file"]))
        body += ["", "■ 本編集で必ず直すこと",
                 "1. BGMを敷く（現状は元データの音のみ。ラウドネスは%s。BGM後に-14 LUFS前後へ）" % (lufs or "未計測"),
                 "2. バナーは公開版デザインの再現。正規テンプレがあれば差し替える",
                 "3. 元データ左上のチャプター表示の映り込み（編集データ側でレイヤーをオフにすれば消える）",
                 "4. 個人情報のマスク確認"]
        p = os.path.join(args.dest, "編集指示", "%s.txt" % clip["slug"])
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(body) + "\n")
        print(os.path.basename(p), "%.2f秒" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
