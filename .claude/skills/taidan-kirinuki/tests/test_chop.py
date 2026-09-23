# -*- coding: utf-8 -*-
"""scripts/chop.py の単体テスト（第3段階：common.cut_range/window_dbへの一本化）。
ffmpegを実際に呼ぶ統合テストは含まない（db_fnはモックする）。
"""
import os
import sys
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import chop as CH  # noqa: E402


BOUNDS = {"fps": 30.0, "gstart": {}}


def clip(cuts, xfade=None):
    c = {"slug": "s1", "cuts": cuts}
    if xfade is not None:
        c["xfade"] = xfade
    return c


class ClassifyBoundaryTest(unittest.TestCase):
    def test_both_sides_loud_is_a_bad_cut(self):
        self.assertEqual(CH.classify_boundary(-10.0, -10.0), "★ぶつ切り")

    def test_both_sides_quiet_is_ok(self):
        self.assertEqual(CH.classify_boundary(-40.0, -40.0), "OK")

    def test_one_side_near_threshold_is_caution(self):
        # 閾値-26dBに対し margin=3dB以内（-27dB）まで迫っている
        self.assertEqual(CH.classify_boundary(-27.0, -40.0), "△")

    def test_exactly_at_threshold_is_not_yet_flagged_as_bad_cut(self):
        # 閾値ちょうどは「超えた」に含めない（>のみ）
        self.assertEqual(CH.classify_boundary(-26.0, -26.0), "△")


class ClipTimelineTest(unittest.TestCase):
    def test_uses_cut_range_for_every_cut(self):
        cuts = [{"src": (1.0, 2.0)}, {"src": (3.0, 4.0)}]
        tl = CH.clip_timeline({"cuts": cuts}, BOUNDS)
        self.assertEqual(len(tl), 2)
        for (S, E, cut), expected_cut in zip(tl, cuts):
            self.assertEqual(cut, expected_cut)
            self.assertLess(S, E)


class CheckClipTest(unittest.TestCase):
    def test_skips_boundary_that_is_contiguous_with_neighbor(self):
        # Arrange: 2カット目の頭が1カット目の尻とちょうど同じ時刻（地続き）
        cuts = [{"src": (1.0, 2.0)}, {"src": (2.0, 3.0)}]
        c = clip(cuts)

        def loud_everywhere(t0, dur):
            return -5.0
        rows = CH.check_clip(c, BOUNDS, loud_everywhere)
        labels = [(i, lab) for i, lab, *_ in rows]
        # 1カット目のOUTと2カット目のINは地続きなのでスキップされ、報告されない
        self.assertNotIn((0, "OUT"), labels)
        self.assertNotIn((1, "IN "), labels)

    def test_reports_bad_cut_at_a_real_boundary(self):
        cuts = [{"src": (1.0, 2.0)}, {"src": (5.0, 6.0)}]
        c = clip(cuts)

        def loud_everywhere(t0, dur):
            return -5.0
        rows = CH.check_clip(c, BOUNDS, loud_everywhere)
        self.assertTrue(any(flag == "★ぶつ切り" for *_, flag, _ in rows))

    def test_ok_boundary_is_not_reported(self):
        cuts = [{"src": (1.0, 2.0)}]

        def quiet_everywhere(t0, dur):
            return -40.0
        rows = CH.check_clip(clip(cuts), BOUNDS, quiet_everywhere)
        self.assertEqual(rows, [])


class ClipTotalSecondsTest(unittest.TestCase):
    def test_subtracts_xfade_from_first_segment_only(self):
        cuts = [{"src": (0.0, 2.0)}, {"src": (10.0, 11.0)}]
        total_with_xfade = CH.clip_total_seconds(clip(cuts), BOUNDS, xfade=0.35)
        total_without_xfade = CH.clip_total_seconds(clip(cuts), BOUNDS, xfade=0.0)
        self.assertAlmostEqual(total_without_xfade - total_with_xfade, 0.35)


if __name__ == "__main__":
    unittest.main()
