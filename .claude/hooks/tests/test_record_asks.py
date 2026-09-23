""".claude/hooks/record_asks.py の単体テスト（PostToolUse/AskUserQuestion）。"""
import io
import json
import os
import sys
import tempfile
import unittest

HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.normpath(os.path.join(HOOKS_DIR, "..", "skills"))
sys.path.insert(0, HOOKS_DIR)
sys.path.insert(0, SKILLS_DIR)

import record_asks  # noqa: E402
from kirinuki_hooks import asks  # noqa: E402

REAL_SHAPE_RESPONSE = {
    "questions": [
        {
            "question": "予約してよいですか？",
            "header": "承認",
            "options": [{"label": "承認して予約", "description": "予約する"}],
            "multiSelect": False,
        }
    ],
    "answers": {"予約してよいですか？": "承認して予約"},
    "annotations": {},
}


class RecordAsksMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["KIRINUKI_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        del os.environ["KIRINUKI_STATE_DIR"]
        self.tmp.cleanup()

    def _run(self, payload):
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            record_asks.main(stdin=io.StringIO(json.dumps(payload)))
        finally:
            sys.stdout = old_stdout
        return out.getvalue()

    def test_records_a_real_shaped_payload(self):
        payload = {
            "session_id": "sess-1",
            "tool_name": "AskUserQuestion",
            "tool_response": REAL_SHAPE_RESPONSE,
        }
        self._run(payload)
        entries = asks.load_entries("sess-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["answer"], "承認して予約")

    def test_non_ask_user_question_tool_is_ignored(self):
        payload = {"session_id": "sess-1", "tool_name": "Bash", "tool_response": {}}
        self._run(payload)
        self.assertEqual(asks.load_entries("sess-1"), [])

    def test_prints_nothing_on_success(self):
        payload = {
            "session_id": "sess-1",
            "tool_name": "AskUserQuestion",
            "tool_response": REAL_SHAPE_RESPONSE,
        }
        printed = self._run(payload)
        self.assertEqual(printed.strip(), "")

    def test_malformed_json_input_does_not_raise(self):
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            record_asks.main(stdin=io.StringIO("not json"))
        finally:
            sys.stdout = old_stdout
        # no exception is the point of this test

    def test_unexpected_error_never_raises_only_reports(self):
        payload = {"session_id": "sess-1", "tool_name": "AskUserQuestion", "tool_response": REAL_SHAPE_RESPONSE}

        def _boom(data, now=None):
            raise RuntimeError("boom")

        original = asks.record
        asks.record = _boom
        try:
            printed = self._run(payload)
        finally:
            asks.record = original
        # must not raise; may print a systemMessage but nothing that looks like a deny
        if printed.strip():
            result = json.loads(printed)
            self.assertNotIn("hookSpecificOutput", result)


if __name__ == "__main__":
    unittest.main()
