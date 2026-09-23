# -*- coding: utf-8 -*-
"""テロップOCR走査まわりの共通ロジック（mitchaku-kirinuki/scripts/subscan.py から移設）。
帯の切替検出そのもの（compute_frame_diffs・find_changepoints・build_segments等）や
ffmpegでの画像抽出は各スキルのファイル形式・キャッシュ命名に依存するため各スキルに残す。
"""
import subprocess
from difflib import SequenceMatcher

MAX_BISECTION_PROBES = 12         # 二分探索1回あたりのOCR呼び出し上限（2^12=4096フレームまで対応）
TEXT_SIMILARITY_THRESHOLD = 0.6   # OCR読み揺れを許容した一致判定の閾値

BAND_Y_WARN_THRESHOLD_PX = 20
EMPTY_RATE_WARN_THRESHOLD = 0.5


# --- テロップ帯の位置推定・警告 ---

def clamp_search_window(y0, h, frame_height):
    """帯位置推定の探索窓 [y0, y0+h) が画像の高さをはみ出さないようにする。
    高さ側を縮めて収める（y0をずらすと、ffmpegのcropが暗黙にy0をずらす場合と
    区別がつかなくなり、best_band_startの戻り値を元座標に戻す計算が狂う。
    Codexレビューで検出：既定値(band_y=860, margin=150)の窓が1080px高の
    素材で下端を140pxはみ出し、ffmpeg側で無断にy0がずれていた）。
    """
    y0 = max(0, y0)
    h = max(0, min(h, frame_height - y0))
    return y0, h


def edge_row_profile(rows):
    """各行（0-255の画素値配列）について横方向の隣接差分の合計をエネルギーとして返す。
    テロップ文字がある帯は背景より横方向のエッジが多いため、この値が高くなる。
    """
    return [sum(abs(row[i] - row[i - 1]) for i in range(1, len(row))) for row in rows]


def best_band_start(profile, band_h):
    """profile中でband_h幅の合計エネルギーが最大になる開始インデックスを返す。"""
    if len(profile) <= band_h:
        return 0
    window = sum(profile[:band_h])
    best_start, best_sum = 0, window
    for start in range(1, len(profile) - band_h + 1):
        window += profile[start + band_h - 1] - profile[start - 1]
        if window > best_sum:
            best_sum, best_start = window, start
    return best_start


def band_offset_warning(estimated_y, configured_y, threshold=BAND_Y_WARN_THRESHOLD_PX):
    """推定帯位置が設定値とthreshold px以上ずれていたら警告文を返す（無ければNone）。"""
    diff = abs(estimated_y - configured_y)
    if diff > threshold:
        return ("警告: テロップ帯の推定位置(y=%d)が設定値(y=%d)と%dpxずれています"
                "（BAND_Yの調整を検討してください）" % (estimated_y, configured_y, diff))
    return None


def empty_rate(texts):
    """OCR結果のうち空文字列の割合を返す。"""
    if not texts:
        return 0.0
    return sum(1 for t in texts if not t) / len(texts)


def empty_rate_warning(rate, threshold=EMPTY_RATE_WARN_THRESHOLD):
    if rate > threshold:
        return ("警告: OCR結果の空行率が%.0f%%です"
                "（帯位置やしきい値、フェードインの取り逃しを確認してください）" % (rate * 100))
    return None


# --- フェードイン対策の二分探索（純粋アルゴリズム。OCR呼び出しは注入する） ---

def bisect_first_true(check_fn, lo, hi, max_probes=MAX_BISECTION_PROBES):
    """[lo, hi) の範囲で check_fn(frame) が最初にTrueになるフレームを二分探索で返す。
    check_fn は lo側False→hi側Trueの単調性を前提にする。プローブ回数に上限を設け、
    超えたらその時点のhiを返す（OCR呼び出しが爆発しないようにする）。
    """
    probes = 0
    while lo < hi and probes < max_probes:
        mid = (lo + hi) // 2
        if check_fn(mid):
            hi = mid
        else:
            lo = mid + 1
        probes += 1
    return hi


def texts_similar(a, b, threshold=TEXT_SIMILARITY_THRESHOLD):
    """OCRの読み揺れを許容したテキスト一致判定。"""
    if a == b:
        return True
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def refine_segment_start(ocr_text_at, seg_start, seg_mid, center_text, max_probes=MAX_BISECTION_PROBES):
    """区間先頭付近のOCRが中央のOCRと食い違う場合（フェードイン等で先頭がまだ
    表示しきっていない）、二分探索で「中央と同じテキストになる最初のフレーム」を探す。
    ocr_text_at(frame)->str は呼び出し側（実際はOCR実行）から注入する。
    先頭と中央が一致していれば探索せずseg_startをそのまま返す（OCR呼び出しを増やさない）。
    """
    if texts_similar(ocr_text_at(seg_start), center_text):
        return seg_start
    return bisect_first_true(
        lambda f: texts_similar(ocr_text_at(f), center_text), seg_start, seg_mid, max_probes)


# --- OCRプロセスの実行・結果照合（impure） ---

def run_ocr(ocrcli, paths):
    """({path: text}, OCRプロセス自体が失敗したか) を返す（qc_cuts.py/scan_qc.pyと
    同じ規約）。プロセスが失敗した場合、その回の対象パスは結果に含めない
    （「空文字列を正しく読んだ」ことと区別するため、呼び出し側でNG扱いにする）。
    """
    if not paths:
        return {}, False
    r = subprocess.run([ocrcli] + paths, capture_output=True, text=True)
    if r.returncode != 0:
        return {}, True
    result = {}
    for line in r.stdout.split("\n"):
        if "\t" not in line:
            continue
        p, t = line.split("\t", 1)
        result[p] = t.strip()
    return result, False


def missing_ocr_results(paths, ocr):
    """OCRプロセス自体は成功(returncode=0)しても、要求した画像の一部だけ結果行が
    無いことがある。ocr辞書にkeyが無いpathを欠落として返す（空文字列の結果は
    「正しく空文字列を読んだ」であり欠落ではないので区別する。Codexレビューで検出）。
    """
    return [p for p in paths if p not in ocr]
