# -*- coding: utf-8 -*-
"""scripts/cutpoints.py の単体テスト（第3段階：切り抜きそのものの精度）。
Whisper CLI・ffmpegを実際に呼ぶ統合テストは含まない（実データでの動作確認は
別途手動で行い、結果を報告する）。
"""
import os
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import cutpoints as CP  # noqa: E402


def telop_row(start, end, fa, fb, text):
    return {"start": start, "end": end, "frame_start": fa, "frame_end": fb, "text": text}


class LoadTelopRowsTest(unittest.TestCase):
    def test_parses_frame_numbers_and_text(self):
        # Arrange
        content = " 198.965  202.102 f  5963-  6057 どうしても数字が出ない時とか／絶対あるので\n"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = f.name
        try:
            # Act
            rows = CP.load_telop_rows(path)
            # Assert
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["frame_start"], 5963)
            self.assertEqual(rows[0]["frame_end"], 6057)
            self.assertEqual(rows[0]["text"], "どうしても数字が出ない時とか／絶対あるので")
        finally:
            os.unlink(path)

    def test_sorts_rows_by_start_time(self):
        content = (
            " 10.000   11.000 f   300-   330 あとから\n"
            "  1.000    2.000 f    30-    60 さきに\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = f.name
        try:
            rows = CP.load_telop_rows(path)
            self.assertEqual([r["text"] for r in rows], ["さきに", "あとから"])
        finally:
            os.unlink(path)


class ClassifyEndingTest(unittest.TestCase):
    def test_node_suffix_is_continuing_form(self):
        # Arrange: 実データの罠1「〜ので」で終える継続形
        self.assertEqual(CP.classify_ending("どうしても数字が出ない時とか／絶対あるので"), "継続形")

    def test_shi_suffix_is_continuing_form(self):
        # Arrange: 実データの罠2「〜し」で終える継続形
        self.assertEqual(CP.classify_ending("そこからでも／ずっとチャットは動き続けるし"), "継続形")

    def test_omou_ndesu_yo_is_continuing_form_even_though_it_ends_in_yo(self):
        # 「〜と思うんですよ」は表面上「よ」で終わる言い切り風だが、実際は段落途中という
        # SKILL.md記載の罠。「よ」より先に判定することを確認する
        self.assertEqual(CP.classify_ending("この環境が最高だと思うんですよ"), "継続形")

    def test_tte_suffix_is_quotation_style(self):
        # Arrange: 実データの罠3「〜って」で終える引用止め・テ形止め
        self.assertEqual(CP.classify_ending("営業って"), "引用止め")

    def test_suruzo_suffix_is_quotation_style(self):
        self.assertEqual(CP.classify_ending("絶対に成功するぞ"), "引用止め")

    def test_desu_suffix_is_declarative(self):
        self.assertEqual(CP.classify_ending("本当です"), "言い切り")

    def test_question_mark_is_declarative(self):
        self.assertEqual(CP.classify_ending("本当なんですか？"), "言い切り")

    def test_unknown_ending_returns_unknown_label(self):
        self.assertEqual(CP.classify_ending("読み取れない断片"), "不明")

    def test_empty_text_is_no_telop_label(self):
        self.assertEqual(CP.classify_ending(""), "不明（無テロップ）")

    def test_uses_last_segment_when_multiline(self):
        # 「／」で複数行のテロップは、カット終点に近い最後の行だけで判定する
        self.assertEqual(CP.classify_ending("最初の行です／最後はので"), "継続形")


class ParseSilencesTest(unittest.TestCase):
    def test_pairs_start_and_end_tokens(self):
        text = (
            "[silencedetect] silence_start: 1.5\n"
            "[silencedetect] silence_end: 1.9 | silence_duration: 0.4\n"
        )
        self.assertEqual(CP.parse_silences(text), [(1.5, 1.9)])

    def test_survives_progress_output_merged_into_the_same_line(self):
        # 実データで観測された、frame=...の進捗ログがsilence_endと同じ物理行に混ざるケース
        text = (
            "silence_start: 828.431451\n"
            "frame=24921 fps=471 speed=15.7x    [silencedetect] silence_end: 829.855238 | "
            "silence_duration: 1.423787\n"
        )
        self.assertEqual(CP.parse_silences(text), [(828.431451, 829.855238)])

    def test_ignores_dangling_start_without_matching_end(self):
        text = "silence_start: 5.0\n"
        self.assertEqual(CP.parse_silences(text), [])


class EnsureSilenceFileTest(unittest.TestCase):
    """ffmpegが失敗したときにキャッシュファイルを作らないことを確認する
    （Codexレビューで検出：以前はreturncodeを見ておらず、失敗しても空/不完全な
    ファイルを書いてしまい、次回以降そのまま「生成済み」として再生成されなかった）。
    """

    def setUp(self):
        self._run = CP.subprocess.run

    def tearDown(self):
        CP.subprocess.run = self._run

    def test_raises_and_does_not_write_file_when_ffmpeg_fails(self):
        class R:
            returncode = 1
            stderr = ""
        CP.subprocess.run = lambda *a, **k: R()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "silence30.txt")
            with self.assertRaises(RuntimeError):
                CP.ensure_silence_file(path, "missing.mp4")
            self.assertFalse(os.path.exists(path))

    def test_writes_file_when_ffmpeg_succeeds(self):
        class R:
            returncode = 0
            stderr = "silence_start: 1.0\nsilence_end: 1.5 | silence_duration: 0.5\n"
        CP.subprocess.run = lambda *a, **k: R()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "silence30.txt")
            CP.ensure_silence_file(path, "src.mp4")
            self.assertTrue(os.path.exists(path))


class FindSilenceNearTest(unittest.TestCase):
    def test_finds_interval_covering_time_within_tolerance(self):
        silences = [(830.73, 831.32)]
        hit = CP.find_silence_near(silences, 831.097)
        self.assertEqual(hit, (830.73, 831.32, 831.32 - 830.73))

    def test_returns_none_when_time_is_far_from_any_interval(self):
        silences = [(830.73, 831.32)]
        self.assertIsNone(CP.find_silence_near(silences, 900.0))

    def test_tolerance_extends_slightly_beyond_interval_edges(self):
        # Arrange: 境界のわずか外側でも許容範囲内ならヒットする
        silences = [(10.0, 10.5)]
        # Act / Assert
        self.assertIsNotNone(CP.find_silence_near(silences, 10.5 + 0.03, tolerance=0.05))
        self.assertIsNone(CP.find_silence_near(silences, 10.5 + 0.5, tolerance=0.05))


class CautionReasonsTest(unittest.TestCase):
    def test_declarative_with_silence_and_no_gap_has_no_reasons(self):
        reasons = CP.caution_reasons("言い切り", (1.0, 1.5, 0.5), gap=1.0)
        self.assertEqual(reasons, [])

    def test_non_declarative_ending_is_flagged(self):
        reasons = CP.caution_reasons("継続形", (1.0, 1.5, 0.5), gap=1.0)
        self.assertIn("語尾が継続形で終えている", reasons)

    def test_missing_silence_is_flagged(self):
        reasons = CP.caution_reasons("言い切り", None, gap=1.0)
        self.assertIn("終点付近に無音の谷が無い", reasons)

    def test_short_gap_is_flagged(self):
        reasons = CP.caution_reasons("言い切り", (1.0, 1.5, 0.5), gap=0.1)
        self.assertTrue(any("次の発話が始まっている" in r for r in reasons))

    def test_gap_none_is_not_flagged_as_short(self):
        # Whisper未使用（gap=None）のときは、間隔の短さでは要注意にしない
        reasons = CP.caution_reasons("言い切り", (1.0, 1.5, 0.5), gap=None)
        self.assertEqual(reasons, [])


class BuildRowTest(unittest.TestCase):
    def test_ok_row_has_no_reasons(self):
        row = CP.build_row(telop_row(48.0, 50.0, 1440, 1500, "本当です"), [(49.9, 50.1)])
        self.assertFalse(row["caution"])
        self.assertEqual(row["reasons"], [])

    def test_caution_row_reports_missing_silence(self):
        # Arrange: 実データの罠「本当です」（言い切りだが終点に無音が無い）を再現
        row = CP.build_row(telop_row(48.382, 50.651, 1451, 1520, "本当です"), [])
        # Assert
        self.assertTrue(row["caution"])
        self.assertIn("終点付近に無音の谷が無い", row["reasons"])

    def test_format_row_includes_frame_range_and_text(self):
        row = CP.build_row(telop_row(48.382, 50.651, 1451, 1520, "本当です"), [])
        line = CP.format_row(row)
        self.assertIn("1451", line)
        self.assertIn("1520", line)
        self.assertIn("本当です", line)
        self.assertIn("要注意", line)


if __name__ == "__main__":
    unittest.main()
