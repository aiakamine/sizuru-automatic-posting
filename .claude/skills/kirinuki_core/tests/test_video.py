# -*- coding: utf-8 -*-
"""kirinuki_core.video の単体テスト。
移しただけの関数（parse_frame_rate等）はmitchaku-kirinukiの既存テストが
import経由で担保する。ここでは第3段階の対談展開で新設した3関数
（get_stream_rates・is_variable_frame_rate・frame_index）だけを検証する。
ffmpeg/ffprobeを実際に呼ぶ統合テストは含まない。
"""
import os
import sys
import unittest

CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(CORE_DIR))
from kirinuki_core import video as V  # noqa: E402


class GetStreamRatesTest(unittest.TestCase):
    def setUp(self):
        self._run = V.subprocess.run

    def tearDown(self):
        V.subprocess.run = self._run

    def test_calls_ffprobe_once_and_returns_both_rates(self):
        class R:
            stdout = "30000/1001\n30000/1001\n"
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return R()
        V.subprocess.run = fake_run
        r_rate, avg_rate = V.get_stream_rates("src.mp4")
        self.assertEqual(r_rate, "30000/1001")
        self.assertEqual(avg_rate, "30000/1001")
        self.assertIn("ffprobe", captured["cmd"])
        self.assertIn("src.mp4", captured["cmd"])
        self.assertIn("stream=r_frame_rate,avg_frame_rate", captured["cmd"])

    def test_returns_differing_rates_when_variable(self):
        class R:
            stdout = "30000/1001\n2997000/100066\n"
        V.subprocess.run = lambda cmd, **kwargs: R()
        r_rate, avg_rate = V.get_stream_rates("src.mp4")
        self.assertEqual(r_rate, "30000/1001")
        self.assertEqual(avg_rate, "2997000/100066")


class IsVariableFrameRateTest(unittest.TestCase):
    def test_identical_rates_are_not_variable(self):
        self.assertFalse(V.is_variable_frame_rate("30000/1001", "30000/1001"))

    def test_equivalent_fractions_are_not_variable(self):
        # 表記は違っても実質同じfps（約分すれば一致）なら可変とはみなさない
        self.assertFalse(V.is_variable_frame_rate("30/1", "30000/1000"))

    def test_clearly_different_rates_are_variable(self):
        self.assertTrue(V.is_variable_frame_rate("30000/1001", "24000/1001"))

    def test_difference_within_tolerance_is_not_variable(self):
        # 丸め誤差程度のわずかな差は許容する（30000/1001≈29.9700, 29971/1000=29.9710）
        self.assertFalse(V.is_variable_frame_rate("30000/1001", "29971/1000", tol=0.01))

    def test_difference_beyond_tolerance_is_variable(self):
        self.assertTrue(V.is_variable_frame_rate("30.0", "29.5", tol=0.01))


class FrameIndexTest(unittest.TestCase):
    def test_rounds_to_nearest_frame(self):
        self.assertEqual(V.frame_index(1.0, 30.0), 30)

    def test_rounds_half_up_like_frame_seek_time_counterpart(self):
        fps = 30000 / 1001
        # 811.177s 付近の実データに近い値で、四捨五入が素直に効くことを確認する
        self.assertEqual(V.frame_index(24311 / fps, fps), 24311)

    def test_zero_time_is_frame_zero(self):
        self.assertEqual(V.frame_index(0.0, 30.0), 0)


if __name__ == "__main__":
    unittest.main()
