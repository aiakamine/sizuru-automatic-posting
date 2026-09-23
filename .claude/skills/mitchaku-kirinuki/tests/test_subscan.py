# -*- coding: utf-8 -*-
"""scripts/subscan.py の単体テスト（第3段階：fps自動化・フェードイン対策・帯位置推定）。
ffmpeg/ocrcliを実際に呼ぶ統合テストは含まない（短尺クリップでの動作確認は
別途手動で行い、結果を報告する）。
"""
import os
import sys
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import subscan as S  # noqa: E402


class ParseFrameRateTest(unittest.TestCase):
    def test_parses_fractional_rate(self):
        self.assertAlmostEqual(S.parse_frame_rate("30000/1001"), 30000 / 1001)

    def test_parses_integer_ratio(self):
        self.assertEqual(S.parse_frame_rate("30/1"), 30.0)

    def test_parses_plain_number_without_slash(self):
        self.assertEqual(S.parse_frame_rate("25"), 25.0)

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(S.parse_frame_rate("  30/1\n"), 30.0)


class ClampSearchWindowTest(unittest.TestCase):
    """帯位置推定の探索窓が画像の高さをはみ出さないことを確認する（Codexレビューで検出：
    既定値(band_y=860, margin=150)だと窓が1080px高の素材でも下端を140pxはみ出し、
    ffmpegのcropがy0を勝手にずらして戻り値の座標系と食い違っていた）。
    """

    def test_window_within_bounds_is_unchanged(self):
        y0, h = S.clamp_search_window(y0=100, h=200, frame_height=1080)
        self.assertEqual((y0, h), (100, 200))

    def test_window_exceeding_bottom_is_shrunk_not_shifted(self):
        # 既定値相当（y0=710, h=510）が1080px高の素材をはみ出すケース
        y0, h = S.clamp_search_window(y0=710, h=510, frame_height=1080)
        self.assertEqual(y0, 710)
        self.assertEqual(h, 1080 - 710)

    def test_negative_y0_is_clamped_to_zero(self):
        y0, h = S.clamp_search_window(y0=-50, h=200, frame_height=1080)
        self.assertEqual(y0, 0)


class FrameSeekTimeTest(unittest.TestCase):
    """フレーム境界ちょうどの時刻でシークすると、ffmpegの丸め方次第で1フレーム
    ずれて選ばれることがある（Codexレビューで検出：30fpsで0.067秒シークがframe3を
    返した実測あり）。フレームの中央時刻を使うことで境界の丸め誤差から余裕を持たせる。
    """

    def test_returns_time_within_the_frame_not_at_its_edge(self):
        fps = 30000 / 1001
        t = S.frame_seek_time(2, fps)
        self.assertGreater(t, 2 / fps)
        self.assertLess(t, 3 / fps)

    def test_is_approximately_half_a_frame_after_the_boundary(self):
        fps = 30.0
        t = S.frame_seek_time(10, fps)
        self.assertAlmostEqual(t, 10.5 / fps)


class GetFpsTest(unittest.TestCase):
    def setUp(self):
        self._run = S.subprocess.run

    def tearDown(self):
        S.subprocess.run = self._run

    def test_calls_ffprobe_and_parses_stdout(self):
        class R:
            stdout = "30000/1001\n"
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return R()
        S.subprocess.run = fake_run
        fps = S.get_fps("src.mp4")
        self.assertAlmostEqual(fps, 30000 / 1001)
        self.assertIn("ffprobe", captured["cmd"])
        self.assertIn("src.mp4", captured["cmd"])


class ComputeFrameDiffsTest(unittest.TestCase):
    def test_identical_frames_have_zero_diff(self):
        frame = bytes([100, 100, 100, 100])
        data = frame + frame + frame
        diffs = S.compute_frame_diffs(data, frame_size=4)
        self.assertEqual(diffs, [0, 0])

    def test_detects_difference_between_frames(self):
        frame_a = bytes([0, 0, 0, 0])
        frame_b = bytes([255, 255, 255, 255])
        data = frame_a + frame_b
        diffs = S.compute_frame_diffs(data, frame_size=4)
        self.assertEqual(diffs, [255.0])


class FindChangepointsTest(unittest.TestCase):
    def test_single_spike_produces_one_changepoint(self):
        diffs = [0, 0, 10, 0, 0]
        cps = S.find_changepoints(diffs, threshold=6.0, decay_ratio=0.5)
        self.assertEqual(cps, [3])

    def test_sustained_spike_is_not_double_counted(self):
        # Arrange: 切替直後の数フレームがdecay_ratio*threshold以上を維持する場合、
        # 1回の切替としてまとめる（バタつきを1切替点として扱う）
        diffs = [0, 10, 8, 4, 0]
        cps = S.find_changepoints(diffs, threshold=6.0, decay_ratio=0.5)
        self.assertEqual(cps, [2])

    def test_no_spike_returns_empty_list(self):
        self.assertEqual(S.find_changepoints([1, 1, 1], threshold=6.0), [])


class BuildSegmentsTest(unittest.TestCase):
    def test_splits_by_changepoints(self):
        segs = S.build_segments([10, 20], n_frames=30, min_len=1)
        self.assertEqual(segs, [(0, 10), (10, 20), (20, 30)])

    def test_drops_segments_shorter_than_min_len(self):
        # 変化点10と12が近く、区間(10,12)が短すぎる場合は除外する
        segs = S.build_segments([10, 12], n_frames=30, min_len=4)
        self.assertEqual(segs, [(0, 10), (12, 30)])


class EdgeRowProfileTest(unittest.TestCase):
    def test_flat_row_has_zero_energy(self):
        rows = [[100, 100, 100, 100]]
        self.assertEqual(S.edge_row_profile(rows), [0])

    def test_high_contrast_row_has_higher_energy_than_flat_row(self):
        flat = [100, 100, 100, 100]
        edgy = [0, 255, 0, 255]
        profile = S.edge_row_profile([flat, edgy])
        self.assertLess(profile[0], profile[1])


class BestBandStartTest(unittest.TestCase):
    def test_finds_window_with_highest_total_energy(self):
        # Arrange: 行4-6だけエネルギーが高い帯
        profile = [0, 0, 0, 0, 10, 10, 10, 0, 0, 0]
        # Act
        start = S.best_band_start(profile, band_h=3)
        # Assert
        self.assertEqual(start, 4)

    def test_profile_shorter_than_band_h_returns_zero(self):
        self.assertEqual(S.best_band_start([1, 2], band_h=5), 0)


class BandOffsetWarningTest(unittest.TestCase):
    def test_no_warning_within_threshold(self):
        self.assertIsNone(S.band_offset_warning(estimated_y=870, configured_y=860))

    def test_warning_when_offset_exceeds_threshold(self):
        warn = S.band_offset_warning(estimated_y=900, configured_y=860)
        self.assertIsNotNone(warn)
        self.assertIn("900", warn)
        self.assertIn("860", warn)


class EmptyRateTest(unittest.TestCase):
    def test_computes_ratio_of_empty_strings(self):
        self.assertEqual(S.empty_rate(["", "a", "", "b"]), 0.5)

    def test_empty_list_has_zero_rate(self):
        self.assertEqual(S.empty_rate([]), 0.0)


class EmptyRateWarningTest(unittest.TestCase):
    def test_no_warning_below_threshold(self):
        self.assertIsNone(S.empty_rate_warning(0.3))

    def test_warning_above_threshold(self):
        warn = S.empty_rate_warning(0.6)
        self.assertIsNotNone(warn)
        self.assertIn("60", warn)


class TextsSimilarTest(unittest.TestCase):
    def test_identical_texts_match(self):
        self.assertTrue(S.texts_similar("364日間", "364日間"))

    def test_slightly_different_texts_within_threshold_match(self):
        self.assertTrue(S.texts_similar("364日間=0契約", "364日間三0契約"))

    def test_completely_different_texts_do_not_match(self):
        self.assertFalse(S.texts_similar("364日間", "全く違う"))

    def test_empty_vs_nonempty_does_not_match(self):
        self.assertFalse(S.texts_similar("", "何か"))
        self.assertFalse(S.texts_similar("何か", ""))


class RunOcrTest(unittest.TestCase):
    """OCRプロセス自体の失敗と「正しく空文字列を読んだ」ことを混同しないことを確認する
    （qc_cuts.pyのrun_ocrと同じ規約。Codexレビューで検出：以前は失敗も空文字列も
    同じ{}/""として扱っていたため、OCR失敗時にtelops.txtへ空行を書いてしまっていた）。
    """

    def setUp(self):
        self._run = S.subprocess.run

    def tearDown(self):
        S.subprocess.run = self._run

    def test_no_paths_returns_empty_and_not_failed(self):
        ocr, failed = S.run_ocr("./ocrcli", [])
        self.assertEqual(ocr, {})
        self.assertFalse(failed)

    def test_parses_successful_output(self):
        class R:
            returncode = 0
            stdout = "a.png\tHello\nb.png\t\n"
        S.subprocess.run = lambda *a, **k: R()
        ocr, failed = S.run_ocr("./ocrcli", ["a.png", "b.png"])
        self.assertEqual(ocr, {"a.png": "Hello", "b.png": ""})
        self.assertFalse(failed)

    def test_nonzero_returncode_is_reported_as_failed(self):
        class R:
            returncode = 1
            stdout = ""
        S.subprocess.run = lambda *a, **k: R()
        ocr, failed = S.run_ocr("./ocrcli", ["a.png"])
        self.assertEqual(ocr, {})
        self.assertTrue(failed)


class ExtractBandPngTest(unittest.TestCase):
    def setUp(self):
        self._run = S.subprocess.run

    def tearDown(self):
        S.subprocess.run = self._run

    def test_raises_when_ffmpeg_fails(self):
        # Arrange: ffmpegが失敗した場合、PNGが無いまま黙って処理を続けない
        def fake_run(cmd, **kwargs):
            raise S.subprocess.CalledProcessError(1, cmd)
        S.subprocess.run = fake_run
        with self.assertRaises(S.subprocess.CalledProcessError):
            S.extract_band_png("src.mp4", 10, 30.0, 860, 210, "out.png")


class MissingOcrResultsTest(unittest.TestCase):
    """OCRプロセスは成功(returncode=0)しても、一部の画像だけ結果行が無い場合がある。
    それを「正しく空文字列を読んだ」と混同しないための欠落検出（Codexレビューで検出）。
    """

    def test_returns_paths_absent_from_ocr_dict(self):
        ocr = {"a.png": "Hello"}
        missing = S.missing_ocr_results(["a.png", "b.png"], ocr)
        self.assertEqual(missing, ["b.png"])

    def test_empty_string_result_is_not_missing(self):
        # 正規にOCRが空文字列を返した場合はkey自体は存在する＝欠落ではない
        ocr = {"a.png": ""}
        missing = S.missing_ocr_results(["a.png"], ocr)
        self.assertEqual(missing, [])


class BisectFirstTrueTest(unittest.TestCase):
    def test_finds_first_frame_where_predicate_becomes_true(self):
        # Arrange: frame>=7 でTrueになる単調な述語
        def check(frame):
            return frame >= 7
        # Act
        result = S.bisect_first_true(check, lo=0, hi=20)
        # Assert
        self.assertEqual(result, 7)

    def test_respects_max_probes_limit(self):
        # Arrange: 呼び出し回数を数え、上限を超えないことを確認する
        calls = []

        def check(frame):
            calls.append(frame)
            return frame >= 1000000
        # Act
        S.bisect_first_true(check, lo=0, hi=2 ** 30, max_probes=5)
        # Assert
        self.assertLessEqual(len(calls), 5)


class RefineSegmentStartTest(unittest.TestCase):
    def test_returns_seg_start_unchanged_when_head_already_matches_center(self):
        # Arrange: フェードインしていない通常のケース（追加OCRの呼び出し回数も検証）
        calls = []

        def ocr_text_at(frame):
            calls.append(frame)
            return "364日間0契約"
        # Act
        result = S.refine_segment_start(ocr_text_at, seg_start=100, seg_mid=110,
                                         center_text="364日間0契約")
        # Assert: 一致しているので二分探索せず1回の呼び出しで確定する
        self.assertEqual(result, 100)
        self.assertEqual(calls, [100])

    def test_bisects_to_find_true_start_when_head_is_faded_in(self):
        # Arrange: 実フェードイン頻出パターン。frame<105は空白（表示しきっていない）、
        # frame>=105から中央と同じテキストになる
        def ocr_text_at(frame):
            return "" if frame < 105 else "364日間0契約"
        # Act
        result = S.refine_segment_start(ocr_text_at, seg_start=100, seg_mid=110,
                                         center_text="364日間0契約")
        # Assert
        self.assertEqual(result, 105)


if __name__ == "__main__":
    unittest.main()
