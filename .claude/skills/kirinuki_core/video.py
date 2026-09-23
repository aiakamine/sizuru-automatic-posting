# -*- coding: utf-8 -*-
"""fps・フレーム座標まわりの共通ロジック（mitchaku-kirinuki/scripts/subscan.py から移設）。
第3段階の対談展開で以下を新設する（TDDでRedを先に書いてから実装）:
  - get_stream_rates: ffprobeでr_frame_rateとavg_frame_rateの両方を取得する
  - is_variable_frame_rate: 上記2つが食い違う（＝可変フレームレート）かを判定する純粋関数
  - frame_index: 時刻→フレーム番号の変換（frame_seek_timeの逆変換に相当）
"""
import subprocess


def parse_frame_rate(rate_str):
    """ffprobeのr_frame_rate文字列（例 "30000/1001" や "30/1"）をfloatに変換する。"""
    rate_str = rate_str.strip()
    if "/" in rate_str:
        num, den = rate_str.split("/", 1)
        return float(num) / float(den)
    return float(rate_str)


def get_frame_height(src):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=height",
         "-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True, check=True,
    )
    return int(r.stdout.strip())


def get_fps(src):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True, check=True,
    )
    return parse_frame_rate(r.stdout)


def get_stream_rates(src):
    """r_frame_rate（コンテナのタイムベース）とavg_frame_rate（実測平均）を
    文字列のまま両方返す（"30000/1001"等）。可変フレームレート判定は
    is_variable_frame_rate に渡す前提で、ここではfloat変換しない。
    """
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,avg_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True, check=True,
    )
    lines = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    r_rate, avg_rate = lines[0], lines[1]
    return r_rate, avg_rate


def is_variable_frame_rate(r_rate_str, avg_rate_str, tol=0.01):
    """r_frame_rateとavg_frame_rateがtol秒/フレームを超えて食い違うかを返す。
    可変フレームレート素材はコンテナのタイムベース(r_frame_rate)と実測平均
    (avg_frame_rate)が一致しないため、これで検出する。
    """
    return abs(parse_frame_rate(r_rate_str) - parse_frame_rate(avg_rate_str)) > tol


def frame_seek_time(frame, fps):
    """指定フレームの中央時刻を返す。フレーム境界ちょうどの時刻でシークすると、
    ffmpegの丸め方（コンテナ・デコーダ依存）次第で前後のフレームを拾うことがある
    （実測：30fpsでframe=2の境界0.067秒シークがframe 3を返した。Codexレビューで検出）。
    フレーム内の中央時刻なら境界の丸め誤差から半フレーム分の余裕を持てる。
    """
    return (frame + 0.5) / fps


def frame_index(t, fps):
    """時刻tが属するフレーム番号を返す（frame_seek_timeの逆変換に相当）。"""
    return int(round(t * fps))
