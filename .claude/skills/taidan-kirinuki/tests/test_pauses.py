# -*- coding: utf-8 -*-
"""scripts/pauses.py の単体テスト（第3段階：common.cut_rangeへの一本化）。
ffmpegを実際に呼ぶ統合テストは含まない（silences_fnはモックする）。
"""
import os
import sys
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import pauses as P  # noqa: E402


class CheckBoundaryTest(unittest.TestCase):
    def test_time_inside_silence_is_ok(self):
        def fake_silences(src, t0, t1):
            return [(9.9, 10.2)]
        ok, extra = P.check_boundary("src.mp4", 10.0, silences_fn=fake_silences)
        self.assertTrue(ok)
        self.assertEqual(extra, (9.9, 10.2))

    def test_time_just_outside_margin_is_still_ok(self):
        def fake_silences(src, t0, t1):
            return [(9.0, 9.99)]
        ok, _ = P.check_boundary("src.mp4", 10.0, silences_fn=fake_silences, margin=0.02)
        self.assertTrue(ok)

    def test_time_far_from_any_silence_reports_neighbors(self):
        def fake_silences(src, t0, t1):
            return [(11.0, 11.5), (8.0, 8.5)]
        ok, (nxt, prv) = P.check_boundary("src.mp4", 10.0, silences_fn=fake_silences)
        self.assertFalse(ok)
        self.assertEqual(nxt, (11.0, 11.5))
        self.assertEqual(prv, (8.0, 8.5))

    def test_no_neighboring_silence_at_all(self):
        def fake_silences(src, t0, t1):
            return []
        ok, (nxt, prv) = P.check_boundary("src.mp4", 10.0, silences_fn=fake_silences)
        self.assertFalse(ok)
        self.assertIsNone(nxt)
        self.assertIsNone(prv)


class FormatResultTest(unittest.TestCase):
    def test_ok_message_includes_silence_range(self):
        msg = P.format_result("C1  IN ", 10.0, True, (9.9, 10.2), "")
        self.assertIn("OK", msg)
        self.assertIn("9.90", msg)
        self.assertIn("10.20", msg)

    def test_caution_message_reports_both_neighbors(self):
        msg = P.format_result("C1  OUT", 10.0, False, ((11.0, 11.5), (8.0, 8.5)), "note")
        self.assertIn("要確認", msg)
        self.assertIn("次の無音 +1.00s", msg)
        self.assertIn("前の無音 -1.50s", msg)
        self.assertIn("note", msg)

    def test_caution_message_with_no_neighbors_says_none_nearby(self):
        msg = P.format_result("C1  OUT", 10.0, False, (None, None), "")
        self.assertIn("近傍に無音なし", msg)


if __name__ == "__main__":
    unittest.main()
