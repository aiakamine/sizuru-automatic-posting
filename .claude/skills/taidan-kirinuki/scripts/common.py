# -*- coding: utf-8 -*-
"""共通処理：作業ディレクトリ、fps、カット定義の読み込み、テロップ境界の読み込み

第3段階での変更点:
  ・固定値だったFPSF/FPSをやめ、bounds.json（subscan.pyが書く）のfpsを使う
    （29.97fps固定だと30fps・25fps素材でカットがずれるため）
  ・カット区間の計算 (f0-0.5)/fps・尻のgstartスナップが render.py/qc.py/chop.py/
    pauses.py/gen_docs.py の5か所に重複していたので cut_range に一本化する
  ・chop.pyのdb()相当（窓ごとの実効レベル計算）をload_pcm/window_dbとして共通化する。
    cutpoints.pyが行ごとにffmpegを呼ばず全編を1回だけ読むため
"""
import array
import importlib.util
import json
import math
import os
import struct
import subprocess
import sys

# bounds.jsonにfps_strが無い古い作業フォルダ（第3段階以前）はこれとみなす
DEFAULT_FPS_STR = "30000/1001"


def _parse_fps_str(fps_str):
    if "/" in fps_str:
        num, den = fps_str.split("/", 1)
        return float(num) / float(den)
    return float(fps_str)


def load_clips(path):
    spec = importlib.util.spec_from_file_location("clipsdef", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_bounds(workdir):
    """subscan.py が書き出したテロップ切り替わりフレームとfpsを読む。
    bounds.jsonが無ければ空のboundとfps=29.97扱いにする（テロップ表がまだ無い段階の
    呼び出しに備える。既存の挙動を維持）。
    """
    p = os.path.join(workdir, "bounds.json")
    if not os.path.exists(p):
        return {"bound": [], "gstart": {}, "fps": _parse_fps_str(DEFAULT_FPS_STR),
                "fps_str": DEFAULT_FPS_STR}
    d = json.load(open(p))
    fps_str = d.get("fps_str")
    if fps_str is None:
        print("警告: bounds.jsonにfps_strが無いため%sとみなします"
              "（subscan.pyを第3段階版で実行し直すことを推奨）" % DEFAULT_FPS_STR, file=sys.stderr)
        fps_str = DEFAULT_FPS_STR
    fps = d.get("fps")
    if fps is None:
        fps = _parse_fps_str(fps_str)
    return {"bound": d["bound"], "gstart": {int(k): v for k, v in d["gstart"].items()},
            "fps": fps, "fps_str": fps_str}


def frames(t, fps):
    return int(round(t * fps))


def snap_tail_time(frame, bounds):
    """frameを「次のテロップへの切替が完了したフレーム」とみなし、gstartスナップ後
    （＝次のテロップに変わり始める1つ前のフレーム）の表示時刻を返す。
    render.py が実際にカットを切る尻の時刻と同じ規則（cut_rangeのEと同じ計算）。
    cutpoints.py はclips.pyのカットではなくtelops.txtの各行の終点フレームにこれを使う。
    """
    snapped = bounds["gstart"].get(frame, frame)
    return (snapped - 0.5) / bounds["fps"]


def cut_range(cut, bounds):
    """カット定義(cut["src"]=(開始秒,終了秒))から、render.pyが実際に切る素材上の
    (開始時刻, 終了時刻)を返す。頭はテロップの最初のフレーム、尻は次のテロップに
    変わり始める1つ前のフレーム（gstartスナップ）に揃える。
    render.py/qc.py/chop.py/pauses.py/gen_docs.py の5か所に重複していた計算の一本化。
    """
    s, e = cut["src"]
    f0 = frames(s, bounds["fps"])
    f1 = frames(e, bounds["fps"])
    return (f0 - 0.5) / bounds["fps"], snap_tail_time(f1, bounds)


# 隣接カットの尻と頭が実質同じ時刻（＝地続き、実際には切っていない）かどうかの許容誤差。
# chop.py（ぶつ切り判定）とalign_check.py（発話とテロップの一致検査）が共通で使う：
# 地続きの境界は実際にはカットしていないので、無音・発話境界の検査対象から除外する
CONTIGUOUS_JOIN_TOLERANCE_SEC = 0.02


def is_contiguous_join(a_end, b_start, tolerance=CONTIGUOUS_JOIN_TOLERANCE_SEC):
    """前のカットの尻(a_end)と次のカットの頭(b_start)が地続きかを返す。"""
    return abs(a_end - b_start) < tolerance


def run(cmd, quiet=False):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        if not quiet:
            print(" ".join(cmd[:14]), "...", file=sys.stderr)
            print(p.stderr[-2500:], file=sys.stderr)
        raise SystemExit(1)
    return p


def pcm(src, t0, dur, rate=16000):
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % t0, "-t", "%.3f" % dur,
                        "-i", src, "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                       capture_output=True)
    d = p.stdout
    n = len(d) // 2
    return struct.unpack("<%dh" % n, d[:n * 2]) if n else ()


def _probe_start_time(args, src):
    r = subprocess.run(
        ["ffprobe", "-v", "error"] + args
        + ["-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True)
    out = r.stdout.strip()
    if not out or out == "N/A":
        return 0.0
    try:
        return float(out)
    except ValueError:
        return 0.0


def get_audio_start_time(src):
    """音声ストリームが、コンテナ全体（フォーマット）の基準時刻に対して何秒遅れて
    始まるかを返す。edit list等で音声トラックが映像より遅れて始まる素材があり、
    そのままPCMを頭から詰めて読むと窓の絶対時刻とサンプル配列のインデックスがずれる。

    音声ストリーム単体のstart_timeをそのまま使わないのは、全ストリームが同じだけ
    後ろにずれている（コンテナ全体の基準時刻自体が0でない）素材では、音声に実際の
    遅れが無いのに大きな無音パディングを入れてしまうため（Codexレビューで検出：
    フォーマットのstart_timeを差し引かず絶対値のまま使っていた回帰）。
    フォーマット・ストリームどちらかの値が取れない場合は0.0とみなす（大半の素材はこれ）。
    """
    stream_start = _probe_start_time(["-select_streams", "a:0", "-show_entries", "stream=start_time"], src)
    format_start = _probe_start_time(["-show_entries", "format=start_time"], src)
    return max(0.0, stream_start - format_start)


def load_pcm(src, rate=16000):
    """srcの全編を16kHzモノラルPCM(int16)として1回だけ読み込む。
    chop.py/cutpoints.pyが境界ごとにffmpegを呼ばず、窓ごとの実効レベルをこの配列から
    計算するために使う（本編30分超で行ごとにffmpegを呼ぶと遅すぎるため）。

    音声ストリームがコンテナの絶対時間より遅れて始まる素材（edit list等）では、
    デコード結果の先頭が実際には時刻0ではなくstart_time秒から始まっている。
    window_dbは配列インデックスをコンテナの絶対時刻としてそのまま使うため、
    その分だけ無音（0）でパディングして絶対時刻とインデックスを合わせる
    （Codexレビューで検出：パディング無しだと音声開始前の時刻が「無音」ではなく
    誤って音声ありと判定され、逆に終端付近が範囲外で「無音」に誤判定されていた）。

    ffmpegが失敗した場合（srcが無い・音声ストリームが無い等）は例外を送出する。
    黙って空/部分的なPCMを返すと、window_db/has_silence_gapが「無音」と区別できず、
    音声を全く解析できていないのに「谷あり（無音）」と誤って報告してしまう
    （Codexレビューで検出：cutpoints.pyがデコード失敗を無音と混同していた）。
    """
    start = get_audio_start_time(src)
    # get_audio_start_time は音声ストリーム a:0 のstart_timeを見るため、デコード側も
    # 明示的に同じストリームをmapする。-mapを付けないとffmpegが（複数音声トラックが
    # ある素材で）別のストリームを自動選択することがあり、パディング量と実際に
    # デコードした音声のずれが生じる（Codexレビューで検出）。
    # aresample=async=1は、ストリーム内部でPTSが不連続（音声パケットの欠落等）な
    # 区間があった場合に無音で埋めて連続なタイムラインへ補正する（先頭のずれは
    # このフィルタだけでは補正されないため、上のget_audio_start_time側で別途扱う）。
    # min_hard_compは既定0.1秒で、それ未満の短いギャップは補正されずに読み飛ばされる
    # （Codexレビューで検出：64ms程度の短い欠落を補正し損ねる）ため、小さい値に下げて
    # 短いギャップも即座に無音でパディングされるようにする。
    # 既知の限界：内部の不連続がどこまで正しく補正されるかは実素材で未検証。
    # ダウンロードしたYouTube動画・画面録画では通常発生しない前提だが、発生した場合は
    # 本関数のインデックスベースの窓計算より、境界ごとに直接シークする方式
    # （旧chop.pyのdb()）の方が確実（次回実制作時の改善フェーズで確認する）。
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-map", "0:a:0",
                        "-af", "aresample=async=1:min_hard_comp=0.001", "-vn",
                        "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("音声の読み込みに失敗しました(ffmpeg exit=%d): %s\n%s"
                            % (p.returncode, src, p.stderr.decode("utf-8", "replace")[-1000:]))
    d = p.stdout
    n = len(d) // 2
    # array('h')でint16のまま詰めて持つ（Codexレビューで検出：Pythonのintのタプルだと
    # オブジェクトのオーバーヘッドで30分の音声が約1GBになる。array('h')なら1サンプル
    # 2バイトのまま持てる）。frombytesはネイティブのバイト順を使うため、
    # リトルエンディアンでない環境向けにbyteswapで補正する（このリポジトリはmacOS専用
    # で実質常にリトルエンディアンだが、想定外の環境でも値が壊れないようにする）。
    samples = array.array("h")
    samples.frombytes(d[:n * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    pad = int(round(start * rate))
    if pad > 0:
        samples = array.array("h", [0]) * pad + samples
    return samples


def window_db(samples, rate, t0, dur):
    """samplesのうち[t0, t0+dur)秒の実効レベル(dBFS)を返す。範囲外・無音は-99.0
    （0除算・音量無限小を避けるための下限値。chop.pyの既存挙動と同じ）。
    """
    i0 = max(0, int(t0 * rate))
    i1 = min(len(samples), int((t0 + dur) * rate))
    n = i1 - i0
    if n <= 0:
        return -99.0
    r = math.sqrt(sum(x * x for x in samples[i0:i1]) / n)
    return 20 * math.log10(r / 32768.0) if r > 0 else -99.0


# カット点直前/直後の無音判定に使う窓とレベル（chop.pyの★ぶつ切り判定と同じ規約）。
# 境界そのものではなく10ms離した60ms窓を見るのは、境界直後の立ち上がりで
# 誤って「発話あり」と判定しないため（chop.pyの既存の値をそのまま踏襲）
SILENCE_CHECK_GAP_SEC = 0.01
SILENCE_CHECK_WINDOW_SEC = 0.06
SILENCE_CHECK_THRESHOLD_DB = -26.0


def silence_levels(samples, rate, t):
    """時刻tの直前60ms・直後60ms（境界から10msの余裕を空けた窓）の実効レベル(dBFS)を
    (直前, 直後)のタプルで返す。has_silence_gapと、cutpoints.pyの候補表表示
    （実測したdB値をそのまま見せる。谷の実際の秒数は測っていないので断定しない）
    で共有する。
    """
    before = window_db(samples, rate, t - SILENCE_CHECK_GAP_SEC - SILENCE_CHECK_WINDOW_SEC,
                        SILENCE_CHECK_WINDOW_SEC)
    after = window_db(samples, rate, t + SILENCE_CHECK_GAP_SEC, SILENCE_CHECK_WINDOW_SEC)
    return before, after


def has_silence_gap(samples, rate, t, threshold=SILENCE_CHECK_THRESHOLD_DB):
    """時刻tの直前60ms・直後60ms（境界から10msの余裕を空けた窓）の実効レベルを見て、
    どちらか一方でも閾値dBFS以下（無音寄り）ならTrue（＝無音の谷がある）を返す。
    両方とも閾値を超えていれば谷が無い＝ぶつ切りの疑いとしてFalseを返す
    （chop.pyの★ぶつ切り判定と同じ窓・同じ閾値。対談はBGMが-30dB前後あり、
    -30dBの無音検出では谷がほとんど取れないためこの絶対レベル判定を使う）。
    """
    before, after = silence_levels(samples, rate, t)
    return not (before > threshold and after > threshold)


# align_check.pyのis_silent_at専用（chop.pyのぶつ切り判定=has_silence_gapとは別物）。
# has_silence_gapは「前後どちらかが静かならOK」という緩い判定で、カット点そのものに
# 発話がかかっている場合（例：発話が0.99秒から始まりカット点が1.00秒）でも
# 前側の窓だけを見て無音と誤判定してしまう（team-leadの実測、2026-09-17）。
# is_silent_pointはカット点を中心にした狭い窓自体のレベルだけを見る、より厳密な判定
IS_SILENT_POINT_HALF_WINDOW_SEC = 0.015


def is_silent_point(samples, rate, t, half_window=IS_SILENT_POINT_HALF_WINDOW_SEC,
                     threshold=SILENCE_CHECK_THRESHOLD_DB):
    """時刻tを中心にした±half_window秒の窓の実効レベルが閾値dBFS以下（無音）かを返す。"""
    return window_db(samples, rate, t - half_window, half_window * 2) <= threshold


def silences(src, t0, t1, ratio=0.55, minlen=0.10, rate=16000, step_ms=10):
    """[t0,t1] のうち「局所中央値の ratio 倍を下回る状態が minlen 秒以上続く」区間を返す"""
    v = pcm(src, t0, t1 - t0, rate)
    step = int(rate * step_ms / 1000.0)
    if len(v) < step * 3:
        return []
    pr = [(t0 + i / float(rate), math.sqrt(sum(x * x for x in v[i:i + step]) / step))
          for i in range(0, len(v) - step, step)]
    med = sorted(x[1] for x in pr)[len(pr) // 2]
    out, cur = [], None
    for t, x in pr:
        if x < med * ratio:
            cur = [t, t] if cur is None else [cur[0], t]
        else:
            if cur and cur[1] - cur[0] >= minlen:
                out.append(tuple(cur))
            cur = None
    if cur and cur[1] - cur[0] >= minlen:
        out.append(tuple(cur))
    return out
