# -*- coding: utf-8 -*-
"""scripts/gen_docs.py の単体テスト（第3段階：common.cut_rangeへの一本化、fps表記の実測化）。
ffmpegを実際に呼ぶ統合テストは含まない。
"""
import os
import sys
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import gen_docs as G  # noqa: E402


BOUNDS_2997 = {"fps": 30000.0 / 1001, "fps_str": "30000/1001", "gstart": {}}
BOUNDS_25 = {"fps": 25.0, "fps_str": "25/1", "gstart": {}}
LAYOUT = {"banner_y": 201, "banner_body_h": 213, "band_w": 1080, "band_h": 594, "band_y": 542,
          "cta_y": 1225}


class CleanTest(unittest.TestCase):
    def test_removes_noise_tokens_and_collapses_whitespace(self):
        self.assertEqual(G.clean("apos  結果が  出た  www."), "結果が 出た")


class LinesBetweenTest(unittest.TestCase):
    def test_dedupes_consecutive_identical_lines(self):
        segs = [(0.0, 1.0), (1.0, 2.0)]
        txt = {0: "同じ本文", 1: "同じ本文"}
        lines = G.lines_between(segs, txt, 0.0, 2.0)
        self.assertEqual(len(lines), 1)

    def test_drops_lines_shorter_than_two_chars(self):
        segs = [(0.0, 1.0)]
        txt = {0: "あ"}
        self.assertEqual(G.lines_between(segs, txt, 0.0, 1.0), [])


class FpsLabelTest(unittest.TestCase):
    def test_formats_2997_fps(self):
        self.assertEqual(G.fps_label(BOUNDS_2997), "29.97fps")

    def test_formats_25_fps(self):
        self.assertEqual(G.fps_label(BOUNDS_25), "25.00fps")


class TimelineTest(unittest.TestCase):
    def test_uses_cut_range_for_each_cut(self):
        clip = {"cuts": [{"src": (1.0, 2.0)}, {"src": (10.0, 11.0)}]}
        tl, total = G.timeline(clip, BOUNDS_2997, default_xfade=0.0)
        self.assertEqual(len(tl), 2)
        self.assertGreater(total, 0)


class BuildDocBodyTest(unittest.TestCase):
    def test_body_reports_measured_fps_not_a_fixed_value(self):
        clip = {"file": "s1.mp4", "cuts": [{"src": (1.0, 2.0)}], "xfade": 0.0}
        tl, total = G.timeline(clip, BOUNDS_25, default_xfade=0.0)
        body = G.build_doc_body(1, clip, tl, total, [], {}, LAYOUT, "SOURCE", BOUNDS_25, default_xfade=0.0)
        joined = "\n".join(body)
        self.assertIn("25.00fps", joined)
        self.assertNotIn("29.97fps", joined)

    def test_body_includes_cut_note_and_telop_lines(self):
        clip = {"file": "s1.mp4", "cuts": [{"src": (1.0, 2.0), "note": "フック"}], "xfade": 0.0}
        tl, total = G.timeline(clip, BOUNDS_2997, default_xfade=0.0)
        segs = [(tl[0][0], tl[0][1])]
        txt = {0: "テロップ本文"}
        body = G.build_doc_body(1, clip, tl, total, segs, txt, LAYOUT, "SOURCE", BOUNDS_2997, default_xfade=0.0)
        joined = "\n".join(body)
        self.assertIn("フック", joined)
        self.assertIn("テロップ本文", joined)

    def test_reports_default_xfade_transition_when_clip_omits_xfade_key(self):
        # Arrange: Codexレビューで検出した回帰。clips_template.pyのCLIPSはxfadeキーを
        # 個別に持たず、モジュール直下のXFADE(既定0.35)に頼る。build_doc_bodyが独自に
        # 0.0をデフォルトにしていたため、実際は白フラッシュがあるのに
        # 「トランジションなし」と誤って表示していた
        clip = {"file": "s1.mp4", "cuts": [{"src": (1.0, 2.0)}, {"src": (10.0, 11.0)}]}
        tl, total = G.timeline(clip, BOUNDS_2997, default_xfade=0.35)
        body = G.build_doc_body(1, clip, tl, total, [], {}, LAYOUT, "SOURCE", BOUNDS_2997, default_xfade=0.35)
        joined = "\n".join(body)
        self.assertIn("白フラッシュ0.35秒", joined)
        self.assertNotIn("なし（元データのアイキャッチが兼ねる）", joined)


if __name__ == "__main__":
    unittest.main()
