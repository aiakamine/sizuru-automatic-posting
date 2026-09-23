# -*- coding: utf-8 -*-
import argparse, subprocess, sys, json, os, re, math, hashlib
import importlib.util
SRC="src.mp4"
FPS="30000/1001"
FPSF = 30000.0 / 1001.0  # フレームスナップ・timeline.json 用（build_ffmpeg_cmd の "30000/1001" 文字列と同じ値）
HALF_FRAME = 0.5 / FPSF  # 半フレーム（秒）。-ssの丸め誤差でカット端の1フレームが
# 落ちないよう、trim窓と-tを半フレーム分前倒し／延長する安全マージン（I-12）。

# 本編映像を縮小して重ねる位置とサイズ（build_ffmpeg_cmdのscale/overlayと同じ値）。
# 回帰テスト側（元素材との対応チェック）が同じ座標系を要るため、ここで名前を付けて公開する。
VIDEO_BAND_WIDTH = 1080
VIDEO_BAND_HEIGHT = 606
VIDEO_BAND_Y = 552

# 各カットを別入力にして、入力側 -ss でシークしてから trim で微調整する。
# 後半のカットほど先頭からデコードして遅くなる問題を避ける（精度は trim が担保する）。
SEEK_MARGIN = 2.0  # 秒。カット開始のこの秒数手前からシークする（キーフレーム跨ぎの安全マージン）


def _seek_and_offset(a, seek_margin):
    """cutの開始時刻aに対する (入力側-ss値, そこからのオフセット) を返す。
    a が margin より手前なら ss は0にクランプする（負値にしない）。
    """
    ss = max(0.0, a - seek_margin)
    return ss, a - ss


def build_ffmpeg_cmd(cuts, base_png, out, flash_after=0, flash_dur=0.35,
                      fade_in=0.10, fade_out=0.10, tail_fade=0.35, seek_margin=SEEK_MARGIN):
    """ffmpegコマンド一式と合計尺を組み立てる（副作用なし。単体テストのために実行と分離）。
    cuts: [(start,end), ...]  flash_after: この後で白フラッシュ（-1/Noneで無し）
    """
    seeks = [_seek_and_offset(a, seek_margin) for a, _b in cuts]

    fc = []
    for i, ((a, b), (ss, offset)) in enumerate(zip(cuts, seeks)):
        d = b - a
        # -ssは.3fで丸めて渡すため、実際のシーク位置が理論値からサブミリ秒ずれうる。
        # trim窓を半フレーム前倒しして、その丸め誤差でカット端の1フレームが
        # 境界外に落ちないようにする（I-12）。
        start, end = max(0.0, offset - HALF_FRAME), offset + d - HALF_FRAME
        fc.append(f"[{i}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
                  f"scale={VIDEO_BAND_WIDTH}:{VIDEO_BAND_HEIGHT}:flags=lanczos,fps={FPS}[v{i}]")
        fc.append(f"[{i}:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
                  f"afade=t=in:st=0:d={fade_in},afade=t=out:st={max(0,d-fade_out):.3f}:d={fade_out}[a{i}]")

    n = len(cuts)
    if flash_after is not None and 0 <= flash_after < n - 1:
        # group A = cuts[0..flash_after], group B = cuts[flash_after+1..]
        ka = flash_after + 1
        if ka == 1:
            fc.append("[v0]null[VA]"); fc.append("[a0]anull[AA]")
        else:
            fc.append("".join(f"[v{i}]" for i in range(ka)) + f"concat=n={ka}:v=1:a=0[VA]")
            fc.append("".join(f"[a{i}]" for i in range(ka)) + f"concat=n={ka}:v=0:a=1[AA]")
        kb = n - ka
        if kb == 1:
            fc.append(f"[v{ka}]null[VB]"); fc.append(f"[a{ka}]anull[AB]")
        else:
            fc.append("".join(f"[v{i}]" for i in range(ka, n)) + f"concat=n={kb}:v=1:a=0[VB]")
            fc.append("".join(f"[a{i}]" for i in range(ka, n)) + f"concat=n={kb}:v=0:a=1[AB]")
        dur_a = sum(b - a for a, b in cuts[:ka])
        off = max(0.0, dur_a - flash_dur)
        fc.append("[VA]settb=1001/30000[VAt]")
        fc.append("[VB]settb=1001/30000[VBt]")
        fc.append(f"[VAt][VBt]xfade=transition=fadewhite:duration={flash_dur}:offset={off:.3f}[VC]")
        fc.append(f"[AA][AB]acrossfade=d={flash_dur}:c1=tri:c2=tri[AC]")
        total = sum(b - a for a, b in cuts) - flash_dur
    else:
        fc.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[VC]")
        fc.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[AC]")
        total = sum(b - a for a, b in cuts)

    base_idx = n  # base_png の入力インデックス（カット用入力の直後）
    fc.append(f"[{base_idx}:v][VC]overlay=0:{VIDEO_BAND_Y}:shortest=1,format=yuv420p[VOUT]")
    fc.append(f"[AC]afade=t=out:st={max(0,total-tail_fade):.3f}:d={tail_fade},"
              f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[AOUT]")

    cmd = ["ffmpeg", "-v", "error", "-y"]
    for ss, _offset in seeks:
        cmd += ["-ss", f"{ss:.3f}", "-i", SRC]
    # 背景PNGは既定25fpsでループされ、それが overlay の出力タイミングを支配してしまう
    # （本編側でフレーム重複が発生する）。本編と同じFPSを明示して合わせる（I-11）。
    cmd += ["-framerate", FPS, "-loop", "1", "-i", base_png,
            "-filter_complex", ";".join(fc),
            "-map", "[VOUT]", "-map", "[AOUT]",
            # -tも同様に半フレームの余裕を持たせる（totalちょうどで.3f丸めすると
            # 末尾フレームが境界の丸めで削られうる）。映像側の実尺はtrim/concatが
            # 既に決めているため、ここで余分なフレームが増えることはない。
            "-t", f"{total + HALF_FRAME:.3f}",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-r", FPS, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    return cmd, total


def render(cuts, base_png, out, flash_after=0, flash_dur=0.35,
           fade_in=0.10, fade_out=0.10, tail_fade=0.35):
    """cuts: [(start,end), ...]  flash_after: index after which white flash (-1 = none)"""
    cmd, total = build_ffmpeg_cmd(cuts, base_png, out, flash_after, flash_dur,
                                   fade_in, fade_out, tail_fade)
    subprocess.run(cmd, check=True)
    return total


# ============================================================
# CLI（ここから下: フレームスナップ・timeline.json・2パスloudnorm）
# build_ffmpeg_cmd / render の引数・戻り値はここでは一切変更しない。
# ============================================================

def snap_frame(t):
    """秒 t を出力フレーム格子（30000/1001）上の最寄りフレーム番号に丸める。"""
    return round(t * FPSF)


def frame_time(f):
    """フレーム番号 f をそのフレームの時刻（秒）に変換する（snap_frame の逆）。"""
    return f / FPSF


def snap_cuts(cuts):
    """cuts（秒）をフレーム格子にスナップし、[(src_start_frame, src_end_frame), ...] を返す。
    以下はいずれも ValueError で明示的に落とす（不正な入力を黙って書き出さない）:
      - cutsが空
      - 開始・終了が非有限（NaN/Inf）
      - 開始が負
      - スナップの結果 end<=start になったカット（1秒未満の極端に短いカット等）
    """
    if not cuts:
        raise ValueError("cutsが空です（最低1カット必要です）")
    snapped = []
    for i, (a, b) in enumerate(cuts):
        if not (math.isfinite(a) and math.isfinite(b)):
            raise ValueError(
                "カット%d: 開始・終了は有限の数値である必要があります(a=%r, b=%r)" % (i, a, b))
        if a < 0:
            raise ValueError("カット%d: 開始(%.3f)が負です" % (i, a))
        sa, sb = snap_frame(a), snap_frame(b)
        if sb <= sa:
            raise ValueError(
                "カット%d: フレームスナップ後に end(f%d) が start(f%d) 以下になりました"
                % (i, sb, sa)
            )
        snapped.append((sa, sb))
    return snapped


def build_timeline(cut_frames, flash_after=0, flash_frames=0):
    """フレーム番号ベースのタイムラインを組み立てる（qc_cuts.py と共有する座標系）。
    cut_frames: [(src_start_frame, src_end_frame), ...]
    戻り値: (cuts_info, total_frames)
    """
    n = len(cut_frames)
    has_flash = flash_after is not None and 0 <= flash_after < n - 1
    n_frames_list = [b - a for a, b in cut_frames]

    out_starts = []
    t = 0
    limit = (flash_after + 1) if has_flash else n
    for i in range(limit):
        out_starts.append(t)
        t += n_frames_list[i]
    if has_flash:
        t -= flash_frames
        for i in range(limit, n):
            out_starts.append(t)
            t += n_frames_list[i]
    total_frames = t

    cuts_info = []
    for i, ((sa, sb), nf, ostart) in enumerate(zip(cut_frames, n_frames_list, out_starts)):
        cuts_info.append({
            "index": i,
            "src_start_frame": sa,
            "src_end_frame": sb,
            "n_frames": nf,
            "out_start_frame": ostart,
        })
    return cuts_info, total_frames


def write_timeline(out_path, cuts_info, total_frames, flash_after, flash_dur, flash_frames):
    """<out_path>.timeline.json を書き出す（qc_cuts.py が読む唯一の座標系）。"""
    data = {
        "fps": FPS,
        "flash_after": flash_after,
        "flash_dur": flash_dur,
        "flash_frames": flash_frames,
        "total_frames": total_frames,
        "cuts": cuts_info,
    }
    timeline_path = out_path + ".timeline.json"
    with open(timeline_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return timeline_path


# --- ラウドネス正規化（音声のみ再エンコード・映像はcopy） ---
#
# 判定・検証はebur128のIntegrated loudnessに統一する。loudnormのprint_format=json
# (measure_loudnorm)は同じ素材でもebur128実測より最大0.3LU程度低く出る系統的バイアスが
# あり（2026-09-17実測）、これで合否判定すると「補正済みのつもりが実は目標より低いまま」
# になる。measure_loudnormは1回目の補正afに渡すmeasured_*の実測にのみ使う。

LOUDNESS_TARGET_I = -14.0
LOUDNESS_TOLERANCE = 0.5  # QC合否（run.py・expected.json）の許容値。ここでは補正しない
LOUDNESS_CORRECTION_THRESHOLD = 0.2  # fix_loudness内で補正を開始する閾値（TOLERANCEより厳しく）
LOUDNESS_MAX_CORRECTION_ATTEMPTS = 2  # 1回目loudnorm・2回目volumeで打ち切る
TRUE_PEAK_CEILING = -1.5  # loudnormのTP目標と揃える
# 最終エンコード後の合否判定用のTrue Peak上限。補正の目標(TRUE_PEAK_CEILING=-1.5)より
# 緩い値にしている。AACの世代重ね等で数割dBの上振れが起こりうることを実データで確認して
# おり（2026-09-17）、それ自体をFAILにはしないが、実質的にクリッピングに近づく水準
# （0dBFSまで1dBを切る）は見逃さない。
TRUE_PEAK_MAX_ACCEPTABLE = -1.0
# クランプ後のゲインがこれ未満なら「安全にはこれ以上補正できない」とみなし、2回目の
# volumeパスを丸ごとスキップする。得るもの(ラウドネス改善)が無いのに音声を再エンコード
# し直すと、AACの世代重ねでTrue Peakがかえって悪化することを実データで確認した
# （2026-09-17、-1.2dBFS→+0.4dBFSに増加）。
MIN_MEANINGFUL_GAIN_DB = 0.1


_LOUDNORM_REQUIRED_FIELDS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


def measure_loudnorm(path):
    """loudnorm(print_format=json) で I/TP/LRA/thresh/offset を実測する。1回目の補正に
    渡すmeasured_*の実測専用（判定・検証には使わない。上のモジュールコメント参照）。
    ffmpeg自体が失敗(returncode!=0)した場合や、必須フィールドが欠けている場合は
    RuntimeErrorで明示的に落とす（黙って不完全な値を使わない）。
    """
    cmd = ["ffmpeg", "-v", "info", "-i", path,
           "-af", "loudnorm=I=%.1f:TP=-1.5:LRA=11:print_format=json" % LOUDNESS_TARGET_I,
           "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            "loudnormの実測に失敗しました(returncode=%d): %s" % (r.returncode, r.stderr[-500:]))
    text = r.stderr
    start, end = text.rfind("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("loudnormの実測結果を解析できませんでした: %s" % path)
    stats = json.loads(text[start:end + 1])
    missing = [f for f in _LOUDNORM_REQUIRED_FIELDS if f not in stats]
    if missing:
        raise RuntimeError("loudnormの実測結果に必須フィールドがありません: %s" % missing)
    return stats


_EBUR128_INTEGRATED_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+)\s*LUFS")
_EBUR128_TRUE_PEAK_RE = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+)\s*dBFS")


def parse_ebur128_summary(text):
    """ebur128フィルタの出力から最終Summaryの値を取り出す（純粋関数）。
    フレーム毎のログ行にも "I: ... LUFS" が出るため、末尾のSummaryセクションだけを見る。
    True Peakはpeak=trueを付けていないと出ないので、その場合はNoneを返す。
    """
    summary_start = text.rfind("Summary:")
    if summary_start == -1:
        raise RuntimeError("ebur128の実測結果(Summary)を解析できませんでした")
    summary_text = text[summary_start:]
    integrated_match = _EBUR128_INTEGRATED_RE.search(summary_text)
    if not integrated_match:
        raise RuntimeError("ebur128のIntegrated loudnessを解析できませんでした")
    true_peak_match = _EBUR128_TRUE_PEAK_RE.search(summary_text)
    return {
        "integrated": float(integrated_match.group(1)),
        "true_peak": float(true_peak_match.group(1)) if true_peak_match else None,
    }


def measure_ebur128(path):
    """ebur128フィルタでIntegrated loudness（とTrue Peak）を実測する。判定・検証はこちらに統一する。
    ffmpeg自体が失敗(returncode!=0)した場合や、peak=trueを付けているのにTrue Peakが
    解析できない場合はRuntimeErrorで明示的に落とす（黙って不完全な値を使わない）。
    """
    cmd = ["ffmpeg", "-v", "info", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            "ebur128の実測に失敗しました(returncode=%d): %s" % (r.returncode, r.stderr[-500:]))
    summary = parse_ebur128_summary(r.stderr)
    if summary["true_peak"] is None:
        raise RuntimeError("ebur128の実測結果にTrue Peakがありません（peak=trueが効いていない可能性）")
    return summary


def needs_loudness_correction(measured_i, target=LOUDNESS_TARGET_I,
                               threshold=LOUDNESS_CORRECTION_THRESHOLD):
    """ebur128実測値が目標からthresholdより外れているか（純粋関数）。"""
    return abs(measured_i - target) > threshold


def loudness_result_ok(integrated, true_peak, target=LOUDNESS_TARGET_I,
                        tolerance=LOUDNESS_TOLERANCE, tp_max=TRUE_PEAK_MAX_ACCEPTABLE):
    """最終エンコード後の実測(ebur128)が許容範囲内かを判定する（純粋関数）。
    fix_loudnessはどの分岐（補正した/しなかった/打ち切った）を通っても、必ずここで
    最終状態を確認する。戻り値: (合格か, [理由文字列, ...])。
    """
    problems = []
    if abs(integrated - target) > tolerance:
        problems.append(
            "Integrated loudness %.2f LUFSが目標%.1f±%.1fから外れています"
            % (integrated, target, tolerance))
    if true_peak is not None and true_peak > tp_max:
        problems.append(
            "True Peak %.2f dBFSが上限%.1fを超えています" % (true_peak, tp_max))
    return (len(problems) == 0), problems


def gain_to_target(measured_i, target=LOUDNESS_TARGET_I):
    """目標に到達するのに必要なゲイン(dB)を返す（純粋関数）。"""
    return target - measured_i


def clamp_gain_for_true_peak(gain_db, true_peak, ceiling=TRUE_PEAK_CEILING):
    """True Peakがceilingを超えないよう、必要ならgain_dbを抑える（純粋関数）。
    true_peakの実測が無ければ判断しようがないのでgain_dbをそのまま返す。
    ブースト方向(gain_db>=0)でheadroomが無い（既にceiling超過）場合は0dBに留める。
    「補正のつもりが符号が反転して余計にカットする」事態を避ける（カット方向は
    ceiling超過分だけさらに下げてよい）。
    """
    if true_peak is None:
        return gain_db
    headroom = ceiling - true_peak
    if gain_db >= 0:
        return max(0.0, min(gain_db, headroom))
    return min(gain_db, headroom)


def build_linear_correction_af(stats):
    """1回目の補正: loudnormのmeasured_*を使ったlinearモードのaf文字列を組み立てる（純粋関数）。"""
    return (
        "loudnorm=I=%.1f:TP=-1.5:LRA=11:measured_I=%s:measured_TP=%s:"
        "measured_LRA=%s:measured_thresh=%s:offset=%s:linear=true,aresample=48000,apad"
    ) % (LOUDNESS_TARGET_I, stats["input_i"], stats["input_tp"],
         stats["input_lra"], stats["input_thresh"], stats["target_offset"])


def build_volume_correction_af(gain_db):
    """2回目の補正: ebur128実測とのずれをvolume(dB)で直接当てるaf文字列を組み立てる（純粋関数）。"""
    return "volume=%.2fdB,aresample=48000,apad" % gain_db


def probe_video_frame_count(path):
    """実際に書き出された映像の総フレーム数を数える（回帰テストのtotal_frames突合にも使う）。"""
    cmd = ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
           "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return int(r.stdout.strip())


def probe_video_duration_seconds(path):
    """実際に書き出された映像フレーム数を、対応する秒数（フレーム境界）に変換する。
    音声の-tはこの値に合わせる（映像と音声の長さのずれを無くす）。
    """
    return probe_video_frame_count(path) / FPSF


def remux_audio(path, af, duration):
    """音声にafを適用して-c:v copyでremuxする（副作用。フレーム抽出後のQCと同様、
    映像は一切再エンコードしない）。音声が映像より短い場合、-tだけでは伸びない
    （-tは上限であり、無音で継ぎ足しはしない）ため、af側でapadしてから-tで
    正確な長さに揃える。
    """
    tmp_path = path + ".loudfix.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", path, "-af", af,
           "-c:v", "copy", "-t", "%.6f" % duration,
           "-c:a", "aac", "-b:a", "192k", tmp_path]
    subprocess.run(cmd, check=True)
    os.replace(tmp_path, path)


def fix_loudness(path):
    """音声の長さを映像に合わせ、ラウドネスを目標(-14 LUFS)に補正する。
    判定・検証はebur128実測に統一する（モジュール冒頭のコメント参照）。
      1回目: measure_loudnormのmeasured_*を使ったlinearモードのloudnormで補正
      2回目: それでもLOUDNESS_CORRECTION_THRESHOLDを超えていれば、ebur128実測との
             差分をvolume(dB)で直接当てる（True Peakがceilingを超えないようクランプ）
    最大LOUDNESS_MAX_CORRECTION_ATTEMPTS回で打ち切る（際限なく叩き直さない）。
    ずれが無ければ長さ合わせのみ（ラウドネス系のafは掛けない）。
    どの分岐（補正した/しなかった/打ち切った）を通っても、最後に必ずebur128で
    測り直し、目標範囲内かをloudness_result_okで確認する。範囲外なら黙って通さず
    stderrに警告を出す（戻り値のokでも呼び出し側が気づける）。
    戻り値: {"applied": 補正を1回以上適用したか, "ok": 最終状態が許容範囲内か,
             "problems": [理由, ...], "integrated": float, "true_peak": float|None}
    """
    duration = probe_video_duration_seconds(path)
    applied = False
    summary = measure_ebur128(path)
    attempt = 0
    while (needs_loudness_correction(summary["integrated"])
           and attempt < LOUDNESS_MAX_CORRECTION_ATTEMPTS):
        if attempt == 0:
            af = build_linear_correction_af(measure_loudnorm(path))
        else:
            gain = clamp_gain_for_true_peak(
                gain_to_target(summary["integrated"]), summary.get("true_peak"))
            if abs(gain) < MIN_MEANINGFUL_GAIN_DB:
                break  # True Peakの余裕が無く安全にはこれ以上補正できない。無駄な再エンコードもしない
            af = build_volume_correction_af(gain)
        remux_audio(path, af, duration)
        applied = True
        summary = measure_ebur128(path)
        attempt += 1

    if not applied:
        remux_audio(path, "aresample=48000,apad", duration)
        # 長さ合わせだけでも音声を再エンコードしているので、最終状態を測り直す
        summary = measure_ebur128(path)

    ok, problems = loudness_result_ok(summary["integrated"], summary.get("true_peak"))
    if not ok:
        for p in problems:
            print("WARNING: fix_loudness: %s: %s" % (path, p), file=sys.stderr)

    return {"applied": applied, "ok": ok, "problems": problems,
            "integrated": summary["integrated"], "true_peak": summary.get("true_peak")}


# --- CLIエントリポイント ---

def load_clips_plan(path):
    spec = importlib.util.spec_from_file_location("clips_plan", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("clips_plan")
    ap.add_argument("--out", default="out")
    ap.add_argument("--only", nargs="*", default=None,
                     help="このfile名のクリップだけ書き出す（省略時は全件）")
    return ap.parse_args(argv)


def base_image_path(index):
    """clips_plan中の位置(0始まり)から <workdir>/base/base_NN.png のパスを返す（1始まり連番）。"""
    return os.path.join("base", "base_%02d.png" % (index + 1))


def base_image_content_hash(clip):
    """base画像を決める見出し入力(l1/l2/quote)からキャッシュ判定用のハッシュを作る
    （純粋関数）。位置番号(base_image_path)だけでキャッシュを判定すると、同じ作業
    フォルダでクリップの並べ替えや文言の変更をしたときに古い見出しの画像を黙って
    使い回してしまう事故が起こりうる。
    """
    payload = json.dumps(
        {"l1": clip.get("l1"), "l2": clip.get("l2"), "quote": clip.get("quote")},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _base_image_hash_path(base_png):
    return base_png + ".hash"


def ensure_base_image(clip, base_png):
    """base_png が無い、または前回生成時から見出し内容(l1/l2/quote)が変わっていれば
    mkbase.build で(再)生成する。内容が同じ場合のみ再利用する。
    PIL依存はここでのみ必要になるよう遅延importする（純粋なコマンド組み立てのテストを汚さない）。
    """
    content_hash = base_image_content_hash(clip)
    hash_path = _base_image_hash_path(base_png)
    if os.path.exists(base_png) and os.path.exists(hash_path):
        with open(hash_path, encoding="utf-8") as f:
            cached_hash = f.read().strip()
        if cached_hash == content_hash:
            return
    import mkbase
    os.makedirs(os.path.dirname(base_png), exist_ok=True)
    mkbase.build(clip["l1"], clip["l2"], clip["quote"], base_png)
    with open(hash_path, "w", encoding="utf-8") as f:
        f.write(content_hash)


def render_clip(clip_index, clip, out_dir):
    """1本のクリップをフレームスナップ・render・timeline.json・ラウドネス補正まで通す。
    戻り値: (out_path, loudness_result)。loudness_resultはfix_loudness()の戻り値
    （{"applied","ok","problems","integrated","true_peak"}）をそのまま返す。
    """
    base_png = base_image_path(clip_index)
    ensure_base_image(clip, base_png)

    flash_after = clip.get("flash_after", 0)
    flash_dur_raw = clip.get("xfade", 0.35)
    flash_frames = round(flash_dur_raw * FPSF)
    flash_dur = frame_time(flash_frames)

    cut_frames = snap_cuts(clip["cuts"])
    n = len(cut_frames)
    has_flash = flash_after is not None and 0 <= flash_after < n - 1
    if has_flash:
        pre_flash_n_frames = cut_frames[flash_after][1] - cut_frames[flash_after][0]
        if pre_flash_n_frames <= flash_frames:
            raise ValueError(
                "フラッシュ直前のカット(%d番目、%dフレーム)がフラッシュ長(%dフレーム)以下です"
                % (flash_after, pre_flash_n_frames, flash_frames)
            )

    snapped_seconds = [(frame_time(a), frame_time(b)) for a, b in cut_frames]
    out_path = os.path.join(out_dir, clip["file"])
    render(snapped_seconds, base_png, out_path, flash_after=flash_after, flash_dur=flash_dur)

    cuts_info, total_frames = build_timeline(cut_frames, flash_after, flash_frames)
    write_timeline(out_path, cuts_info, total_frames, flash_after, flash_dur, flash_frames)
    loudness_result = fix_loudness(out_path)
    return out_path, loudness_result


def select_clips(clips, only):
    """--only指定があればそのfile名のクリップだけを、元のインデックス付きで返す。"""
    if only is None:
        return list(enumerate(clips))
    wanted = set(only)
    missing = wanted - {c["file"] for c in clips}
    if missing:
        raise ValueError("--only に指定されたファイルが clips_plan にありません: %s"
                          % "、".join(sorted(missing)))
    return [(i, c) for i, c in enumerate(clips) if c["file"] in wanted]


def main(argv):
    """終了コード: 0=成功, 1=検証エラー(引数・カット定義等), 2=レンダリング自体は
    成功したがラウドネスが未解決（loudness_result["ok"]がFalse）のクリップがある。
    """
    args = parse_args(argv)
    clips_plan_path = os.path.abspath(args.clips_plan)
    original_cwd = os.getcwd()
    os.chdir(args.workdir)
    try:
        M = load_clips_plan(clips_plan_path)
        try:
            selected = select_clips(M.CLIPS, args.only)
        except ValueError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 1

        os.makedirs(args.out, exist_ok=True)
        unresolved = []
        for clip_index, clip in selected:
            try:
                out_path, loudness_result = render_clip(clip_index, clip, args.out)
            except ValueError as exc:
                print("ERROR: %s: %s" % (clip.get("file", "?"), exc), file=sys.stderr)
                return 1
            if not loudness_result["ok"]:
                unresolved.append((clip.get("file", out_path), loudness_result["problems"]))

        if unresolved:
            for file_name, problems in unresolved:
                for p in problems:
                    print("WARNING: %s: ラウドネスが未解決です: %s" % (file_name, p),
                          file=sys.stderr)
            return 2
        return 0
    finally:
        os.chdir(original_cwd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
