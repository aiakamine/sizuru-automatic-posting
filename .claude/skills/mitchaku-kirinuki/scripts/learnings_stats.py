# -*- coding: utf-8 -*-
"""改善ログ（references/learnings.md、あれば references/learnings-archive.md）の
定量欄を集計して、回ごとの推移（そのまま使える率・手直し率など）を表示する。
使い方: python3 scripts/learnings_stats.py
        python3 scripts/learnings_stats.py --file references/learnings.md --file other.md
"""
import argparse
import os
import re
import sys

DEFAULT_FILES = ("references/learnings.md", "references/learnings-archive.md")
QUANT_KEYS = ("本数", "そのまま使える", "手直し", "使えない", "QC_NG", "QC誤検出", "作り直し", "書き出し秒")

_ENTRY_HEADING_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(.*)$")


def _find_kirinuki_hooks_parent():
    """kirinuki_hooks の親ディレクトリを探す（探索順は hooks/*.py のラッパと同じ）。"""
    here = os.path.dirname(os.path.realpath(__file__))
    candidates = [os.path.normpath(os.path.join(here, "..", ".."))]
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidates.append(project_dir)
    candidates.append(os.path.expanduser("~/.claude"))
    for parent in candidates:
        if os.path.isfile(os.path.join(parent, "kirinuki_hooks", "records.py")):
            return parent
    return None


def _load_records_module():
    parent = _find_kirinuki_hooks_parent()
    if parent is None:
        sys.exit(
            "kirinuki_hooks が見つかりません。"
            "リポジトリ配下から実行するか install.sh を実行してください。"
        )
    sys.path.insert(0, parent)
    from kirinuki_hooks import records
    return records


records = _load_records_module()


def parse_entries(text):
    """learnings.md（learnings-archive.mdも同形式）を解析し、
    [{"date":, "title":, "quant": {..} or None}, ...] をファイル記載順で返す。
    「## YYYY-MM-DD ...」形式の見出しだけを対象にする（「書き方」「記録」等は除外）。
    """
    sections = records.parse_sections(text)
    entries = []
    for heading, body in sections.items():
        match = _ENTRY_HEADING_RE.match(heading)
        if not match:
            continue
        entries.append({
            "date": match.group(1),
            "title": match.group(2).strip(),
            "quant": records.parse_quant_line(body),
        })
    return entries


def load_entries(paths):
    """複数ファイルからエントリを集める。存在しないファイルは無視する。"""
    entries = []
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            entries.extend(parse_entries(f.read()))
    return entries


def format_report(entries):
    """回ごとの表と、そのまま使える率／手直し率などの推移を文字列で返す。"""
    with_quant = [e for e in entries if e["quant"]]
    without = len(entries) - len(with_quant)
    lines = ["回数: %d件（うち定量あり %d件、定量なし %d件）" % (len(entries), len(with_quant), without)]
    if not with_quant:
        return "\n".join(lines)

    lines.append("%-12s %6s %8s %8s %8s %8s %8s %10s %8s" % (
        "日付", "本数", "使える率", "手直し率", "使えない", "QC_NG", "QC誤検出", "作り直し", "秒"))
    for entry in with_quant:
        q = entry["quant"]
        n = q.get("本数", 0) or 0
        as_is = q.get("そのまま使える", 0)
        rework = q.get("手直し", 0)
        as_is_rate = (as_is / n * 100.0) if n else 0.0
        rework_rate = (rework / n * 100.0) if n else 0.0
        lines.append("%-12s %6d %7.0f%% %7.0f%% %8d %8d %8d %10d %8d" % (
            entry["date"], n, as_is_rate, rework_rate,
            q.get("使えない", 0), q.get("QC_NG", 0), q.get("QC誤検出", 0),
            q.get("作り直し", 0), q.get("書き出し秒", 0)))
    return "\n".join(lines)


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", dest="files", default=None,
                    help="集計対象のlearnings.md（複数指定可。既定は%s）" % "、".join(DEFAULT_FILES))
    args = ap.parse_args(argv)
    if not args.files:
        args.files = list(DEFAULT_FILES)
    return args


def main(argv):
    args = parse_args(argv)
    entries = load_entries(args.files)
    print(format_report(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
