#!/usr/bin/env python3
"""PostToolUse（AskUserQuestion）フック: 質問と回答を kirinuki_hooks.asks 経由で
セッションごとのログに記録する。ブロックは一切しない（PostToolUseは事後フック）。
sns-schedule の submitゲート・kirinukiの予約確認ゲートが、この記録を後で参照する。

失敗しても他のツール呼び出しを妨げないよう、何が起きても例外を投げない
（想定外のエラーはsystemMessageで知らせるだけに留める）。
"""
import json
import os
import sys


def _find_skills_dir():
    here = os.path.dirname(os.path.realpath(__file__))  # .../.claude/hooks
    candidates = [os.path.normpath(os.path.join(here, "..", "skills"))]
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidates.append(os.path.join(project_dir, ".claude", "skills"))
    candidates.append(os.path.expanduser("~/.claude/skills"))
    for parent in candidates:
        if os.path.isfile(os.path.join(parent, "kirinuki_hooks", "asks.py")):
            return parent
    return None


_skills_dir = _find_skills_dir()
if _skills_dir is not None:
    sys.path.insert(0, _skills_dir)
    from kirinuki_hooks import asks  # noqa: E402
else:
    asks = None


def main(stdin=None):
    if asks is None:
        print(
            json.dumps(
                {"systemMessage": "kirinuki_hooks.asks が見つからないため、この質問は記録していません。"},
                ensure_ascii=False,
            )
        )
        return

    raw = (stdin or sys.stdin).read()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    if data.get("tool_name") != "AskUserQuestion":
        return

    try:
        asks.record(data)
    except Exception as exc:  # 記録の失敗で他のツール呼び出しを妨げない
        message = f"AskUserQuestionの記録に失敗しました（承認確認に影響する可能性があります）: {exc}"
        print(json.dumps({"systemMessage": message}, ensure_ascii=False))


if __name__ == "__main__":
    main()
