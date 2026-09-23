# -*- coding: utf-8 -*-
"""scripts/learnings_stats.py の単体テスト。
references/learnings.md（＋あれば learnings-archive.md）の定量欄を集計する。
"""
import os
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import learnings_stats as LS  # noqa: E402

DUMMY_LEARNINGS = """# 改善ログ — taidan-kirinuki

## 書き方

```
## YYYY-MM-DD 素材タイトル（URL）
- 定量：本数=4 そのまま使える=2 手直し=2 使えない=0 QC_NG=0 QC誤検出=3 作り直し=1 書き出し秒=90
```

## 記録

## 2026-09-15 3回目の素材（https://example.com/3）
- 定量：本数=5 そのまま使える=4 手直し=1 使えない=0 QC_NG=0 QC誤検出=0 作り直し=0 書き出し秒=60
- 本数／目的：5本

## 2026-09-08 2回目の素材（https://example.com/2）
- 定量：本数=4 そのまま使える=2 手直し=2 使えない=0 QC_NG=1 QC誤検出=2 作り直し=1 書き出し秒=90
- 本数／目的：4本

## 2026-09-01 1回目の素材（https://example.com/1）
- 本数／目的：3本（定量欄なし。旧形式）
"""


class ParseEntriesTest(unittest.TestCase):
    def test_extracts_dated_entries_only(self):
        entries = LS.parse_entries(DUMMY_LEARNINGS)
        dates = [e["date"] for e in entries]
        self.assertEqual(dates, ["2026-09-15", "2026-09-08", "2026-09-01"])
        # "書き方" "記録" のような見出しは対象外
        self.assertNotIn("書き方", [e["title"] for e in entries])

    def test_quant_is_none_when_missing(self):
        entries = LS.parse_entries(DUMMY_LEARNINGS)
        oldest = [e for e in entries if e["date"] == "2026-09-01"][0]
        self.assertIsNone(oldest["quant"])

    def test_quant_is_parsed_when_present(self):
        entries = LS.parse_entries(DUMMY_LEARNINGS)
        newest = [e for e in entries if e["date"] == "2026-09-15"][0]
        self.assertEqual(newest["quant"]["本数"], 5)
        self.assertEqual(newest["quant"]["そのまま使える"], 4)


class LoadEntriesTest(unittest.TestCase):
    def test_reads_multiple_files_and_skips_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_a = os.path.join(tmp, "learnings.md")
            path_b = os.path.join(tmp, "learnings-archive.md")
            with open(path_a, "w", encoding="utf-8") as f:
                f.write(DUMMY_LEARNINGS)
            with open(path_b, "w", encoding="utf-8") as f:
                f.write("## 2025-01-01 昔の素材\n- 定量：本数=1 そのまま使える=1\n")
            missing = os.path.join(tmp, "does-not-exist.md")
            entries = LS.load_entries([path_a, path_b, missing])
            self.assertEqual(len(entries), 4)


class FormatReportTest(unittest.TestCase):
    def test_reports_count_with_and_without_quant(self):
        entries = LS.parse_entries(DUMMY_LEARNINGS)
        report = LS.format_report(entries)
        self.assertIn("3件", report)
        self.assertIn("定量あり 2件", report)
        self.assertIn("定量なし 1件", report)

    def test_includes_as_is_rate_for_each_entry_with_quant(self):
        entries = LS.parse_entries(DUMMY_LEARNINGS)
        report = LS.format_report(entries)
        # 2026-09-15: 4/5=80%
        self.assertIn("2026-09-15", report)
        self.assertIn("80%", report)

    def test_empty_entries_does_not_crash(self):
        report = LS.format_report([])
        self.assertIn("0件", report)


class MainCliTest(unittest.TestCase):
    def test_main_prints_report_for_given_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "learnings.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(DUMMY_LEARNINGS)
            import io
            out = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = out
            try:
                code = LS.main(["--file", path])
            finally:
                sys.stdout = old_stdout
            self.assertEqual(code, 0)
            self.assertIn("3件", out.getvalue())


if __name__ == "__main__":
    unittest.main()
