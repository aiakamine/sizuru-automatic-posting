# -*- coding: utf-8 -*-
"""kirinuki_hooks/write_gate.py の単体テスト。

予約確認必須: improve.md への Write/Edit は、セッションのAskUserQuestionログに
「予約投稿しますか」を含む質問への回答（採否は問わない）が記録されていなければ拒否する。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from kirinuki_hooks import asks as asks_mod  # noqa: E402
from kirinuki_hooks import write_gate as WG  # noqa: E402


def record_reservation_answer(session_id, answer="今回はしない"):
    question = "使えると判断した本を予約投稿しますか？"
    response = {"questions": [{"question": question, "header": "h", "options": []}], "answers": {question: answer}}
    asks_mod.record({"session_id": session_id, "tool_response": response}, now=1.0)


def event(tool_name="Write", file_path="/work/improve.md", session_id="sess"):
    return {"tool_name": tool_name, "tool_input": {"file_path": file_path}, "session_id": session_id}


class WriteGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["KIRINUKI_STATE_DIR"] = self.tmp.name
        self.config = WG.WriteGateConfig(skill_name="test-skill")

    def tearDown(self):
        del os.environ["KIRINUKI_STATE_DIR"]
        self.tmp.cleanup()

    def test_denies_write_to_improve_md_without_reservation_answer(self):
        result = WG.handle(self.config, event())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("予約投稿しますか", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_allows_write_to_improve_md_after_reservation_answered(self):
        record_reservation_answer("sess")
        result = WG.handle(self.config, event())
        self.assertIsNone(result)

    def test_allows_when_answer_was_to_reserve(self):
        record_reservation_answer("sess", answer="予約する")
        result = WG.handle(self.config, event())
        self.assertIsNone(result)

    def test_edit_tool_is_also_gated(self):
        result = WG.handle(self.config, event(tool_name="Edit"))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_edit_allowed_after_answer(self):
        record_reservation_answer("sess")
        result = WG.handle(self.config, event(tool_name="Edit"))
        self.assertIsNone(result)

    def test_other_files_are_not_gated(self):
        result = WG.handle(self.config, event(file_path="/work/hearing.md"))
        self.assertIsNone(result)

    def test_other_tools_are_not_gated(self):
        result = WG.handle(self.config, event(tool_name="Bash"))
        self.assertIsNone(result)

    def test_reservation_answer_from_a_different_session_does_not_count(self):
        record_reservation_answer("other-session")
        result = WG.handle(self.config, event(session_id="sess"))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_missing_session_id_is_allowed_not_denied(self):
        # Arrange: can't check what we can't identify -- fail open, not closed,
        # for this specific "we truly cannot tell" case
        data = {"tool_name": "Write", "tool_input": {"file_path": "/work/improve.md"}}
        result = WG.handle(self.config, data)
        self.assertIsNone(result)

    def test_path_key_variant_is_also_recognized(self):
        # Arrange: some tools may use "path" instead of "file_path"
        data = {"tool_name": "Write", "tool_input": {"path": "/work/improve.md"}, "session_id": "sess"}
        result = WG.handle(self.config, data)
        self.assertIsNotNone(result)

    def test_main_prints_deny_json(self):
        import io
        import json

        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            WG.main(self.config, stdin=io.StringIO(json.dumps(event())))
        finally:
            sys.stdout = old_stdout
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_main_prints_nothing_when_allowed(self):
        import io
        import json

        record_reservation_answer("sess")
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            WG.main(self.config, stdin=io.StringIO(json.dumps(event())))
        finally:
            sys.stdout = old_stdout
        self.assertEqual(out.getvalue().strip(), "")


if __name__ == "__main__":
    unittest.main()
