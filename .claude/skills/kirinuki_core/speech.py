# -*- coding: utf-8 -*-
"""発話とテロップの一致判定まわりの共通ロジック
（mitchaku-kirinuki/scripts/align_check.py から移設）。
clips_plan.py/telops.txtの読み込み・カット単位の判定オーケストレーション（check_cut）は
スキルごとに書式・カット識別子の付け方が違うため、各スキルのalign_check.pyに残す。
"""
import json
import os
import shutil
import subprocess
from difflib import SequenceMatcher

DEFAULT_RADIUS_SEC = 1.5
DEFAULT_MODEL = "small"
FALLBACK_WHISPER_BIN = os.path.expanduser("~/Library/Python/3.9/bin/whisper")

# 頭の一致判定：Whisperの表記揺れ（漢字/かな等）を許容するための閾値
HEAD_MATCH_MIN_CHARS = 2
HEAD_MATCH_RATIO_THRESHOLD = 0.6
# 比較窓を広く取ると、先頭の語が丸ごと欠けていてもSequenceMatcherが後半の一致で
# 帳尻を合わせてしまい、頭が揃っていない発話を誤ってOKにする（Codexレビューで検出）。
# 「結果が出てるのに評価されてない人とかも」→実際「出てるのに評価されてない人とかも」
# （learnings.md 2026-09-15）は20文字窓だと類似度0.81でOK扱いになってしまうが、
# 6文字窓なら0.5でNGに倒れる。表記揺れの許容と頭語の欠落検出を両立する値として選ぶ。
HEAD_COMPARE_MAX_CHARS = 6

# Whisperの語の時刻には数十ms単位のジッタがあるため、カット点が語にごく僅かに
# かかっただけ（この秒数未満）では頭切れ・尻切れとして扱わない
WORD_OVERLAP_MIN_SEC = 0.05

# 終点後この秒数未満で次の語が始まっていたら「文中切りの疑い」として警告する
TRAILING_GAP_WARN_SEC = 0.3


class WhisperNotFoundError(RuntimeError):
    """whisper CLI がPATHにも既定パスにも見つからないことを表す。"""


# --- 発話とテロップの比較用正規化（句読点・空白・「／」改行マーカーを除く） ---

_STRIP_CHARS = "、。！？!?「」『』・…,.\"'　 \t\r\n"


def normalize_speech(text):
    """比較用に正規化する：空白・句読点・「／」（テロップの改行マーカー）を除く。"""
    text = text.replace("／", "")
    return "".join(ch for ch in text if ch not in _STRIP_CHARS)


# --- Whisper CLI の所在 ---

def find_whisper_cli(explicit=None):
    """whisper CLI のパスを返す。PATH → 既定のフォールバックパスの順で探す。"""
    if explicit:
        return explicit
    found = shutil.which("whisper")
    if found:
        return found
    if os.path.exists(FALLBACK_WHISPER_BIN):
        return FALLBACK_WHISPER_BIN
    raise WhisperNotFoundError(
        "whisper CLIが見つかりません。'pip install openai-whisper' でインストールするか、"
        "--whisper でパスを指定してください（既定のフォールバック: %s）" % FALLBACK_WHISPER_BIN
    )


# --- 音声切り出し・Whisper実行（結果は align_cache/ にキャッシュする） ---

def extract_audio_segment(src, center_time, radius, out_wav):
    """src の center_time±radius 秒を16kHzモノラルwavに切り出す。
    開始時刻が負になる場合は0にクランプする。実際の切り出し開始時刻（秒）を返す
    （呼び出し側がWhisperの語の時刻をこの値だけ補正して絶対時刻に直すため）。

    -mapで音声トラックa:0を明示する。付けないとffmpegが（複数音声トラックがある
    素材で）別のトラックを自動選択することがあり、common.load_pcm/get_audio_start_time
    が見ているトラックと食い違ってしまう（Codexレビューで検出）。
    """
    start = max(0.0, center_time - radius)
    duration = radius * 2
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", "%.3f" % start, "-t", "%.3f" % duration,
         "-i", src, "-map", "0:a:0", "-ar", "16000", "-ac", "1", out_wav],
        check=True,
    )
    return start


def run_whisper(wav_path, cache_dir, model=DEFAULT_MODEL, whisper_bin=None):
    """wav_path をWhisper CLIで解析し、出力json（--output_format json）のパスを返す。
    同名のjsonが既にcache_dirにあれば再実行しない。
    """
    os.makedirs(cache_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(wav_path))[0]
    json_path = os.path.join(cache_dir, base + ".json")
    if os.path.exists(json_path):
        return json_path
    whisper_bin = find_whisper_cli(whisper_bin)
    subprocess.run(
        [whisper_bin, wav_path, "--word_timestamps", "True", "--output_format", "json",
         "--language", "ja", "--model", model, "--output_dir", cache_dir],
        check=True, capture_output=True, text=True,
    )
    if not os.path.exists(json_path):
        raise RuntimeError("Whisperの出力が見つかりません: %s" % json_path)
    return json_path


def parse_whisper_words(whisper_json):
    """whisper CLIのjson（--word_timestamps True）から
    [{"start","end","word"}, ...] を開始時刻昇順で返す。
    """
    words = []
    for seg in whisper_json.get("segments", []):
        for w in seg.get("words", []):
            words.append({"start": w["start"], "end": w["end"], "word": w["word"]})
    words.sort(key=lambda w: w["start"])
    return words


def _cache_meta_path(wav_path):
    return wav_path + ".meta.json"


def _read_cache_meta(meta_path):
    """meta.jsonを読む。存在しない・壊れている（JSONとして読めない）・中身がdictで
    ないときはNone（＝無効）を返す。他の無効条件（src/radius/model不一致）と同じく、
    黙って再生成させるため（Codexレビューで検出：以前はjson.load()がJSONDecodeError
    を送出してload_words_for_boundaryごと落ちていた）。
    """
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def _cache_is_valid(meta, src, radius, model):
    """キャッシュされたwav/Whisper結果が、今回の呼び出しと同じ条件で作られたかを見る。
    src・radius・modelのいずれかが変わっていたら無効（Codexレビューで検出：
    以前はwav_nameだけで再利用していたため、radiusを変えると古いclip_startのまま
    絶対時刻がずれ、modelを変えても古いWhisper結果を使い回していた）。
    srcは絶対パスの一致に加えmtimeも見る。同じパスのsrc.mp4を再ダウンロード・
    再書き出ししても気づけるようにするため（内容全体をハッシュするのは高コストなので、
    安価なmtime比較に留める。これでも大半の「同じパスの入れ替え」事故は検知できる）。
    """
    if meta is None:
        return False
    try:
        src_mtime = os.path.getmtime(src)
    except OSError:
        src_mtime = None
    return (meta.get("src") == os.path.abspath(src)
            and meta.get("src_mtime") == src_mtime
            and meta.get("radius") == radius
            and meta.get("model") == model)


def load_words_for_boundary(src, boundary_time, radius, cache_dir, wav_name,
                             model=DEFAULT_MODEL, whisper_bin=None,
                             extract_fn=None, run_whisper_fn=None):
    """境界時刻±radius秒の語を、絶対時刻（src.mp4上の秒）で返す。
    src・radius・modelが前回と同じ場合に限り、wav・Whisper結果ともcache_dir配下の
    キャッシュを再利用する（実際の抽出開始時刻もメタデータに保存し、再計算しない）。

    extract_fn/run_whisper_fn: 既定はこのモジュールの extract_audio_segment/run_whisper。
    各スキルのalign_check.pyが「自分の名前空間の（テストで差し替え可能な）
    extract_audio_segment/run_whisper」を明示的に渡せるようにするための注入口
    （mitchaku-kirinuki/tests/test_align_check.py が `A.extract_audio_segment` を
    monkeypatchしてload_words_for_boundaryの挙動を検証しているため。ロジックは
    移設前と同一で、呼び出し方だけをkirinuki_core移設に合わせて調整した）。
    """
    extract_fn = extract_fn or extract_audio_segment
    run_whisper_fn = run_whisper_fn or run_whisper

    os.makedirs(cache_dir, exist_ok=True)
    wav_path = os.path.join(cache_dir, wav_name + ".wav")
    json_path = os.path.join(cache_dir, wav_name + ".json")
    meta_path = _cache_meta_path(wav_path)
    meta = _read_cache_meta(meta_path)

    if _cache_is_valid(meta, src, radius, model) and os.path.exists(wav_path) and os.path.exists(json_path):
        clip_start = meta["clip_start"]
    else:
        clip_start = extract_fn(src, boundary_time, radius, wav_path)
        if os.path.exists(json_path):
            os.remove(json_path)  # 古いmodelでのWhisper結果を使い回さないよう明示的に捨てる
        run_whisper_fn(wav_path, cache_dir, model=model, whisper_bin=whisper_bin)
        try:
            src_mtime = os.path.getmtime(src)
        except OSError:
            src_mtime = None
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"src": os.path.abspath(src), "src_mtime": src_mtime,
                       "radius": radius, "model": model, "clip_start": clip_start}, f)

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    words = parse_whisper_words(data)
    for w in words:
        w["start"] += clip_start
        w["end"] += clip_start
    return words


# --- 判定ロジック（純粋関数。Whisper呼び出しから独立してテストする） ---

def head_word_overlaps_cut(words, cut_start, min_overlap=WORD_OVERLAP_MIN_SEC):
    """cut_start より前に始まりカット内まで続く語（頭切れ）があれば (True, その語) を返す。
    語の頭が min_overlap 秒未満しか削れていない場合はASRの時刻ジッタとみなし無視する。
    """
    for w in words:
        if w["start"] < cut_start < w["end"] and (cut_start - w["start"]) >= min_overlap:
            return True, w
    return False, None


def tail_word_overlaps_cut(words, cut_end, min_overlap=WORD_OVERLAP_MIN_SEC):
    """cut_end をまたいで続く語（尻切れ）があれば (True, その語) を返す。
    語の尻が min_overlap 秒未満しか削れていない場合はASRの時刻ジッタとみなし無視する。
    """
    for w in words:
        if w["start"] < cut_end < w["end"] and (w["end"] - cut_end) >= min_overlap:
            return True, w
    return False, None


def head_actual_text(words, cut_start, min_overlap=WORD_OVERLAP_MIN_SEC):
    """cut_start以降に発話された語を時刻順に連結して返す。
    語の頭がASRジッタの範囲内（min_overlap未満）しか削れていない場合はその語自体も含める。
    頭切れとして許容している語を本文比較から除外すると、実際にはある語が欠けて見え、
    本来一致するはずの発話が誤ってNGになるため（Codexレビューで検出）。
    """
    return "".join(
        w["word"] for w in words
        if w["start"] >= cut_start or (cut_start - w["start"]) < min_overlap
    )


def head_actual_text_including_overlap(words, cut_start):
    """cut_startをまたぐ語も含めて、cut_start以降の語を時刻順に連結して返す
    （head_actual_textより広い範囲：w["end"] > cut_start の語を全部含める）。

    無音区間ではWhisperの単語時刻が最大0.75秒ほどずれることが実測されている
    （対談の合成素材、2026-09-15〜17）。is_silent_atで「実際は無音」と分かっている
    カット点では、語のまたぎをジッタ扱いにする代わりに、またぐ語自体を本文比較に
    含めることで、テロップが語を補っているNGクリップ（実際の発話がテロップの
    途中から始まる）を正しくNGのまま検出できるようにする。
    """
    return "".join(w["word"] for w in words if w["end"] > cut_start)


def match_head(expected_norm, actual_norm):
    """正規化済みの期待テロップ冒頭と実際の発話冒頭を比較する。(合格か, 理由) を返す。
    表記揺れ（漢字/かな等）を許容するため、頭を揃えた同じ長さの部分文字列同士を
    完全一致 or 類似度で比較する（極端に短い実際の発話は比較不能としてNG）。
    比較窓を狭めるだけでは、SequenceMatcherが位置を問わない一致を見つけて
    先頭の語が丸ごと欠けていても高い類似度を返してしまうことがある
    （例:「私は営業しています」vs「営業しています」は6文字窓でも類似度0.67で
    通ってしまう。Codexレビューで検出）。そのため先頭の1文字が一致しない場合は
    語そのものが欠けている可能性が高いとみなし、類似度を計算するまでもなくNGにする。
    表記のブレ（同じ語の漢字/かな）は通常2文字目以降で吸収されるため、この判定でも
    正当な表記揺れは引き続き許容できる。
    """
    if not expected_norm:
        return True, "OK（この時刻にテロップなし）"
    if len(actual_norm) < HEAD_MATCH_MIN_CHARS:
        return False, "NG（発話が短すぎて比較不能: 実際=%r）" % actual_norm
    k = min(len(expected_norm), len(actual_norm), HEAD_COMPARE_MAX_CHARS)
    exp_head, act_head = expected_norm[:k], actual_norm[:k]
    if exp_head == act_head:
        return True, "OK（完全一致）"
    if exp_head[0] != act_head[0]:
        return False, "NG（先頭の文字が一致しない: 期待冒頭=%r 実際冒頭=%r。語が欠けている疑い）" % (
            exp_head, act_head)
    ratio = SequenceMatcher(None, exp_head, act_head).ratio()
    if ratio >= HEAD_MATCH_RATIO_THRESHOLD:
        return True, "OK（類似度%.2f）" % ratio
    return False, "NG（期待冒頭=%r 実際冒頭=%r 類似度%.2f）" % (exp_head, act_head, ratio)


def check_head(words, cut_start, expected_text, is_silent_at=None):
    """頭の判定。頭切れ（語のまたぎ）を最優先でNGにし、次に本文の一致を見る。
    expected_text が None（telops.txtにこの時刻を含む行が無い）の場合は、
    「無テロップだからOK」と混同せずNGにする（Codexレビューで検出）。

    is_silent_at: 時刻(float)→そこが実際に無音かを返す関数（省略時はNone＝従来どおり）。
    無音と分かっている時刻では、Whisperの単語時刻が実測で最大0.75秒ほどずれることがあり
    （対談の合成素材、2026-09-17）、WORD_OVERLAP_MIN_SEC=0.05では吸収できず頭切れの
    誤NGになる。is_silent_at(cut_start)がTrueなら頭切れ判定そのものをスキップし、
    本文比較もまたぐ語を含めたhead_actual_text_including_overlapで行う（またぐ語を
    含めないと、テロップが語を補っているクリップが誤ってOKになってしまうため）。
    """
    silent = is_silent_at is not None and is_silent_at(cut_start)
    if not silent:
        overlaps, w = head_word_overlaps_cut(words, cut_start)
        if overlaps:
            return False, "NG（語の頭切れ: %r %.2f-%.2fs がカット開始をまたぐ）" % (
                w["word"], w["start"], w["end"])
    if expected_text is None:
        return False, "NG（telops.txtにこの時刻を含む行が無い。telops.txtの生成を確認してください）"
    expected_norm = normalize_speech(expected_text)
    if silent:
        actual_norm = normalize_speech(head_actual_text_including_overlap(words, cut_start))
    else:
        actual_norm = normalize_speech(head_actual_text(words, cut_start))
    ok, msg = match_head(expected_norm, actual_norm)
    if silent:
        msg += "（無音のためWhisperの時刻ずれとして扱った）"
    return ok, msg


def check_tail(words, cut_end, is_silent_at=None):
    """尻の判定：カット終点をまたいで続く語が無いことだけを見る（本文一致は問わない）。

    is_silent_at: check_headと同じ（時刻→無音かを返す関数）。無音と分かっている
    終点では、Whisperの時刻ずれによる語のまたぎを尻切れとしてNGにしない。
    """
    if is_silent_at is not None and is_silent_at(cut_end):
        return True, "OK（無音のためWhisperの時刻ずれとして扱った）"
    overlaps, w = tail_word_overlaps_cut(words, cut_end)
    if overlaps:
        return False, "NG（語の尻切れ: %r %.2f-%.2fs がカット終点をまたぐ）" % (
            w["word"], w["start"], w["end"])
    return True, "OK（終点前に語が終わっている）"


def trailing_gap(words, cut_end):
    """cut_end以降で最初に始まる語までの間隔（秒）を返す。該当語が無ければNone。"""
    after = [w for w in words if w["start"] >= cut_end]
    if not after:
        return None
    return after[0]["start"] - cut_end


def check_trailing_gap(words, cut_end, threshold=TRAILING_GAP_WARN_SEC):
    """終点直後に語が詰まっていないかの警告（NGにはしない。理由は末尾のNGにはしない旨を参照）。"""
    gap = trailing_gap(words, cut_end)
    if gap is None:
        return False, "情報（終点後±%.1fs内に語が見つからない）" % DEFAULT_RADIUS_SEC
    if gap < threshold:
        return True, "注意（終点後%.2fsで次の発話。文中切りの疑い）" % gap
    return False, "OK（終点後%.2fs空いている）" % gap
