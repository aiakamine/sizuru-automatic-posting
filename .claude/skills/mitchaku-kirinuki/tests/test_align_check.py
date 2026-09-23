# -*- coding: utf-8 -*-
"""scripts/align_check.py の単体テスト（第3段階：切り抜きそのものの精度）。
Whisper CLI・ffmpegを実際に呼ぶ統合テストは含まない（実データでの動作確認は
別途手動で行い、結果を報告する）。判定ロジックは純粋関数として切り出し、
Whisper呼び出し部分はモックで検証する。
"""
import json
import os
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import align_check as A  # noqa: E402


def W(start, end, word):
    return {"start": start, "end": end, "word": word}


class NormalizeSpeechTest(unittest.TestCase):
    def test_removes_multiline_marker_and_whitespace(self):
        # Arrange / Act / Assert
        self.assertEqual(A.normalize_speech("結果が出るみたいな人って／絶対いるんですよ"),
                          "結果が出るみたいな人って絶対いるんですよ")

    def test_removes_punctuation(self):
        self.assertEqual(A.normalize_speech("結果が出てなくても、絶対。"), "結果が出てなくても絶対")

    def test_empty_string_stays_empty(self):
        self.assertEqual(A.normalize_speech(""), "")


class LoadTelopsTest(unittest.TestCase):
    def test_parses_subscan_format(self):
        # Arrange: subscan.py が書き出す固定書式（先頭空白パディングを含む）
        content = " 811.177  813.079 f 24311- 24368 結果が出るみたいな人って／絶対いるんですよ\n"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = f.name
        try:
            # Act
            rows = A.load_telops(path)
            # Assert
            self.assertEqual(rows, [(811.177, 813.079, "結果が出るみたいな人って／絶対いるんですよ")])
        finally:
            os.unlink(path)

    def test_telop_at_returns_matching_text(self):
        rows = [(811.177, 813.079, "結果が出るみたいな人って／絶対いるんですよ")]
        self.assertEqual(A.telop_at(rows, 812.0), "結果が出るみたいな人って／絶対いるんですよ")

    def test_telop_at_returns_none_when_no_row_covers_the_time(self):
        # Arrange: telops.txtの生成が壊れている/範囲外など「本当に確認できていない」ケースを、
        # 「この時刻には正規にテロップが無い」(空文字列)と区別する（Codexレビューで検出：
        # 以前は両方とも""を返し、telops.txtが空でも全カットが黙ってOK扱いになっていた）
        rows = [(811.177, 813.079, "本文")]
        self.assertIsNone(A.telop_at(rows, 900.0))

    def test_telop_at_returns_empty_string_for_a_row_with_no_ocr_text(self):
        # Arrange: subscanが実際に「この区間は無テロップ」と判定した行（正規のOCR結果）
        rows = [(811.177, 813.079, "")]
        self.assertEqual(A.telop_at(rows, 812.0), "")

    def test_telop_at_prefers_the_new_telop_exactly_at_the_shared_boundary(self):
        # Arrange: subscan.pyの行は隣接しており、前の行のendと次の行のstartが同じ値になる。
        # カット頭はテロップの最初のフレームにスナップされる（SKILL.md絶対ルール1）ため、
        # ちょうど境界の時刻は「新しいテロップ」が正しい（Codexレビューで検出した回帰）
        rows = [(808.975, 811.177, "旧テロップ"), (811.177, 813.079, "新テロップ")]
        # Act / Assert
        self.assertEqual(A.telop_at(rows, 811.177), "新テロップ")


class ParseWhisperWordsTest(unittest.TestCase):
    def test_flattens_segments_and_sorts_by_start(self):
        # Arrange: セグメントをまたぐ語順が保証されない前提で敢えて逆順に入れる
        data = {"segments": [
            {"words": [W(2.0, 2.2, "です"), W(2.2, 2.4, "よ")]},
            {"words": [W(0.0, 0.3, "結果"), W(0.3, 0.5, "が")]},
        ]}
        # Act
        words = A.parse_whisper_words(data)
        # Assert
        self.assertEqual([w["word"] for w in words], ["結果", "が", "です", "よ"])

    def test_missing_segments_key_returns_empty_list(self):
        self.assertEqual(A.parse_whisper_words({}), [])


class FindWhisperCliTest(unittest.TestCase):
    def setUp(self):
        self._which = A.shutil.which
        self._exists = A.os.path.exists

    def tearDown(self):
        A.shutil.which = self._which
        A.os.path.exists = self._exists

    def test_explicit_path_wins(self):
        self.assertEqual(A.find_whisper_cli(explicit="/custom/whisper"), "/custom/whisper")

    def test_uses_path_whisper_when_available(self):
        A.shutil.which = lambda name: "/usr/local/bin/whisper"
        self.assertEqual(A.find_whisper_cli(), "/usr/local/bin/whisper")

    def test_falls_back_to_known_path_when_not_on_path(self):
        A.shutil.which = lambda name: None
        A.os.path.exists = lambda p: p == A.FALLBACK_WHISPER_BIN
        self.assertEqual(A.find_whisper_cli(), A.FALLBACK_WHISPER_BIN)

    def test_raises_when_not_found_anywhere(self):
        A.shutil.which = lambda name: None
        A.os.path.exists = lambda p: False
        with self.assertRaises(A.WhisperNotFoundError):
            A.find_whisper_cli()


class ExtractAudioSegmentTest(unittest.TestCase):
    def setUp(self):
        self._run = A.subprocess.run
        self.captured = None

    def tearDown(self):
        A.subprocess.run = self._run

    def _capture(self, cmd, **kwargs):
        self.captured = cmd
        return None

    def test_builds_ffmpeg_command_around_center_time(self):
        # Arrange
        A.subprocess.run = self._capture
        # Act
        start = A.extract_audio_segment("src.mp4", 811.20, 1.5, "out.wav")
        # Assert
        self.assertEqual(start, 811.20 - 1.5)
        self.assertIn("-i", self.captured)
        self.assertEqual(self.captured[self.captured.index("-i") + 1], "src.mp4")
        self.assertEqual(self.captured[self.captured.index("-ss") + 1], "%.3f" % (811.20 - 1.5))
        self.assertEqual(self.captured[self.captured.index("-t") + 1], "%.3f" % 3.0)
        self.assertEqual(self.captured[-1], "out.wav")

    def test_clamps_negative_start_to_zero(self):
        # Arrange: カット開始が動画冒頭に近く center-radius が負になる場合
        A.subprocess.run = self._capture
        # Act
        start = A.extract_audio_segment("src.mp4", 0.5, 1.5, "out.wav")
        # Assert
        self.assertEqual(start, 0.0)
        self.assertEqual(self.captured[self.captured.index("-ss") + 1], "0.000")


class RunWhisperTest(unittest.TestCase):
    def setUp(self):
        self._run = A.subprocess.run
        self._find = A.find_whisper_cli
        self.calls = []

    def tearDown(self):
        A.subprocess.run = self._run
        A.find_whisper_cli = self._find

    def test_returns_cached_json_without_calling_whisper(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            wav_path = os.path.join(cache_dir, "c00_cut00_head.wav")
            json_path = os.path.join(cache_dir, "c00_cut00_head.json")
            open(wav_path, "w").close()
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({"segments": []}, f)

            def fail(*a, **k):
                raise AssertionError("キャッシュがあるのにWhisperを再実行した")
            A.subprocess.run = fail
            # Act
            result = A.run_whisper(wav_path, cache_dir)
            # Assert
            self.assertEqual(result, json_path)

    def test_invokes_whisper_cli_with_expected_flags_when_no_cache(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            wav_path = os.path.join(cache_dir, "c00_cut00_head.wav")
            open(wav_path, "w").close()
            json_path = os.path.join(cache_dir, "c00_cut00_head.json")

            def fake_run(cmd, **kwargs):
                self.calls.append(cmd)
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump({"segments": []}, f)

                class R:
                    returncode = 0
                return R()
            A.subprocess.run = fake_run
            # Act
            result = A.run_whisper(wav_path, cache_dir, model="small", whisper_bin="/bin/whisper")
            # Assert
            self.assertEqual(result, json_path)
            cmd = self.calls[0]
            self.assertEqual(cmd[0], "/bin/whisper")
            self.assertIn(wav_path, cmd)
            self.assertIn("--word_timestamps", cmd)
            self.assertIn("--output_format", cmd)
            self.assertIn("json", cmd)
            self.assertIn("--language", cmd)
            self.assertIn("ja", cmd)
            self.assertIn("--model", cmd)
            self.assertIn("small", cmd)
            self.assertIn("--output_dir", cmd)
            self.assertIn(cache_dir, cmd)

    def test_raises_when_whisper_did_not_produce_json(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            wav_path = os.path.join(cache_dir, "c00_cut00_head.wav")
            open(wav_path, "w").close()

            def fake_run(cmd, **kwargs):
                class R:
                    returncode = 0
                return R()
            A.subprocess.run = fake_run
            with self.assertRaises(RuntimeError):
                A.run_whisper(wav_path, cache_dir, whisper_bin="/bin/whisper")


class HeadWordOverlapsCutTest(unittest.TestCase):
    def test_detects_word_straddling_cut_start(self):
        words = [W(810.5, 811.4, "よ")]
        overlaps, w = A.head_word_overlaps_cut(words, 811.20)
        self.assertTrue(overlaps)
        self.assertEqual(w["word"], "よ")

    def test_no_overlap_when_word_starts_after_cut(self):
        words = [W(811.20, 811.5, "結果")]
        overlaps, w = A.head_word_overlaps_cut(words, 811.20)
        self.assertFalse(overlaps)
        self.assertIsNone(w)

    def test_no_overlap_when_word_ends_before_cut(self):
        words = [W(810.0, 811.0, "よ")]
        overlaps, w = A.head_word_overlaps_cut(words, 811.20)
        self.assertFalse(overlaps)

    def test_negligible_overlap_within_asr_jitter_is_not_flagged(self):
        # Arrange: 03_C カット0の実例。「結果」(811.180-811.420) を811.20で切ると
        # 頭が0.02sしか削れない＝ASRの時刻ジッタの範囲として頭切れ扱いしない
        words = [W(811.180, 811.420, "結果")]
        overlaps, _ = A.head_word_overlaps_cut(words, 811.20)
        self.assertFalse(overlaps)

    def test_overlap_above_threshold_is_flagged(self):
        # 浮動小数の境界誤差を避けるため、閾値ちょうどではなく明確に上回る値で確認する
        words = [W(811.180, 811.420, "結果")]
        overlaps, _ = A.head_word_overlaps_cut(words, 811.180 + A.WORD_OVERLAP_MIN_SEC + 0.01)
        self.assertTrue(overlaps)


class TailWordOverlapsCutTest(unittest.TestCase):
    def test_detects_word_straddling_cut_end(self):
        words = [W(818.8, 819.3, "から")]
        overlaps, w = A.tail_word_overlaps_cut(words, 819.05)
        self.assertTrue(overlaps)
        self.assertEqual(w["word"], "から")

    def test_no_overlap_when_word_ends_before_cut_end(self):
        words = [W(818.0, 818.9, "から")]
        overlaps, w = A.tail_word_overlaps_cut(words, 819.05)
        self.assertFalse(overlaps)

    def test_negligible_overlap_within_asr_jitter_is_not_flagged(self):
        # Arrange: 語の尻が0.02sしか削れない場合はASRの時刻ジッタとして無視する
        words = [W(818.8, 819.07, "から")]
        overlaps, _ = A.tail_word_overlaps_cut(words, 819.05)
        self.assertFalse(overlaps)


class HeadActualTextTest(unittest.TestCase):
    def test_concatenates_words_from_cut_start_onward_in_order(self):
        words = [W(811.20, 811.4, "結果"), W(811.4, 811.6, "が"), W(810.0, 811.1, "よ")]
        text = A.head_actual_text(words, 811.20)
        self.assertEqual(text, "結果が")

    def test_empty_when_no_words_after_cut_start(self):
        words = [W(810.0, 811.0, "よ")]
        self.assertEqual(A.head_actual_text(words, 811.20), "")


class MatchHeadTest(unittest.TestCase):
    def test_exact_match_is_ok(self):
        ok, _ = A.match_head("結果が出るみたいな人って", "結果が出るみたいな人って")
        self.assertTrue(ok)

    def test_kanji_kana_variant_within_ratio_is_ok(self):
        # 表記揺れ（かな寄りの読み取り）でも高い類似度ならOK
        ok, _ = A.match_head("結果が出るみたいな人って", "結果がでるみたいな人って")
        self.assertTrue(ok)

    def test_completely_different_text_is_ng(self):
        ok, _ = A.match_head("結果が出るみたいな人って", "全く違う発話内容です")
        self.assertFalse(ok)

    def test_empty_expected_is_ok_regardless_of_actual(self):
        ok, _ = A.match_head("", "何か喋っている")
        self.assertTrue(ok)

    def test_actual_too_short_is_ng(self):
        ok, reason = A.match_head("結果が出るみたいな人って", "結")
        self.assertFalse(ok)
        self.assertIn("比較不能", reason)

    def test_missing_leading_word_is_ng_despite_high_overall_similarity(self):
        # Arrange: learnings.md記載の実例（2026-09-15）。テロップは「結果が」を補っており、
        # 実際の発話は「出てるのに」から始まる。全体の類似度は高い(0.81)ため、比較窓を
        # 広く取ると誤ってOKになってしまう（Codexレビューで検出された回帰）
        ok, reason = A.match_head("結果が出てるのに評価されてない人とかも",
                                   "出てるのに評価されてない人とかも")
        # Assert
        self.assertFalse(ok)
        self.assertIn("NG", reason)

    def test_missing_leading_word_not_caught_by_narrow_window_alone(self):
        # Arrange: Codexレビューで検出された追加の反例。6文字窓に絞っただけでは
        # まだ「私は」の欠落を見逃す（類似度0.67で通ってしまう）。先頭文字そのものが
        # 一致しているかを見る必要がある
        ok, reason = A.match_head("私は営業しています", "営業しています")
        # Assert
        self.assertFalse(ok)
        self.assertIn("NG", reason)


class CheckHeadTest(unittest.TestCase):
    def test_word_straddling_cut_start_is_ng_before_text_comparison(self):
        # Arrange: テキストが一致していても頭切れがあれば問答無用でNG
        words = [W(810.5, 811.5, "結果"), W(811.5, 811.8, "が")]
        # Act
        ok, reason = A.check_head(words, 811.20, "結果が出る")
        # Assert
        self.assertFalse(ok)
        self.assertIn("頭切れ", reason)

    def test_matching_head_text_without_overlap_is_ok(self):
        words = [W(811.20, 811.4, "結果"), W(811.4, 811.6, "が")]
        ok, _ = A.check_head(words, 811.20, "結果が出るみたいな人って")
        self.assertTrue(ok)

    def test_old_cut_start_misses_the_intended_phrase(self):
        # Arrange: 03_C カット0の実例（旧値813.08はこのフレーズの発話が既に終わった後）。
        # 813.08近辺に語が無い（=次のテロップの発話がまだ始まっていない)ケースを再現する
        words = []
        # Act
        ok, reason = A.check_head(words, 813.08, "結果が出てるのに評価されてない人とかも")
        # Assert
        self.assertFalse(ok)
        self.assertIn("比較不能", reason)

    def test_no_telop_coverage_is_ng_not_silently_ok(self):
        # Arrange: telops.txtにこの時刻を含む行が無い（生成失敗・範囲外等）。
        # 空文字列（正規に無テロップ）と混同して自動OKにしない（Codexレビューで検出）
        words = [W(811.20, 811.4, "結果")]
        # Act
        ok, reason = A.check_head(words, 811.20, None)
        # Assert
        self.assertFalse(ok)
        self.assertIn("telops.txt", reason)


class CheckTailTest(unittest.TestCase):
    def test_word_straddling_cut_end_is_ng(self):
        words = [W(818.8, 819.3, "から")]
        ok, reason = A.check_tail(words, 819.05)
        self.assertFalse(ok)
        self.assertIn("尻切れ", reason)

    def test_word_ending_before_cut_end_is_ok(self):
        words = [W(818.0, 818.9, "から")]
        ok, _ = A.check_tail(words, 819.05)
        self.assertTrue(ok)


class TrailingGapTest(unittest.TestCase):
    def test_returns_gap_to_next_word(self):
        words = [W(819.30, 819.5, "次")]
        self.assertAlmostEqual(A.trailing_gap(words, 819.05), 0.25)

    def test_returns_none_when_no_word_after_cut_end(self):
        words = [W(818.0, 818.9, "前")]
        self.assertIsNone(A.trailing_gap(words, 819.05))


class CheckTrailingGapTest(unittest.TestCase):
    def test_short_gap_is_a_warning_not_failure(self):
        words = [W(819.10, 819.3, "次")]
        is_warn, reason = A.check_trailing_gap(words, 819.05)
        self.assertTrue(is_warn)
        self.assertIn("注意", reason)

    def test_gap_above_threshold_is_ok(self):
        # 浮動小数の境界誤差を避けるため、閾値ちょうどではなく明確に上回る値で確認する
        words = [W(819.05 + A.TRAILING_GAP_WARN_SEC + 0.05, 819.6, "次")]
        is_warn, reason = A.check_trailing_gap(words, 819.05)
        self.assertFalse(is_warn)
        self.assertIn("OK", reason)

    def test_no_following_word_is_informational_not_a_warning(self):
        is_warn, reason = A.check_trailing_gap([], 819.05)
        self.assertFalse(is_warn)
        self.assertIn("情報", reason)


class BoundaryWavNameTest(unittest.TestCase):
    def test_different_boundary_times_produce_different_names(self):
        # Arrange: clips_plan.py でカット点を修正した際、古いキャッシュを再利用しないための保証
        old_name = A.boundary_wav_name("c02_cut00", "head", 813.08)
        new_name = A.boundary_wav_name("c02_cut00", "head", 811.20)
        # Act / Assert
        self.assertNotEqual(old_name, new_name)

    def test_same_boundary_time_produces_same_name(self):
        a = A.boundary_wav_name("c02_cut00", "head", 811.20)
        b = A.boundary_wav_name("c02_cut00", "head", 811.20)
        self.assertEqual(a, b)


class LoadWordsForBoundaryTest(unittest.TestCase):
    """extract_audio_segmentをモックし、subprocess.run（run_whisperの内部実装）は
    実物のまま動かして、キャッシュの有効性判定を検証する（Codexレビューで検出：
    radius/model/srcを変えても古いキャッシュを使い回す回帰）。
    """

    def setUp(self):
        self._extract = A.extract_audio_segment
        self._subprocess_run = A.subprocess.run
        self.extract_calls = []
        self.subprocess_calls = []

        def fake_extract(src, boundary_time, radius, out_wav):
            self.extract_calls.append((src, boundary_time, radius, out_wav))
            open(out_wav, "w").close()
            return max(0.0, boundary_time - radius)

        def fake_subprocess_run(cmd, **kwargs):
            self.subprocess_calls.append(cmd)
            wav_path = cmd[1]
            cache_dir = cmd[cmd.index("--output_dir") + 1]
            json_path = os.path.join(cache_dir, os.path.splitext(os.path.basename(wav_path))[0] + ".json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({"segments": []}, f)

            class R:
                returncode = 0
            return R()

        A.extract_audio_segment = fake_extract
        A.subprocess.run = fake_subprocess_run

    def tearDown(self):
        A.extract_audio_segment = self._extract
        A.subprocess.run = self._subprocess_run

    def _load(self, src="src.mp4", boundary_time=811.20, radius=1.5, cache_dir=None,
              wav_name="c00_head", model="small"):
        return A.load_words_for_boundary(src, boundary_time, radius, cache_dir, wav_name,
                                          model=model, whisper_bin="/bin/whisper")

    def test_reuses_cache_when_params_are_unchanged(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            self._load(cache_dir=cache_dir)
            self._load(cache_dir=cache_dir)
        self.assertEqual(len(self.extract_calls), 1)
        self.assertEqual(len(self.subprocess_calls), 1)

    def test_changing_radius_invalidates_cache_and_recomputes_clip_start(self):
        # Arrange: 同じwav_nameのまま radius だけ変える（実運用では起きうる）
        with tempfile.TemporaryDirectory() as cache_dir:
            self._load(cache_dir=cache_dir, radius=1.5)
            # Act
            self._load(cache_dir=cache_dir, radius=3.0)
        # Assert: radiusが変わったので再抽出・再認識される（古いclip_startを使い回さない）
        self.assertEqual(len(self.extract_calls), 2)
        self.assertEqual(self.extract_calls[1][2], 3.0)
        self.assertEqual(len(self.subprocess_calls), 2)

    def test_changing_model_invalidates_whisper_cache(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            self._load(cache_dir=cache_dir, model="small")
            # Act
            self._load(cache_dir=cache_dir, model="medium")
        # Assert: モデルが変わったので古いWhisper結果を使い回さず再度呼ぶ
        self.assertEqual(len(self.subprocess_calls), 2)
        self.assertIn("medium", self.subprocess_calls[1])

    def test_changing_source_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            self._load(cache_dir=cache_dir, src="src_a.mp4")
            self._load(cache_dir=cache_dir, src="src_b.mp4")
        self.assertEqual(len(self.extract_calls), 2)

    def test_replacing_source_file_at_the_same_path_invalidates_cache(self):
        # Arrange: 同じパスのsrc.mp4を再ダウンロード/再書き出しした場合、パス比較だけでは
        # 気づけず古い単語タイムスタンプを使い回してしまう（Codexレビューで検出）。
        # mtimeも見ることで、安価に（内容を全読みせず）この事故を検知する
        with tempfile.TemporaryDirectory() as cache_dir, \
             tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as src_file:
            src_path = src_file.name
        try:
            os.utime(src_path, (1000, 1000))
            self._load(cache_dir=cache_dir, src=src_path)
            # Act: 同じパスのままファイルを更新（mtimeが変わる）
            os.utime(src_path, (2000, 2000))
            self._load(cache_dir=cache_dir, src=src_path)
            # Assert
            self.assertEqual(len(self.extract_calls), 2)
        finally:
            os.unlink(src_path)


class CheckCutTest(unittest.TestCase):
    def setUp(self):
        self._loader = A.load_words_for_boundary

    def tearDown(self):
        A.load_words_for_boundary = self._loader

    def test_reports_ng_when_head_word_overlaps_and_ok_when_tail_clean(self):
        # Arrange: head側はまたぎ語あり、tail側は語が終点前に終わっている
        def fake_loader(src, boundary_time, radius, cache_dir, wav_name, model=None, whisper_bin=None):
            if "_head_" in wav_name:
                return [W(810.5, 811.5, "結果")]
            return [W(818.0, 818.9, "から")]
        A.load_words_for_boundary = fake_loader
        # Act
        rows = A.check_cut("src.mp4", [(811.177, 813.079, "結果が出るみたいな人って")],
                            811.20, 819.05, "cache", "c00_cut00")
        # Assert
        head_row = next(r for r in rows if r[2] == "head")
        tail_row = next(r for r in rows if r[2] == "tail")
        self.assertTrue(head_row[0])   # NG
        self.assertFalse(tail_row[0])  # OK

    def test_is_silent_at_is_passed_through_and_suppresses_word_straddle_ng(self):
        # Arrange: 対談スキルで実測されたWhisperの時刻ジッタ（無音区間で最大0.75秒ほど
        # ずれる）と同じ再現。is_silent_atを渡すと頭切れ・尻切れの誤NGがOKになる
        def fake_loader(src, boundary_time, radius, cache_dir, wav_name, model=None, whisper_bin=None):
            if "_head_" in wav_name:
                return [W(0.84, 1.30, "結果"), W(1.30, 1.6, "が")]
            return [W(7.97, 8.89, "そう")]
        A.load_words_for_boundary = fake_loader
        rows = A.check_cut("src.mp4", [(0.0, 999.0, "結果が出るみたいな人って")],
                            0.984, 8.692, "cache", "c00_cut00", is_silent_at=lambda t: True)
        head_row = next(r for r in rows if r[2] == "head")
        tail_row = next(r for r in rows if r[2] == "tail")
        self.assertFalse(head_row[0])  # NGでない
        self.assertFalse(tail_row[0])  # NGでない
        self.assertIn("無音", head_row[4])
        self.assertIn("無音", tail_row[4])


class MakeIsSilentAtTest(unittest.TestCase):
    """silence30.txt（無ければensure_silence_fileで作る）の区間の内側（端から0.015秒以上
    離れている）にカット点が入っていれば無音として扱うis_silent_at(t)を作る。

    以前は区間の外側でも±0.03秒以内なら無音扱いにしていたが、これはゆるすぎて本物の
    頭切れ・尻切れを見逃す（team-leadの実測、2026-09-17）。区間の端に近すぎる場合も
    「無音と言い切れない」として除外する（find_silence_interior、margin=0.015秒）。
    ensure_silence_fileはモックし、ffmpegを実際には呼ばない。
    """

    def setUp(self):
        self._ensure = A.ensure_silence_file

    def tearDown(self):
        A.ensure_silence_file = self._ensure

    def test_time_well_inside_silence_interval_is_silent(self):
        def fake_ensure(path, src):
            with open(path, "w", encoding="utf-8") as f:
                f.write("silence_start: 10.0\nsilence_end: 10.5 | silence_duration: 0.5\n")
            return path
        A.ensure_silence_file = fake_ensure
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "silence30.txt")
            is_silent_at = A.make_is_silent_at(path, "src.mp4")
        self.assertTrue(is_silent_at(10.2))

    def test_time_too_close_to_interval_edge_is_not_silent(self):
        # Arrange: 区間の端から0.015秒未満は「無音と言い切れない」として除外する
        def fake_ensure(path, src):
            with open(path, "w", encoding="utf-8") as f:
                f.write("silence_start: 10.0\nsilence_end: 10.5 | silence_duration: 0.5\n")
            return path
        A.ensure_silence_file = fake_ensure
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "silence30.txt")
            is_silent_at = A.make_is_silent_at(path, "src.mp4")
        self.assertFalse(is_silent_at(10.0 + 0.01))
        self.assertFalse(is_silent_at(10.5 - 0.01))
        self.assertFalse(is_silent_at(10.5 + 0.01))  # 区間の外側は許容しない

    def test_time_just_beyond_margin_from_edges_is_silent(self):
        def fake_ensure(path, src):
            with open(path, "w", encoding="utf-8") as f:
                f.write("silence_start: 10.0\nsilence_end: 10.5 | silence_duration: 0.5\n")
            return path
        A.ensure_silence_file = fake_ensure
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "silence30.txt")
            is_silent_at = A.make_is_silent_at(path, "src.mp4")
        self.assertTrue(is_silent_at(10.0 + 0.02))
        self.assertTrue(is_silent_at(10.5 - 0.02))

    def test_time_far_from_any_silence_is_not_silent(self):
        def fake_ensure(path, src):
            with open(path, "w", encoding="utf-8") as f:
                f.write("silence_start: 10.0\nsilence_end: 10.5 | silence_duration: 0.5\n")
            return path
        A.ensure_silence_file = fake_ensure
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "silence30.txt")
            is_silent_at = A.make_is_silent_at(path, "src.mp4")
        self.assertFalse(is_silent_at(50.0))

    def test_does_not_regenerate_when_silence_file_already_exists(self):
        calls = []

        def fake_ensure(path, src):
            calls.append(path)
            return path
        A.ensure_silence_file = fake_ensure
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "silence30.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
            A.make_is_silent_at(path, "src.mp4")
        self.assertEqual(calls, [path])


class MainSilenceIntegrationTest(unittest.TestCase):
    """main()が--silenceで指定したファイルを使ってis_silent_atを組み立て、
    check_cutへ渡すことを確認する（Whisper・ffmpegはモック）。
    """

    def setUp(self):
        self._loader = A.load_words_for_boundary
        self._ensure = A.ensure_silence_file

        def fake_loader(src, boundary_time, radius, cache_dir, wav_name, model=None, whisper_bin=None):
            if "_head_" in wav_name:
                return [W(0.84, 1.30, "結果")]
            return [W(7.97, 8.89, "そう")]
        A.load_words_for_boundary = fake_loader

        def fake_ensure(path, src):
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("silence_start: 0.5\nsilence_end: 1.05 | silence_duration: 0.55\n"
                            "silence_start: 8.6\nsilence_end: 9.0 | silence_duration: 0.4\n")
            return path

        A.ensure_silence_file = fake_ensure

    def tearDown(self):
        A.load_words_for_boundary = self._loader
        A.ensure_silence_file = self._ensure

    def test_silence_file_suppresses_word_straddle_ng_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            clips_path = os.path.join(d, "clips_plan.py")
            with open(clips_path, "w", encoding="utf-8") as f:
                f.write('CLIPS = [{"file": "c1.mp4", "cuts": [(0.984, 8.692)]}]\n')
            telops_path = os.path.join(d, "telops.txt")
            with open(telops_path, "w", encoding="utf-8") as f:
                f.write(" 0.000 999.000 f     0- 30000 結果が出るみたいな人って\n")
            silence_path = os.path.join(d, "silence30.txt")
            rc = A.main([clips_path, "--telops", telops_path, "--src", "src.mp4",
                         "--silence", silence_path])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
