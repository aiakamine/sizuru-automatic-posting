# -*- coding: utf-8 -*-
"""kirinuki_core.speech の単体テスト。
check_head/check_tail に追加した is_silent_at（時刻→無音かを返す関数）だけを対象にする。
その他の関数（normalize_speech・match_head等）は mitchaku-kirinuki/tests/test_align_check.py
が import 経由で担保している（移しただけの関数はそちら任せにする、という既存方針を踏襲）。

Whisperの単語時刻は、無音区間ではジッタが最大0.75秒程度に達することが実測されている
（対談の合成素材、2026-09-17）。既存の WORD_OVERLAP_MIN_SEC=0.05 では吸収できないため、
「そのカット点が実際に無音かどうか」を呼び出し側（各スキルのalign_check.py）が判定して
渡し、無音なら語のまたぎをNGにしない・頭の本文比較にまたぐ語も含める、という設計にした。
"""
import json
import os
import sys
import tempfile
import unittest

CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(CORE_DIR))
from kirinuki_core import speech as S  # noqa: E402


def W(start, end, word):
    return {"start": start, "end": end, "word": word}


class ExtractAudioSegmentMapsAudioTrackTest(unittest.TestCase):
    """複数音声トラックがある素材で、ffmpegが自動選択したトラックが
    common.load_pcm/get_audio_start_time が見ているa:0と食い違わないよう、
    -mapで明示的にa:0を指定する（common.load_pcmと同じ考え方。Codexレビューで検出）。
    """

    def setUp(self):
        self._run = S.subprocess.run
        self.captured = None

    def tearDown(self):
        S.subprocess.run = self._run

    def test_maps_first_audio_track_explicitly(self):
        def fake_run(cmd, **kwargs):
            self.captured = cmd
        S.subprocess.run = fake_run
        S.extract_audio_segment("src.mp4", 10.0, 1.5, "out.wav")
        self.assertIn("-map", self.captured)
        self.assertEqual(self.captured[self.captured.index("-map") + 1], "0:a:0")


class HeadActualTextIncludingOverlapTest(unittest.TestCase):
    def test_includes_words_straddling_cut_start(self):
        # Arrange: 「結果」がcut_start(1.107)をまたいでいる（0.84-1.30）
        words = [W(0.84, 1.30, "結果"), W(1.30, 1.5, "が")]
        text = S.head_actual_text_including_overlap(words, 1.107)
        self.assertEqual(text, "結果が")

    def test_excludes_words_entirely_before_cut_start(self):
        words = [W(0.0, 0.5, "前の語"), W(1.2, 1.5, "後の語")]
        text = S.head_actual_text_including_overlap(words, 1.107)
        self.assertEqual(text, "後の語")

    def test_word_ending_exactly_at_cut_start_is_excluded(self):
        words = [W(0.0, 1.107, "前の語")]
        self.assertEqual(S.head_actual_text_including_overlap(words, 1.107), "")


class CheckHeadWithIsSilentAtTest(unittest.TestCase):
    def test_default_behavior_is_unchanged_when_is_silent_at_is_none(self):
        # Arrange: 頭切れがあれば、is_silent_atを渡さない限り従来どおりNG
        words = [W(810.5, 811.5, "結果"), W(811.5, 811.8, "が")]
        ok, reason = S.check_head(words, 811.20, "結果が出る")
        self.assertFalse(ok)
        self.assertIn("頭切れ", reason)

    def test_word_straddling_cut_is_not_ng_when_silent(self):
        # Arrange: team-leadの実測例。カット0.984秒・発話は1.107秒から・Whisperは
        # 「結果」を0.84-1.30秒とした（実際は無音の中で切っている）
        words = [W(0.84, 1.30, "結果"), W(1.30, 1.5, "が")]
        ok, reason = S.check_head(words, 0.984, "結果が出るみたいな人って",
                                   is_silent_at=lambda t: True)
        self.assertTrue(ok)
        self.assertIn("無音", reason)

    def test_same_words_are_ng_when_not_silent(self):
        # Arrange: 同じ語の並びでも、is_silent_atがFalseを返せば従来どおりNG扱い
        words = [W(0.84, 1.30, "結果"), W(1.30, 1.5, "が")]
        ok, reason = S.check_head(words, 0.984, "結果が出るみたいな人って",
                                   is_silent_at=lambda t: False)
        self.assertFalse(ok)
        self.assertIn("頭切れ", reason)

    def test_silent_head_compares_against_word_straddling_the_cut(self):
        # Arrange: テロップが語を補っている（「結果が」）NGクリップは、無音区間補正後も
        # 実際の発話（「出てるのに」）と比べて正しくNGになる
        words = [W(0.5, 0.9, "出てる"), W(0.9, 1.1, "のに")]
        ok, reason = S.check_head(words, 0.984, "結果が出てるのに評価されてない人とかも",
                                   is_silent_at=lambda t: True)
        self.assertFalse(ok)

    def test_silent_head_with_expected_text_matching_the_straddling_word_is_ok(self):
        # Arrange: 発話の頭に正しく置いたOKクリップは、無音補正後もOKのまま
        words = [W(0.84, 1.30, "結果"), W(1.30, 1.6, "が"), W(1.6, 2.0, "出る")]
        ok, reason = S.check_head(words, 0.984, "結果が出るみたいな人って",
                                   is_silent_at=lambda t: True)
        self.assertTrue(ok)

    def test_missing_telop_row_is_still_ng_even_when_silent(self):
        # Arrange: telops.txtに行が無い場合の既存NGは、無音判定と無関係に維持する
        words = [W(0.84, 1.30, "結果")]
        ok, reason = S.check_head(words, 0.984, None, is_silent_at=lambda t: True)
        self.assertFalse(ok)
        self.assertIn("telops.txt", reason)


class CheckTailWithIsSilentAtTest(unittest.TestCase):
    def test_default_behavior_is_unchanged_when_is_silent_at_is_none(self):
        words = [W(818.8, 819.3, "から")]
        ok, reason = S.check_tail(words, 819.05)
        self.assertFalse(ok)
        self.assertIn("尻切れ", reason)

    def test_word_straddling_cut_end_is_not_ng_when_silent(self):
        # Arrange: team-leadの実測例。カット5.355秒・発話は5.384秒から・Whisperは
        # 「出」を5.28-5.50秒とした
        words = [W(5.28, 5.50, "出")]
        ok, reason = S.check_tail(words, 5.355, is_silent_at=lambda t: True)
        self.assertTrue(ok)
        self.assertIn("無音", reason)

    def test_same_word_is_ng_when_not_silent(self):
        words = [W(5.28, 5.50, "出")]
        ok, reason = S.check_tail(words, 5.355, is_silent_at=lambda t: False)
        self.assertFalse(ok)
        self.assertIn("尻切れ", reason)

    def test_large_jitter_example_is_not_ng_when_silent(self):
        # Arrange: team-leadの実測例。カット8.692秒・発話は8.717秒から・Whisperは
        # 「そう」を7.97-8.89秒とした（直前の無音を単語に含めている）
        words = [W(7.97, 8.89, "そう")]
        ok, reason = S.check_tail(words, 8.692, is_silent_at=lambda t: True)
        self.assertTrue(ok)


class CheckHeadAndCheckTailEmptyWordsTest(unittest.TestCase):
    """語が1つも無い（Whisperが何も聞き取らなかった）境界の挙動を固定する。
    頭切れ・尻切れ判定はまたぐ語が無ければ常にFalseになるため、is_silent_atの
    有無に関わらずcheck_tailはOKになる。check_headは実際の発話が空文字列になり
    HEAD_MATCH_MIN_CHARS未満で「短すぎて比較不能」のNGになる。
    """

    def test_check_head_with_no_words_is_ng_for_being_too_short(self):
        ok, reason = S.check_head([], 1.0, "結果が出る", is_silent_at=lambda t: False)
        self.assertFalse(ok)
        self.assertIn("短すぎて比較不能", reason)

    def test_check_head_with_no_words_is_still_ng_when_silent(self):
        # Arrange: is_silent_atがTrueでも、頭切れ判定をスキップするだけで
        # 本文比較（空文字列 vs 期待テロップ）自体は変わらずNGのまま
        ok, reason = S.check_head([], 1.0, "結果が出る", is_silent_at=lambda t: True)
        self.assertFalse(ok)
        self.assertIn("短すぎて比較不能", reason)

    def test_check_tail_with_no_words_is_ok(self):
        # Arrange: またぐ語が無ければ尻切れではない（is_silent_atを渡さない既定でも）
        ok, reason = S.check_tail([], 1.0)
        self.assertTrue(ok)

    def test_check_tail_with_no_words_is_ok_when_silent(self):
        ok, reason = S.check_tail([], 1.0, is_silent_at=lambda t: True)
        self.assertTrue(ok)


class WordBoundaryExactlyAtCutPointTest(unittest.TestCase):
    """語の境界がカット点とちょうど同じ時刻の場合の挙動を固定する
    （head_word_overlaps_cut/tail_word_overlaps_cutは w["start"] < cut < w["end"] という
    厳密不等号なので、語の端がカット点とちょうど一致する場合はまたぎ扱いにならない）。
    """

    def test_word_starting_exactly_at_cut_start_is_not_a_head_overlap(self):
        # Arrange: 語がカット開始とちょうど同時に始まる（頭切れではない）
        words = [W(1.0, 1.5, "発話です")]
        ok, reason = S.check_head(words, 1.0, "発話です", is_silent_at=lambda t: False)
        self.assertTrue(ok, reason)

    def test_word_ending_exactly_at_cut_end_is_not_a_tail_overlap(self):
        # Arrange: 語がカット終点とちょうど同時に終わる（尻切れではない）
        words = [W(0.5, 1.0, "発話です")]
        ok, reason = S.check_tail(words, 1.0, is_silent_at=lambda t: False)
        self.assertTrue(ok, reason)


class LoadWordsForBoundaryCorruptedMetaTest(unittest.TestCase):
    """load_words_for_boundaryが使うWhisperキャッシュのメタ(<wav>.meta.json)が壊れて
    いても、他の無効条件（メタ無し・src/radius/model不一致）と同様に黙って再生成する
    （Codexレビューで検出：以前はjson.load()がJSONDecodeErrorを送出して落ちていた）。
    """

    def _fake_extract(self, calls):
        def extract_fn(src, boundary_time, radius, out_wav):
            calls.append((src, boundary_time, radius, out_wav))
            open(out_wav, "w").close()
            return max(0.0, boundary_time - radius)
        return extract_fn

    def _fake_run_whisper(self, calls):
        def run_whisper_fn(wav_path, cache_dir, model=None, whisper_bin=None):
            calls.append((wav_path, model))
            json_path = os.path.join(cache_dir, os.path.splitext(os.path.basename(wav_path))[0] + ".json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({"segments": []}, f)
            return json_path
        return run_whisper_fn

    def test_corrupted_meta_json_is_treated_as_missing_and_regenerates(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            wav_path = os.path.join(cache_dir, "c00_head.wav")
            open(wav_path, "w").close()
            with open(wav_path + ".meta.json", "w", encoding="utf-8") as f:
                f.write("{not valid json")
            extract_calls, whisper_calls = [], []
            S.load_words_for_boundary(
                "src.mp4", 10.0, 1.5, cache_dir, "c00_head", model="small",
                extract_fn=self._fake_extract(extract_calls),
                run_whisper_fn=self._fake_run_whisper(whisper_calls))
        self.assertEqual(len(extract_calls), 1)
        self.assertEqual(len(whisper_calls), 1)

    def test_meta_json_that_is_not_a_dict_is_treated_as_missing(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            wav_path = os.path.join(cache_dir, "c00_head.wav")
            open(wav_path, "w").close()
            with open(wav_path + ".meta.json", "w", encoding="utf-8") as f:
                json.dump([1, 2, 3], f)
            extract_calls, whisper_calls = [], []
            S.load_words_for_boundary(
                "src.mp4", 10.0, 1.5, cache_dir, "c00_head", model="small",
                extract_fn=self._fake_extract(extract_calls),
                run_whisper_fn=self._fake_run_whisper(whisper_calls))
        self.assertEqual(len(extract_calls), 1)
        self.assertEqual(len(whisper_calls), 1)


if __name__ == "__main__":
    unittest.main()
