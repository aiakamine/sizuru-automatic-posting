# -*- coding: utf-8 -*-
"""scripts/qc.py の単体テスト（第3段階：common.cut_rangeへの一本化）。
ffmpeg/OCRを実際に呼ぶ統合テストは含まない。
"""
import os
import sys
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import qc as QC  # noqa: E402


BOUNDS = {"fps": 30.0, "gstart": {}}


class SegAtTest(unittest.TestCase):
    def test_finds_segment_covering_time(self):
        segs = [(0.0, 1.0), (1.0, 2.0)]
        i, text = QC.seg_at(segs, {1: "本文"}, 1.5)
        self.assertEqual(i, 1)
        self.assertEqual(text, "本文")

    def test_time_outside_any_segment_returns_none_index(self):
        segs = [(0.0, 1.0)]
        i, text = QC.seg_at(segs, {}, 5.0)
        self.assertIsNone(i)
        self.assertEqual(text, "")


class NormTest(unittest.TestCase):
    def test_removes_all_whitespace(self):
        self.assertEqual(QC.norm("結果 が\t出た\n"), "結果が出た")


class JudgeTest(unittest.TestCase):
    def test_matching_prefix_is_ok(self):
        self.assertTrue(QC.judge("結果が出るみたいな人って", "結果が出るみたいな"))

    def test_empty_expected_is_always_ok(self):
        self.assertTrue(QC.judge("", "何か映っている"))

    def test_completely_different_text_is_ng(self):
        self.assertFalse(QC.judge("結果が出るみたいな人って", "全く関係ない文字列です"))


class TimelineTest(unittest.TestCase):
    def test_uses_cut_range_and_accumulates_offsets(self):
        clip = {"cuts": [{"src": (1.0, 2.0)}, {"src": (10.0, 11.0)}]}
        tl = QC.timeline(clip, BOUNDS, default_xfade=0.0)
        self.assertEqual(len(tl), 2)
        S0, E0, D0, t0_0, _ = tl[0]
        S1, E1, D1, t0_1, _ = tl[1]
        self.assertAlmostEqual(t0_0, 0.0)
        self.assertAlmostEqual(t0_1, D0)
        self.assertAlmostEqual(D0, E0 - S0)


class ChecksForCutTest(unittest.TestCase):
    def test_no_xfade_checks_head_and_tail(self):
        kinds = [kind for _, kind in QC.checks_for_cut(0, xfade=0.0)]
        self.assertEqual(kinds, ["head", "head+", "tail"])

    def test_xfade_first_cut_skips_tail(self):
        kinds = [kind for _, kind in QC.checks_for_cut(0, xfade=0.35)]
        self.assertNotIn("tail", kinds)

    def test_xfade_second_cut_only_checks_tail(self):
        kinds = [kind for _, kind in QC.checks_for_cut(1, xfade=0.35)]
        self.assertEqual(kinds, ["tail"])

    def test_third_cut_with_xfade_checks_everything(self):
        kinds = [kind for _, kind in QC.checks_for_cut(2, xfade=0.35)]
        self.assertEqual(kinds, ["head", "head+", "tail"])


class BuildJobsTest(unittest.TestCase):
    def test_generates_one_job_per_check_point(self):
        clip = {"slug": "s1", "cuts": [{"src": (1.0, 2.0)}]}
        jobs = QC.build_jobs(clip, BOUNDS, default_xfade=0.0, out_dir="/out", tmp_dir="/tmp/qc")
        kinds = [j[4] for j in jobs]
        self.assertEqual(kinds, ["head", "head+", "tail"])
        for ot, p, slug, i, kind, srct in jobs:
            self.assertEqual(slug, "s1")
            self.assertEqual(i, 0)
            self.assertTrue(p.startswith("/tmp/qc/s1_c00_"))


if __name__ == "__main__":
    unittest.main()
