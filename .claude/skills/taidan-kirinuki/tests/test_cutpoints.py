# -*- coding: utf-8 -*-
"""scripts/cutpoints.py（対談）の単体テスト（第3段階・対談スキルへの展開）。
語尾分類自体（classify_ending等）はkirinuki_core.endingsに共通化されており、
mitchaku-kirinukiの既存テストがimport経由で担保している。ここでは対談固有の部分
（telops.txtの読み取り、絶対レベルによる無音判定、gstartスナップ後の時刻を見ること、
--from/--to）を検証する。Whisper CLI・ffmpegを実際に呼ぶ統合テストは含まない。
"""
import os
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import cutpoints as CP  # noqa: E402


def write_telops(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for i, (s, e, text) in enumerate(rows):
            f.write("%3d %8.3f-%8.3f (%4.1fs) %s\n" % (i, s, e, e - s, text))


def loud_samples(rate=16000, seconds=4.0):
    n = int(rate * seconds)
    return tuple((20000 if i % 2 == 0 else -20000) for i in range(n))


def quiet_samples(rate=16000, seconds=4.0):
    return (0,) * int(rate * seconds)


class LoadTelopRowsTest(unittest.TestCase):
    def test_parses_index_time_and_text(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "telops.txt")
            write_telops(path, [(48.382, 50.651, "本当です")])
            rows = CP.load_telop_rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["index"], 0)
        self.assertAlmostEqual(rows[0]["start"], 48.382)
        self.assertAlmostEqual(rows[0]["end"], 50.651)
        self.assertEqual(rows[0]["text"], "本当です")

    def test_sorts_by_start_time(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "telops.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("  1   10.000-  11.000 ( 1.0s) あとから\n")
                f.write("  0    1.000-   2.000 ( 1.0s) さきに\n")
            rows = CP.load_telop_rows(path)
        self.assertEqual([r["text"] for r in rows], ["さきに", "あとから"])


class BuildRowTest(unittest.TestCase):
    def test_both_sides_loud_is_caution(self):
        # Arrange: 終点2.0sの前後がどちらも発話レベル(-26dBFS超)＝谷なし
        samples = loud_samples()
        row = CP.build_row({"start": 0.0, "end": 2.0, "text": "本当です"}, snapped_end=2.0,
                            samples=samples)
        self.assertTrue(row["caution"])
        self.assertIn("終点付近に無音の谷が無い", row["reasons"])

    def test_one_side_quiet_is_not_flagged_for_silence(self):
        # Arrange: 終点2.0sの前後どちらかが無音寄り＝谷あり
        n = int(16000 * 4.0)
        samples = tuple((20000 if (i % 2 == 0 and i < 16000 * 2.0) else 0) for i in range(n))
        row = CP.build_row({"start": 0.0, "end": 2.0, "text": "本当です"}, snapped_end=2.0,
                            samples=samples)
        self.assertNotIn("終点付近に無音の谷が無い", row["reasons"])

    def test_continuing_ending_is_flagged_regardless_of_silence(self):
        samples = quiet_samples()
        row = CP.build_row({"start": 0.0, "end": 2.0, "text": "〜と思うんですよね"}, snapped_end=2.0,
                            samples=samples)
        self.assertTrue(row["caution"])
        self.assertTrue(any("継続形" in r for r in row["reasons"]))

    def test_declarative_with_silence_and_no_gap_has_no_reasons(self):
        samples = quiet_samples()
        row = CP.build_row({"start": 0.0, "end": 2.0, "text": "本当です"}, snapped_end=2.0,
                            samples=samples, whisper_gap=1.0)
        self.assertFalse(row["caution"])

    def test_reports_measured_db_levels_not_a_fabricated_duration(self):
        # Arrange: Codexレビューで検出した回帰。以前は「谷0.12s」のように、実際には
        # 測っていない固定長(直前60ms+直後60ms)の無音区間があるかのように表示していた。
        # 実際に測っているのは直前/直後それぞれのdBFSだけなので、その値をそのまま出す
        samples = quiet_samples()
        row = CP.build_row({"start": 0.0, "end": 2.0, "text": "本当です"}, snapped_end=2.0,
                            samples=samples)
        self.assertNotIn("silence", row)
        self.assertIn("before_db", row)
        self.assertIn("after_db", row)
        self.assertTrue(row["has_silence_gap"])


class FormatRowTest(unittest.TestCase):
    def test_includes_text_and_caution_mark(self):
        samples = loud_samples()
        row = CP.build_row({"start": 0.0, "end": 2.0, "text": "本当です"}, snapped_end=2.0,
                            samples=samples)
        line = CP.format_row(row)
        self.assertIn("本当です", line)
        self.assertIn("要注意", line)

    def test_uses_the_correct_label_for_the_snapped_end_time(self):
        # Arrange: team-leadが指摘した誤字（「切実点」→「実際の切れ目」）の再現防止
        samples = loud_samples()
        row = CP.build_row({"start": 0.0, "end": 2.0, "text": "本当です"}, snapped_end=2.0,
                            samples=samples)
        line = CP.format_row(row)
        self.assertIn("実際の切れ目", line)
        self.assertNotIn("切実点", line)

    def test_does_not_claim_a_fabricated_silence_duration(self):
        samples = quiet_samples()
        row = CP.build_row({"start": 0.0, "end": 2.0, "text": "本当です"}, snapped_end=2.0,
                            samples=samples)
        line = CP.format_row(row)
        self.assertIn("谷あり", line)
        self.assertNotIn("0.12s", line)
        self.assertIn("dB", line)


class MainTest(unittest.TestCase):
    def setUp(self):
        self._load_pcm = CP.C.load_pcm
        self._whisper_gap = CP.whisper_gap_after
        CP.C.load_pcm = lambda src, rate=16000: quiet_samples(rate=rate)

    def tearDown(self):
        CP.C.load_pcm = self._load_pcm
        CP.whisper_gap_after = self._whisper_gap

    def _make_workdir(self, rows, bound=None, gstart=None, fps=30.0):
        workdir = tempfile.mkdtemp()
        write_telops(os.path.join(workdir, "telops.txt"), rows)
        import json
        with open(os.path.join(workdir, "bounds.json"), "w") as f:
            json.dump({"fps": fps, "fps_str": "30/1", "bound": bound or [],
                      "gstart": gstart or {}}, f)
        return workdir

    def test_filters_rows_outside_from_to_window(self):
        workdir = self._make_workdir([(0.0, 2.0, "本当です"), (10.0, 12.0, "そうですね")])
        old_stdout = sys.stdout
        import io
        sys.stdout = io.StringIO()
        try:
            CP.main([  # --from/--toで2番目の行だけに絞る
                "src.mp4", workdir, "--from", "9.0", "--to", "11.0"])
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertIn("そうですね", out)
        self.assertNotIn("本当です", out)

    def test_uses_gstart_snapped_frame_for_the_endpoint(self):
        # Arrange: 終点フレーム(2.0s@30fps=60)がgstartで55にスナップされる
        workdir = self._make_workdir([(0.0, 2.0, "本当です")], gstart={"60": 55})
        old_stdout = sys.stdout
        import io
        sys.stdout = io.StringIO()
        try:
            CP.main(["src.mp4", workdir])
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        expected_snapped = (55 - 0.5) / 30.0
        self.assertIn("%8.3f" % expected_snapped, out)

    def test_from_boundary_is_inclusive(self):
        # Arrange: --fromちょうどの開始時刻を持つ行は含まれる（start < t0 だけを除外する）
        workdir = self._make_workdir([(9.0, 10.0, "本当です"), (0.0, 2.0, "そうですね")])
        old_stdout = sys.stdout
        import io
        sys.stdout = io.StringIO()
        try:
            CP.main(["src.mp4", workdir, "--from", "9.0"])
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertIn("本当です", out)
        self.assertNotIn("そうですね", out)

    def test_to_boundary_is_inclusive(self):
        # Arrange: --toちょうどの開始時刻を持つ行は含まれる（start > t1 だけを除外する）
        workdir = self._make_workdir([(0.0, 2.0, "本当です"), (9.0, 10.0, "そうですね")])
        old_stdout = sys.stdout
        import io
        sys.stdout = io.StringIO()
        try:
            CP.main(["src.mp4", workdir, "--to", "0.0"])
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertIn("本当です", out)
        self.assertNotIn("そうですね", out)

    def test_rows_with_no_telop_text_are_skipped(self):
        # Arrange: 無テロップ行（本文が空。build_segmentsが無音区間もtelops.txtに
        # 書き出すため実際に発生しうる）はcutpoints.pyの候補表には出さない
        workdir = self._make_workdir([(0.0, 2.0, ""), (2.0, 4.0, "本当です")])
        old_stdout = sys.stdout
        import io
        sys.stdout = io.StringIO()
        try:
            CP.main(["src.mp4", workdir])
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertIn("1行中", out)
        self.assertIn("本当です", out)

    def test_whisper_flag_invokes_whisper_gap_after(self):
        workdir = self._make_workdir([(0.0, 2.0, "本当です")])
        calls = []
        CP.whisper_gap_after = lambda *a, **k: calls.append(1) or 1.2
        old_stdout = sys.stdout
        import io
        sys.stdout = io.StringIO()
        try:
            CP.main(["src.mp4", workdir, "--whisper"])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(len(calls), 1)

    def test_without_whisper_flag_does_not_invoke_whisper_gap_after(self):
        workdir = self._make_workdir([(0.0, 2.0, "本当です")])
        calls = []
        CP.whisper_gap_after = lambda *a, **k: calls.append(1) or 1.2
        old_stdout = sys.stdout
        import io
        sys.stdout = io.StringIO()
        try:
            CP.main(["src.mp4", workdir])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(len(calls), 0)


if __name__ == "__main__":
    unittest.main()
