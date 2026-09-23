# -*- coding: utf-8 -*-
"""kirinuki_hooks/records.py の単体テスト。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from kirinuki_hooks import records as R  # noqa: E402

HEADINGS = ("目的", "本数", "尺")


def make_text(*, status="確定", overrides=None, skill_line=None):
    overrides = overrides or {}
    lines = ["# 記録"]
    if skill_line is not None:
        lines.append("skill: %s" % skill_line)
    lines += ["", "状態: %s" % status, ""]
    for heading in HEADINGS:
        lines.append("## %s" % heading)
        lines.append(overrides.get(heading, "記入済み"))
        lines.append("")
    return "\n".join(lines)


class ParseSectionsTest(unittest.TestCase):
    def test_splits_body_by_heading(self):
        text = "## A\nbody-a\n## B\nbody-b\n"
        sections = R.parse_sections(text)
        self.assertEqual(sections["A"], "body-a")
        self.assertEqual(sections["B"], "body-b")

    def test_last_occurrence_of_duplicate_heading_wins(self):
        text = "## A\nfirst\n## A\nsecond\n"
        sections = R.parse_sections(text)
        self.assertEqual(sections["A"], "second")

    def test_heading_name_surrounding_whitespace_is_trimmed(self):
        text = "##  A \nbody\n"
        sections = R.parse_sections(text)
        self.assertIn("A", sections)


class IsSectionEmptyTest(unittest.TestCase):
    def test_blank_body_is_empty(self):
        self.assertTrue(R.is_section_empty("   \n  "))

    def test_html_comment_only_is_empty(self):
        self.assertTrue(R.is_section_empty("<!-- 未定 -->"))

    def test_placeholder_only_is_empty(self):
        for placeholder in ("未定", "TODO", "-"):
            with self.subTest(placeholder=placeholder):
                self.assertTrue(R.is_section_empty(placeholder))

    def test_placeholder_as_substring_is_not_empty(self):
        self.assertFalse(R.is_section_empty("未定の部分あり"))
        self.assertFalse(R.is_section_empty("TODO: 後で書く"))

    def test_real_content_is_not_empty(self):
        self.assertFalse(R.is_section_empty("記入済み"))


class RecordSkillTest(unittest.TestCase):
    def test_returns_value_when_present(self):
        text = "skill: mitchaku-kirinuki\n状態: 確定\n"
        self.assertEqual(R.record_skill(text), "mitchaku-kirinuki")

    def test_returns_none_when_absent(self):
        text = "状態: 確定\n"
        self.assertIsNone(R.record_skill(text))

    def test_accepts_fullwidth_colon(self):
        text = "skill：taidan-kirinuki\n"
        self.assertEqual(R.record_skill(text), "taidan-kirinuki")


class StatusLineCheckTest(unittest.TestCase):
    def test_ok_when_single_matching_value(self):
        ok, problem = R.status_line_check("状態: 確定\n", "確定")
        self.assertTrue(ok)
        self.assertIsNone(problem)

    def test_fails_when_no_status_line(self):
        ok, problem = R.status_line_check("本文だけ\n", "確定")
        self.assertFalse(ok)
        self.assertIn("状態", problem)

    def test_fails_when_value_does_not_match(self):
        ok, _ = R.status_line_check("状態: 未確定\n", "確定")
        self.assertFalse(ok)

    def test_accepts_fullwidth_colon_and_surrounding_whitespace(self):
        ok, _ = R.status_line_check(" 状態 ： 確定 \n", "確定")
        self.assertTrue(ok)

    def test_does_not_partial_match_trailing_text(self):
        ok, _ = R.status_line_check("状態: 確定済み\n", "確定")
        self.assertFalse(ok)

    def test_ignores_status_line_inside_html_comment(self):
        text = "<!-- 状態: 確定 -->\n状態: 未確定\n"
        ok, _ = R.status_line_check(text, "確定")
        self.assertFalse(ok)

    def test_ignores_status_line_inside_code_fence(self):
        text = "```\n状態: 確定\n```\n状態: 未確定\n"
        ok, _ = R.status_line_check(text, "確定")
        self.assertFalse(ok)

    def test_fails_on_conflicting_values(self):
        text = "状態: 確定\n状態: 未確定\n"
        ok, problem = R.status_line_check(text, "確定")
        self.assertFalse(ok)
        self.assertIn("競合", problem)

    def test_duplicate_identical_values_is_not_a_conflict(self):
        text = "状態: 確定\n状態: 確定\n"
        ok, _ = R.status_line_check(text, "確定")
        self.assertTrue(ok)


class ValidateRecordTest(unittest.TestCase):
    def test_passes_when_complete(self):
        text = make_text()
        ok, missing, status_ok, _ = R.validate_record(text, HEADINGS, "確定")
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertTrue(status_ok)

    def test_fails_when_heading_empty(self):
        text = make_text(overrides={"本数": ""})
        ok, missing, _, _ = R.validate_record(text, HEADINGS, "確定")
        self.assertFalse(ok)
        self.assertIn("本数", missing)

    def test_fails_when_status_wrong(self):
        text = make_text(status="未確定")
        ok, _, status_ok, _ = R.validate_record(text, HEADINGS, "確定")
        self.assertFalse(ok)
        self.assertFalse(status_ok)


class RejectionFormatTest(unittest.TestCase):
    """`却下：I-01（理由：…）` 形式の検査（全角/半角の括弧・コロン両対応）。"""

    def test_no_problems_when_no_rejection_lines(self):
        text = "- 採用：I-01\n- 保留・観察中：I-02（発生回数1）\n"
        self.assertEqual(R.find_rejection_problems(text), [])

    def test_well_formed_rejection_is_not_a_problem(self):
        text = "- 却下：I-01（理由：優先度が低い）\n"
        self.assertEqual(R.find_rejection_problems(text), [])

    def test_well_formed_rejection_with_multiple_ids_is_not_a_problem(self):
        text = "- 却下：I-03、I-04、I-05（理由：ヒアリング手順は現状通りでよい）\n"
        self.assertEqual(R.find_rejection_problems(text), [])

    def test_missing_reason_marker_is_a_problem(self):
        text = "- 却下：I-01（優先度が低い）\n"
        problems = R.find_rejection_problems(text)
        self.assertEqual(len(problems), 1)
        self.assertIn("I-01", problems[0])

    def test_empty_reason_is_a_problem(self):
        text = "- 却下：I-01（理由：）\n"
        problems = R.find_rejection_problems(text)
        self.assertEqual(len(problems), 1)

    def test_accepts_fullwidth_and_halfwidth_parens_and_colons(self):
        for line in ("- 却下:I-01(理由:優先度が低い)\n", "- 却下：I-01（理由：優先度が低い）\n"):
            with self.subTest(line=line):
                self.assertEqual(R.find_rejection_problems(line), [])

    def test_mentioning_rejection_in_prose_is_not_flagged(self):
        # 「却下」という語を含むだけの文（却下行ではない）は誤検出しない
        text = "- 詰まった点：前回の却下について再検討した\n"
        self.assertEqual(R.find_rejection_problems(text), [])

    def test_validate_improve_rejections_scopes_to_user_judgement_section(self):
        text = (
            "## 改善提案\n"
            "- 却下について検討中\n"  # 却下で始まらないので対象外
            "## ユーザー判断\n"
            "- 却下：I-01（優先度が低い）\n"  # 理由マーカー無し＝不合格
        )
        ok, problems = R.validate_improve_rejections(text)
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)

    def test_validate_improve_rejections_passes_when_all_well_formed(self):
        text = "## ユーザー判断\n- 却下：I-01（理由：優先度が低い）\n"
        ok, problems = R.validate_improve_rejections(text)
        self.assertTrue(ok)
        self.assertEqual(problems, [])


class ParseQuantLineTest(unittest.TestCase):
    def test_parses_all_fields(self):
        body = "- 定量：本数=4 そのまま使える=2 手直し=2 使えない=0 QC_NG=0 QC誤検出=3 作り直し=1 書き出し秒=90\n"
        fields = R.parse_quant_line(body)
        self.assertEqual(fields, {
            "本数": 4, "そのまま使える": 2, "手直し": 2, "使えない": 0,
            "QC_NG": 0, "QC誤検出": 3, "作り直し": 1, "書き出し秒": 90,
        })

    def test_returns_none_when_no_quant_line(self):
        self.assertIsNone(R.parse_quant_line("- 本数／目的：4本\n"))

    def test_accepts_fullwidth_colon(self):
        fields = R.parse_quant_line("- 定量：本数=1\n")
        self.assertEqual(fields, {"本数": 1})

    def test_ignores_quant_mentioned_in_other_lines(self):
        # 「定量」で始まらない行は対象外（本文中に単語として出てくるだけのケース）
        self.assertIsNone(R.parse_quant_line("- 詰まった点：定量欄の書式を統一した\n"))


if __name__ == "__main__":
    unittest.main()
