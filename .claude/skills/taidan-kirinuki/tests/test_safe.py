# -*- coding: utf-8 -*-
"""scripts/safe.py の単体テスト（第3段階：固定fpsをやめ、bounds.jsonのfpsを使う）。
ffmpegを実際に呼ぶ統合テストは含まない。
"""
import os
import sys
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import safe as SF  # noqa: E402


class BlockMediansTest(unittest.TestCase):
    def test_computes_median_per_block(self):
        env = [1, 2, 3] + [10, 20, 30]
        meds = SF.block_medians(env, block=3)
        self.assertEqual(meds, [2, 20])


class LocalMedianAtTest(unittest.TestCase):
    def test_returns_block_value_at_block_start(self):
        meds = [2.0, 20.0]
        self.assertAlmostEqual(SF.local_median_at(meds, 0, block=3), 2.0)

    def test_interpolates_between_blocks(self):
        meds = [0.0, 10.0]
        # i=block*0.5 -> ちょうど中間
        self.assertAlmostEqual(SF.local_median_at(meds, 1, block=2), 5.0)

    def test_clamps_to_last_block_beyond_range(self):
        meds = [1.0, 2.0]
        self.assertAlmostEqual(SF.local_median_at(meds, 100, block=3), 2.0)


class ComputeSilencesTest(unittest.TestCase):
    def test_detects_a_quiet_stretch(self):
        # 中央値10（大多数が10）に対しratio=0.55なら閾値5.5。真ん中の1が続く区間は
        # 閾値未満なので無音として検出される
        env = [10] * 20 + [1] * 10 + [10] * 20
        sil = SF.compute_silences(env, SF.block_medians(env, block=len(env)),
                                   rate=1000, step=1, ratio=0.55, minlen=0.005)
        self.assertEqual(len(sil), 1)

    def test_no_quiet_stretch_when_all_loud(self):
        env = [10] * 30
        sil = SF.compute_silences(env, SF.block_medians(env, block=len(env)))
        self.assertEqual(sil, [])


class ComputeSafeRowsTest(unittest.TestCase):
    def test_frame_landing_inside_silence_is_safe(self):
        fps = 30.0
        bound = [90]  # t = (90-0.5)/30 = 2.9833...
        t = (90 - 0.5) / fps
        silences = [(t - 0.01, t + 0.01)]
        rows = SF.compute_safe_rows(bound, fps, silences, {})
        self.assertEqual(len(rows), 1)
        f, tt, ok, body = rows[0]
        self.assertTrue(ok)

    def test_frame_far_from_silence_is_not_safe(self):
        fps = 30.0
        bound = [90]
        rows = SF.compute_safe_rows(bound, fps, [(1000.0, 1000.5)], {})
        self.assertFalse(rows[0][2])

    def test_uses_the_given_fps_not_a_fixed_constant(self):
        # Arrange: 同じフレーム番号でも25fpsと29.97fpsで違う時刻になる
        bound = [90]
        rows_25 = SF.compute_safe_rows(bound, 25.0, [], {})
        rows_2997 = SF.compute_safe_rows(bound, 30000.0 / 1001, [], {})
        self.assertNotAlmostEqual(rows_25[0][1], rows_2997[0][1])

    def test_matches_closest_telop_text_within_tolerance(self):
        fps = 30.0
        bound = [90]
        t = (90 - 0.5) / fps
        telop = {t: "本文"}
        rows = SF.compute_safe_rows(bound, fps, [], telop)
        self.assertEqual(rows[0][3], "本文")

    def test_telop_text_beyond_tolerance_is_blank(self):
        fps = 30.0
        bound = [90]
        telop = {9999.0: "遠い本文"}
        rows = SF.compute_safe_rows(bound, fps, [], telop)
        self.assertEqual(rows[0][3], "")


class FormatRowTest(unittest.TestCase):
    def test_safe_row_shows_safe_label(self):
        line = SF.format_row(90, 2.983, True, "本文")
        self.assertIn("SAFE", line)
        self.assertIn("本文", line)

    def test_unsafe_row_shows_dashes(self):
        line = SF.format_row(90, 2.983, False, "")
        self.assertIn("----", line)


if __name__ == "__main__":
    unittest.main()
