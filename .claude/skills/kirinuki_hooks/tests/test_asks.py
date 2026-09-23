# -*- coding: utf-8 -*-
"""kirinuki_hooks/asks.py の単体テスト。

AskUserQuestion の PostToolUse payload は実データ（このセッションのtranscriptより）:
{"questions": [{"question": "...", "header": "...",
                "options": [{"label": "...", "description": "..."}],
                "multiSelect": false}],
 "answers": {"<質問文そのまま>": "<回答文字列>"},
 "annotations": {}}
これが tool_response に載る想定。tool_result / tool_output、JSON文字列、
{"type":"text","text":"<json>"} の形にもフォールバックする。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from kirinuki_hooks import asks  # noqa: E402

REAL_SHAPE_RESPONSE = {
    "questions": [
        {
            "question": "予約する日時は https://youtu.be/abc123 でよいですか？",
            "header": "承認",
            "options": [
                {"label": "承認して予約 (推奨)", "description": "この内容で予約する"},
                {"label": "修正する", "description": "内容を直す"},
            ],
            "multiSelect": False,
        }
    ],
    "answers": {
        "予約する日時は https://youtu.be/abc123 でよいですか？": "承認して予約 (推奨)",
    },
    "annotations": {},
}


def _event(session_id="sess-1", tool_response=None, tool_input=None):
    event = {"session_id": session_id, "tool_name": "AskUserQuestion"}
    if tool_response is not None:
        event["tool_response"] = tool_response
    if tool_input is not None:
        event["tool_input"] = tool_input
    return event


class AsksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["KIRINUKI_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        del os.environ["KIRINUKI_STATE_DIR"]
        self.tmp.cleanup()

    # --- extract_response ---

    def test_extract_response_from_tool_response_dict(self):
        data = _event(tool_response=REAL_SHAPE_RESPONSE)
        self.assertEqual(asks.extract_response(data), REAL_SHAPE_RESPONSE)

    def test_extract_response_falls_back_to_tool_result(self):
        data = {"session_id": "s", "tool_result": REAL_SHAPE_RESPONSE}
        self.assertEqual(asks.extract_response(data), REAL_SHAPE_RESPONSE)

    def test_extract_response_falls_back_to_tool_output(self):
        data = {"session_id": "s", "tool_output": REAL_SHAPE_RESPONSE}
        self.assertEqual(asks.extract_response(data), REAL_SHAPE_RESPONSE)

    def test_extract_response_decodes_json_string(self):
        data = {"session_id": "s", "tool_response": json.dumps(REAL_SHAPE_RESPONSE)}
        self.assertEqual(asks.extract_response(data), REAL_SHAPE_RESPONSE)

    def test_extract_response_decodes_text_wrapper(self):
        wrapped = {"type": "text", "text": json.dumps(REAL_SHAPE_RESPONSE)}
        data = {"session_id": "s", "tool_response": wrapped}
        self.assertEqual(asks.extract_response(data), REAL_SHAPE_RESPONSE)

    def test_extract_response_none_when_nothing_present(self):
        self.assertIsNone(asks.extract_response({"session_id": "s"}))

    def test_extract_response_none_when_json_string_is_garbage(self):
        data = {"session_id": "s", "tool_response": "not json"}
        self.assertIsNone(asks.extract_response(data))

    # --- record + load_entries ---

    def test_record_then_load_roundtrips(self):
        data = _event(tool_response=REAL_SHAPE_RESPONSE)
        asks.record(data, now=1000.0)
        entries = asks.load_entries("sess-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["question"], REAL_SHAPE_RESPONSE["questions"][0]["question"])
        self.assertEqual(entries[0]["answer"], "承認して予約 (推奨)")
        self.assertEqual(entries[0]["ts"], 1000.0)

    def test_record_multiple_questions_in_one_call_each_get_their_own_answer(self):
        response = {
            "questions": [
                {"question": "Q1？", "header": "h", "options": []},
                {"question": "Q2？", "header": "h", "options": []},
            ],
            "answers": {"Q1？": "A1", "Q2？": "A2"},
        }
        asks.record(_event(tool_response=response), now=1.0)
        entries = asks.load_entries("sess-1")
        by_question = {e["question"]: e["answer"] for e in entries}
        self.assertEqual(by_question, {"Q1？": "A1", "Q2？": "A2"})

    def test_record_is_a_noop_when_no_session_id(self):
        asks.record({"tool_response": REAL_SHAPE_RESPONSE})
        # no exception, and nothing recorded anywhere retrievable
        self.assertEqual(asks.load_entries("sess-1"), [])

    def test_record_is_a_noop_when_no_questions_found(self):
        asks.record(_event(tool_response={"answers": {}}))
        self.assertEqual(asks.load_entries("sess-1"), [])

    def test_load_entries_when_nothing_recorded_returns_empty(self):
        self.assertEqual(asks.load_entries("never-seen"), [])

    def test_load_entries_ignores_corrupted_lines(self):
        path = asks._asks_path("sess-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json\n")
            f.write(json.dumps({"ts": 1.0, "question": "Q？", "answer": "A"}) + "\n")
        entries = asks.load_entries("sess-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["question"], "Q？")

    def test_answers_missing_for_a_question_is_none(self):
        response = {
            "questions": [{"question": "Q？", "header": "h", "options": []}],
            "answers": {},
        }
        asks.record(_event(tool_response=response), now=1.0)
        entries = asks.load_entries("sess-1")
        self.assertIsNone(entries[0]["answer"])

    def test_falls_back_to_tool_input_questions_when_response_has_none(self):
        # Arrange: response only has answers, no "questions" key -- fall
        # back to tool_input.questions for the question text/options
        data = _event(
            tool_response={"answers": {"Q？": "A"}},
            tool_input={"questions": [{"question": "Q？", "header": "h", "options": []}]},
        )
        asks.record(data, now=1.0)
        entries = asks.load_entries("sess-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["answer"], "A")

    def test_sessions_are_isolated(self):
        asks.record(_event(session_id="sess-a", tool_response=REAL_SHAPE_RESPONSE), now=1.0)
        self.assertEqual(asks.load_entries("sess-b"), [])
        self.assertEqual(len(asks.load_entries("sess-a")), 1)

    # --- find_answered_question ---

    def test_find_answered_question_matches_by_predicate(self):
        asks.record(_event(tool_response=REAL_SHAPE_RESPONSE), now=1.0)
        found = asks.find_answered_question("sess-1", lambda e: "youtu.be/abc123" in e["question"])
        self.assertIsNotNone(found)

    def test_find_answered_question_none_when_predicate_never_matches(self):
        asks.record(_event(tool_response=REAL_SHAPE_RESPONSE), now=1.0)
        found = asks.find_answered_question("sess-1", lambda e: "nope" in e["question"])
        self.assertIsNone(found)

    def test_find_answered_question_ignores_unanswered_entries(self):
        response = {
            "questions": [{"question": "Q？", "header": "h", "options": []}],
            "answers": {},  # never answered
        }
        asks.record(_event(tool_response=response), now=1.0)
        found = asks.find_answered_question("sess-1", lambda e: True)
        self.assertIsNone(found)

    def test_find_answered_question_respects_min_ts(self):
        asks.record(_event(tool_response=REAL_SHAPE_RESPONSE), now=100.0)
        # answered before min_ts -- must not count
        found = asks.find_answered_question("sess-1", lambda e: True, min_ts=200.0)
        self.assertIsNone(found)
        # answered at/after min_ts -- counts
        found2 = asks.find_answered_question("sess-1", lambda e: True, min_ts=100.0)
        self.assertIsNotNone(found2)

    def test_debug_snapshot_written_once_and_never_to_stdout(self):
        asks.record(_event(tool_response=REAL_SHAPE_RESPONSE), now=1.0)
        debug_path = asks._debug_path()
        self.assertTrue(debug_path.is_file())
        snapshot = json.loads(debug_path.read_text(encoding="utf-8"))
        self.assertIn("top_level_keys", snapshot)

    def test_debug_snapshot_is_not_overwritten_on_second_call(self):
        asks.record(_event(tool_response=REAL_SHAPE_RESPONSE), now=1.0)
        first_mtime = asks._debug_path().stat().st_mtime
        asks.record(_event(session_id="sess-2", tool_response=REAL_SHAPE_RESPONSE), now=2.0)
        second_mtime = asks._debug_path().stat().st_mtime
        self.assertEqual(first_mtime, second_mtime)


if __name__ == "__main__":
    unittest.main()
