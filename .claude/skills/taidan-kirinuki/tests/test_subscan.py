# -*- coding: utf-8 -*-
"""scripts/subscan.py の単体テスト（第3段階：fps自動化・VFR検出）。
ffmpeg/ffprobeを実際に呼ぶ統合テストは含まない。
"""
import json
import os
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import subscan as S  # noqa: E402


class FrameDiffTest(unittest.TestCase):
    def test_identical_frames_have_zero_diff(self):
        frame = bytes([100, 100, 100])
        self.assertEqual(S.frame_diff(frame, frame, frame_size=3, sample_stride=1), 0)

    def test_detects_difference_between_frames(self):
        a = bytes([0, 0, 0])
        b = bytes([255, 255, 255])
        self.assertEqual(S.frame_diff(b, a, frame_size=3, sample_stride=1), 255.0)


class GroupChangepointsTest(unittest.TestCase):
    def test_groups_nearby_indices(self):
        groups = S.group_changepoints([10, 11, 12, 50], max_gap=3)
        self.assertEqual(groups, [(10, 12), (50, 50)])

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(S.group_changepoints([]), [])

    def test_respects_max_gap_boundary(self):
        # gapがちょうどmax_gap+1なら別グループ
        groups = S.group_changepoints([10, 14], max_gap=3)
        self.assertEqual(groups, [(10, 10), (14, 14)])


class ScanBandDiffsFailureTest(unittest.TestCase):
    """ffmpegの終了コードを見ずbounds.jsonを上書きしていた回帰の修正
    （Codexレビューで検出）。走査失敗時・0フレーム読めなかった時は例外を送出し、
    呼び出し側（main）がbounds.jsonを書く前に止まるようにする。
    """

    def setUp(self):
        self._popen = S.subprocess.Popen

    def tearDown(self):
        S.subprocess.Popen = self._popen

    class _FakeStderr:
        def read(self):
            return b"error detail"

    class _FakePopen:
        def __init__(self, chunks, returncode):
            self._chunks = list(chunks)
            self.returncode = returncode
            self.stdout = self
            self.stderr = ScanBandDiffsFailureTest._FakeStderr()

        def read(self, n):
            if self._chunks:
                return self._chunks.pop(0)
            return b""

        def close(self):
            pass

        def wait(self):
            return self.returncode

    def test_raises_when_ffmpeg_exits_nonzero(self):
        # Arrange: 1フレーム分だけ読めてからffmpegが失敗終了する
        frame = bytes(S.W * S.H)
        S.subprocess.Popen = lambda *a, **k: self._FakePopen([frame, frame], returncode=1)
        with self.assertRaises(RuntimeError):
            S.scan_band_diffs("src.mp4", 900, 170, 8.0)

    def test_raises_when_zero_frames_are_read(self):
        # Arrange: ffmpeg自体は成功終了(0)するが、1フレームも読めなかった
        S.subprocess.Popen = lambda *a, **k: self._FakePopen([], returncode=0)
        with self.assertRaises(RuntimeError):
            S.scan_band_diffs("src.mp4", 900, 170, 8.0)

    def test_succeeds_when_frames_are_read_and_exit_is_zero(self):
        frame = bytes(S.W * S.H)
        S.subprocess.Popen = lambda *a, **k: self._FakePopen([frame, frame, frame], returncode=0)
        raw, n = S.scan_band_diffs("src.mp4", 900, 170, 8.0)
        self.assertEqual(n, 3)


class CheckVariableFrameRateTest(unittest.TestCase):
    def setUp(self):
        self._get_rates = S.get_stream_rates

    def tearDown(self):
        S.get_stream_rates = self._get_rates

    def test_constant_frame_rate_has_no_warning(self):
        S.get_stream_rates = lambda src: ("30000/1001", "30000/1001")
        warning, fps_str = S.check_variable_frame_rate("src.mp4")
        self.assertIsNone(warning)
        self.assertEqual(fps_str, "30000/1001")

    def test_variable_frame_rate_returns_warning(self):
        S.get_stream_rates = lambda src: ("30000/1001", "24000/1001")
        warning, fps_str = S.check_variable_frame_rate("src.mp4")
        self.assertIsNotNone(warning)
        self.assertIn("可変フレームレート", warning)


class MainTest(unittest.TestCase):
    """main()がbounds.jsonにfps/fps_strを書くこと・VFRで終了コード1になることを確認する
    （ffmpegのフレーム走査自体はscan_band_diffsをモックして切り離す）。
    """

    def setUp(self):
        self._get_rates = S.get_stream_rates
        self._scan = S.scan_band_diffs

    def tearDown(self):
        S.get_stream_rates = self._get_rates
        S.scan_band_diffs = self._scan

    def test_writes_fps_and_fps_str_to_bounds_json(self):
        S.get_stream_rates = lambda src: ("25/1", "25/1")
        S.scan_band_diffs = lambda src, y, h, th: ([10, 11, 50], 100)
        with tempfile.TemporaryDirectory() as workdir:
            rc = S.main(["src.mp4", workdir])
            self.assertEqual(rc, 0)
            with open(os.path.join(workdir, "bounds.json")) as f:
                bounds = json.load(f)
        self.assertEqual(bounds["fps_str"], "25/1")
        self.assertAlmostEqual(bounds["fps"], 25.0)
        self.assertEqual(bounds["bound"], [11, 50])
        # 総フレーム数(subtext.build_segmentsが最後の区間を作るのに使う。
        # team-leadが合成素材で確認：最後の境界より後ろのテロップが欠落するバグの修正)
        self.assertEqual(bounds["frames"], 100)

    def test_returns_exit_code_1_for_variable_frame_rate_material(self):
        S.get_stream_rates = lambda src: ("30000/1001", "24000/1001")

        def fail_scan(*a, **k):
            raise AssertionError("VFR素材ではフレーム走査に進んではいけない")
        S.scan_band_diffs = fail_scan
        with tempfile.TemporaryDirectory() as workdir:
            rc = S.main(["src.mp4", workdir])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
