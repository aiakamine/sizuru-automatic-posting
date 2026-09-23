# -*- coding: utf-8 -*-
"""scripts/render.py の単体テスト（第3段階：出力fpsをbounds.jsonのfpsに合わせる）。
ffmpegを実際に呼ぶ統合テストは含まない（run_fnはモックする）。
"""
import os
import sys
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import render as R  # noqa: E402


BOUNDS_2997 = {"fps": 30000.0 / 1001, "fps_str": "30000/1001", "gstart": {}}
BOUNDS_25 = {"fps": 25.0, "fps_str": "25/1", "gstart": {}}
LAYOUT = {"banner_x": 0, "banner_y": 201, "band_w": 1080, "band_h": 594, "band_y": 542,
          "cta_x": 0, "cta_y": 1225}
DEFAULT_CROP = (1920, 1056, 0, 12)


def make_clip(cuts, xfade=0.0, outfade=0.30):
    return {"slug": "s1", "file": "s1.mp4", "cuts": cuts, "xfade": xfade, "outfade": outfade}


class TimelineTest(unittest.TestCase):
    def test_uses_cut_range_and_accumulates_offsets(self):
        clip = make_clip([{"src": (1.0, 2.0)}, {"src": (10.0, 11.0)}])
        tl, total = R.timeline(clip, BOUNDS_2997, default_xfade=0.0)
        self.assertEqual(len(tl), 2)
        self.assertAlmostEqual(tl[1][3], tl[0][2])  # 2カット目の開始オフセット=1カット目の尺
        self.assertGreater(total, 0)

    def test_xfade_shortens_total_by_overlap(self):
        clip_no_xf = make_clip([{"src": (1.0, 2.0)}, {"src": (10.0, 11.0)}], xfade=0.0)
        clip_xf = make_clip([{"src": (1.0, 2.0)}, {"src": (10.0, 11.0)}], xfade=0.35)
        _, total_no_xf = R.timeline(clip_no_xf, BOUNDS_2997, default_xfade=0.0)
        _, total_xf = R.timeline(clip_xf, BOUNDS_2997, default_xfade=0.0)
        self.assertAlmostEqual(total_no_xf - total_xf, 0.35)


class BuildFilterComplexTest(unittest.TestCase):
    def _fc_for(self, bounds):
        clip = make_clip([{"src": (1.0, 2.0)}])
        tl, total = R.timeline(clip, bounds, default_xfade=0.0)
        return R.build_filter_complex(tl, total, clip, bounds, LAYOUT, DEFAULT_CROP, xfade=0.0)

    def test_includes_bounds_fps_str_in_the_scale_filter(self):
        fc = self._fc_for(BOUNDS_2997)
        self.assertTrue(any("fps=30000/1001" in line for line in fc))

    def test_uses_25fps_when_bounds_say_so(self):
        fc = self._fc_for(BOUNDS_25)
        self.assertTrue(any("fps=25/1" in line for line in fc))
        self.assertFalse(any("fps=30000/1001" in line for line in fc))

    def test_xfade_adds_crossfade_filters(self):
        clip = make_clip([{"src": (1.0, 2.0)}, {"src": (10.0, 11.0)}], xfade=0.35)
        tl, total = R.timeline(clip, BOUNDS_2997, default_xfade=0.0)
        fc = R.build_filter_complex(tl, total, clip, BOUNDS_2997, LAYOUT, DEFAULT_CROP, xfade=0.35)
        self.assertTrue(any("xfade=transition=fadewhite" in line for line in fc))
        self.assertTrue(any("acrossfade" in line for line in fc))

    def test_no_xfade_has_no_crossfade_filters(self):
        fc = self._fc_for(BOUNDS_2997)
        self.assertFalse(any("xfade=transition" in line for line in fc))


class BuildRenderCommandTest(unittest.TestCase):
    def test_uses_bounds_fps_str_for_output_framerate(self):
        cmd = R.build_render_command("src.mp4", "cta.mp4", "banner.mov", ["[0:v]copy[vout]"],
                                      10.0, BOUNDS_2997, "out.mp4")
        self.assertEqual(cmd[cmd.index("-r") + 1], "30000/1001")
        self.assertIn("color=c=black:s=1080x1920:r=30000/1001", cmd)

    def test_uses_25fps_string_when_bounds_are_25fps(self):
        cmd = R.build_render_command("src.mp4", "cta.mp4", "banner.mov", ["[0:v]copy[vout]"],
                                      10.0, BOUNDS_25, "out.mp4")
        self.assertEqual(cmd[cmd.index("-r") + 1], "25/1")
        self.assertIn("color=c=black:s=1080x1920:r=25/1", cmd)

    def test_output_path_is_last_argument(self):
        cmd = R.build_render_command("src.mp4", "cta.mp4", "banner.mov", ["[0:v]copy[vout]"],
                                      10.0, BOUNDS_2997, "out.mp4")
        self.assertEqual(cmd[-1], "out.mp4")


if __name__ == "__main__":
    unittest.main()
