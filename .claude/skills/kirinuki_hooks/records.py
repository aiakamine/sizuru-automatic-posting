# -*- coding: utf-8 -*-
"""ヒアリング/改善フェーズの記録ファイル（hearing.md / improve.md）の解析・検査。
スキル非依存の純粋関数のみを置く（副作用・状態ファイルI/Oは state.py 側）。
"""
import re

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_STATUS_LINE_RE = re.compile(r"^\s*状態\s*[:：]\s*(.+?)\s*$")
_SKILL_LINE_RE = re.compile(r"^\s*skill\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_HEADING_PREFIX = "## "
_EMPTY_PLACEHOLDERS = ("未定", "TODO", "-")


def parse_sections(text):
    """`## 見出し` 行で本文を区切り、{見出し: 本文} を返す。同じ見出しが複数あれば最後を採用する。"""
    sections = {}
    heading = None
    body = []
    for line in text.splitlines():
        if line.startswith(_HEADING_PREFIX):
            if heading is not None:
                sections[heading] = "\n".join(body)
            heading = line[len(_HEADING_PREFIX):].strip()
            body = []
        elif heading is not None:
            body.append(line)
    if heading is not None:
        sections[heading] = "\n".join(body)
    return sections


def is_section_empty(body):
    """HTMLコメントと前後空白を除いて空、またはプレースホルダのみなら空扱い。"""
    stripped = _HTML_COMMENT_RE.sub("", body).strip()
    return stripped == "" or stripped in _EMPTY_PLACEHOLDERS


def record_skill(text):
    """記録冒頭の `skill: <名前>` 行の値を返す（無ければ None＝旧形式の記録）。"""
    for line in text.splitlines():
        match = _SKILL_LINE_RE.match(line)
        if match:
            return match.group(1)
    return None


def _strip_noise_for_status(text):
    """状態行の判定対象から、HTMLコメントとコードフェンス（``` ... ```）の中身を除く。"""
    text = _HTML_COMMENT_RE.sub("", text)
    text = _CODE_FENCE_RE.sub("", text)
    return text


def _status_line_values(text):
    """コメント/コードフェンスを除いた本文から、`状態: <値>` 行の値を出現順に返す。"""
    cleaned = _strip_noise_for_status(text)
    values = []
    for line in cleaned.splitlines():
        match = _STATUS_LINE_RE.match(line)
        if match:
            values.append(match.group(1))
    return values


def status_line_check(text, expected):
    """(合格か, 不合格理由 or None) を返す。
    有効な状態行が0個、または値が競合している（矛盾する複数の値がある）場合は不合格。
    """
    values = _status_line_values(text)
    unique = sorted(set(values))
    if not values:
        return False, "状態: %s の行がありません" % expected
    if len(unique) > 1:
        return False, "状態行の値が競合しています（%s）" % "、".join(unique)
    if values[0] != expected:
        return False, "状態: %s ではありません（現在の値: %s）" % (expected, values[0])
    return True, None


def missing_headings(sections, required):
    return [heading for heading in required if is_section_empty(sections.get(heading, ""))]


def validate_record(text, required_headings, expected_status):
    """hearing.md / improve.md 共通の検査。
    戻り値: (合格か, 未入力の見出し名リスト, 状態行が合格か, 状態行の不合格理由 or None)
    """
    sections = parse_sections(text)
    missing = missing_headings(sections, required_headings)
    status_ok, status_problem = status_line_check(text, expected_status)
    return (status_ok and not missing), missing, status_ok, status_problem


# --- 却下行の形式検査（`却下：I-01（理由：…）`。全角/半角の括弧・コロン両対応） ---

# 行頭（任意の "- "/"* " 箇条書き）の直後に「却下」+ コロンが来る行だけを対象にする
# （文中に「却下」という語が出てくるだけの行を誤検出しないため）。
_REJECTION_START_RE = re.compile(r"^(?:[-*]\s*)?却下\s*[:：]")
# ids 部分は「（」「(」の手前までを丸ごと取り、理由は「理由[:：]」の後ろの文字列を取る。
_REJECTION_LINE_RE = re.compile(
    r"^(?:[-*]\s*)?却下\s*[:：]\s*(?P<ids>[^（(]*)[（(]\s*理由\s*[:：]\s*(?P<reason>.*?)\s*[）)]\s*$"
)


def find_rejection_problems(text):
    """`却下：I-xx（理由：…）` 形式でない、または理由が空の却下行を見つける。
    戻り値: 問題のあった行文字列のリスト（無ければ空リスト）。
    """
    problems = []
    for line in text.splitlines():
        stripped = line.strip()
        if not _REJECTION_START_RE.match(stripped):
            continue
        match = _REJECTION_LINE_RE.match(stripped)
        if not match or not match.group("reason").strip():
            problems.append(stripped)
    return problems


def validate_improve_rejections(text):
    """improve.md の `## ユーザー判断` 内にある却下行の形式を検査する。
    戻り値: (合格か, 問題のあった行文字列のリスト)
    """
    sections = parse_sections(text)
    body = sections.get("ユーザー判断", "")
    problems = find_rejection_problems(body)
    return (not problems), problems


# --- 改善ログ（references/learnings.md）の定量欄の解析 ---

# 例: "- 定量：本数=4 そのまま使える=2 手直し=2 使えない=0 QC_NG=0 QC誤検出=3 作り直し=1 書き出し秒=90"
_QUANT_START_RE = re.compile(r"^(?:[-*]\s*)?定量\s*[:：]\s*(?P<fields>.+)$")
_QUANT_FIELD_RE = re.compile(r"(?P<key>[^\s=]+)=(?P<value>-?\d+)")


def parse_quant_line(text):
    """本文中の「定量：key=value ...」行を {key: int値} で返す（無ければ None）。"""
    for line in text.splitlines():
        match = _QUANT_START_RE.match(line.strip())
        if not match:
            continue
        fields = {}
        for field_match in _QUANT_FIELD_RE.finditer(match.group("fields")):
            fields[field_match.group("key")] = int(field_match.group("value"))
        return fields
    return None
