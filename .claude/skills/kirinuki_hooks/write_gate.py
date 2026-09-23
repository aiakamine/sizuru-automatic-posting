# -*- coding: utf-8 -*-
"""PreToolUse（Write|Edit）フックの共通実装。

予約確認必須: `improve.md`（DREAM.md の記録ファイル）への書き込みは、セッション内で
DREAM.md 1.5「使えると判断した本を予約投稿しますか？」の質問に回答済み（採否は
問わない）でなければ拒否する。「keep it simple」の方針で、実際に使える本があったか
どうかは判定しない ―― 質問が答えられているかどうかだけを見る。

このフックは「予約確認を忘れる事故」を防ぐガードであり、悪意ある回避を防ぐ
セキュリティ境界ではない。判定できない場合（session_id が無い等）は許可する
（判定できないこと自体を deny の理由にはしない ―― 無関係な呼び出しを壊さないため）。
"""
import json
import os
import sys

from . import asks as asks_mod

_RESERVATION_QUESTION_SUBSTRING = "予約投稿しますか"


class WriteGateConfig:
    def __init__(self, skill_name):
        self.skill_name = skill_name


def _deny(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _target_path(tool_input):
    for key in ("file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def handle(config, data):
    """イベント(dict)を検査し、denyすべきなら出力用dictを、許可なら None を返す。"""
    if data.get("tool_name") not in ("Write", "Edit"):
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path = _target_path(tool_input)
    if not path or os.path.basename(path) != "improve.md":
        return None

    session_id = data.get("session_id")
    if not session_id:
        return None  # 判定できない＝このフックの検査対象外として許可する

    found = asks_mod.find_answered_question(
        session_id, lambda entry: _RESERVATION_QUESTION_SUBSTRING in (entry.get("question") or "")
    )
    if found is not None:
        return None
    return _deny(
        "improve.md を書く前に、DREAM.md 1.5「使えると判断した本を予約投稿しますか？」を"
        "AskUserQuestionで確認してください（予約する／今回はしない、どちらでもよい）"
    )


def main(config, stdin=None):
    raw = (stdin or sys.stdin).read()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    try:
        result = handle(config, data)
    except Exception as exc:  # ここに来た時点で improve.md への書き込みだと確定している→安全側(deny)
        print(
            json.dumps(
                _deny("予約確認の検査中にエラーが発生しました。安全のため拒否します: %s" % exc),
                ensure_ascii=False,
            )
        )
        return
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))
