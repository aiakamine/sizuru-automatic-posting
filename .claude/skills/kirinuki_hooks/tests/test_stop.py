# -*- coding: utf-8 -*-
"""kirinuki_hooks/stop.py の単体テスト。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from kirinuki_hooks import stop as ST  # noqa: E402
from kirinuki_hooks import state as S  # noqa: E402

HEADINGS = ("今回のFB", "改善提案", "ユーザー判断")


def make_config(skill_name="test-skill"):
    return ST.StopConfig(skill_name=skill_name, required_improve_headings=HEADINGS)


def write_improve(path, *, status="完了", overrides=None, skill_line=None):
    overrides = overrides or {}
    lines = ["# 記録"]
    if skill_line is not None:
        lines.append("skill: %s" % skill_line)
    lines += ["", "状態: %s" % status, ""]
    for heading in HEADINGS:
        lines.append("## %s" % heading)
        lines.append(overrides.get(heading, "記入済み"))
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def touch_mp4(workdir, name="clip1.mp4", content=b"finished data"):
    out_dir = os.path.join(workdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, name), "wb") as f:
        f.write(content)


def payload(session_id="sess", stop_hook_active=False):
    return {"session_id": session_id, "cwd": "/tmp", "hook_event_name": "Stop",
            "stop_hook_active": stop_hook_active}


class StopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.environ["KIRINUKI_STATE_DIR"] = self.state_dir
        self.workdir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.workdir)
        self.config = make_config()

    def tearDown(self):
        del os.environ["KIRINUKI_STATE_DIR"]
        self.tmp.cleanup()

    def add(self, session_id, workdir):
        S.add_workdir(self.config.skill_name, session_id, workdir)

    def test_allows_when_stop_hook_already_active(self):
        touch_mp4(self.workdir)
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload(stop_hook_active=True))
        self.assertIsNone(result)

    def test_allows_when_no_state(self):
        result = ST.handle(self.config, payload(session_id="none"))
        self.assertIsNone(result)

    def test_allows_when_no_mp4(self):
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertIsNone(result)

    def test_allows_when_only_zero_byte_mp4(self):
        touch_mp4(self.workdir, content=b"")
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertIsNone(result)

    def test_blocks_when_finished_mp4_and_improve_missing(self):
        touch_mp4(self.workdir)
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertEqual(result["decision"], "block")
        self.assertIn(self.workdir, result["reason"])

    def test_blocks_when_heading_empty(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"), overrides={"ユーザー判断": ""})
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertEqual(result["decision"], "block")
        self.assertIn("ユーザー判断", result["reason"])

    def test_allows_when_improve_complete(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertIsNone(result)

    def test_ignores_nonexistent_workdir(self):
        self.add("sess", os.path.join(self.tmp.name, "missing"))
        result = ST.handle(self.config, payload())
        self.assertIsNone(result)

    def test_block_output_shape(self):
        touch_mp4(self.workdir)
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertEqual(set(result.keys()), {"decision", "reason"})
        self.assertEqual(result["decision"], "block")
        self.assertIn("状態: 完了", result["reason"])

    def test_improve_is_directory_blocks(self):
        touch_mp4(self.workdir)
        os.makedirs(os.path.join(self.workdir, "improve.md"))
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertEqual(result["decision"], "block")

    # --- G-4: skill: 所有権チェック ---

    def test_other_skill_record_is_allowed(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"), skill_line="other-skill", status="未完了")
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertIsNone(result)

    def test_matching_skill_record_is_checked(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"), skill_line="test-skill", status="未完了")
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertEqual(result["decision"], "block")

    # --- S-1/S-2: 集合が変わらなければ繰り返し差し戻さない ---

    def test_second_stop_with_same_mp4_set_is_allowed(self):
        touch_mp4(self.workdir)
        self.add("sess", self.workdir)
        first = ST.handle(self.config, payload())
        self.assertEqual(first["decision"], "block")
        second = ST.handle(self.config, payload())
        self.assertIsNone(second)

    def test_stop_blocks_again_when_mp4_set_changes(self):
        touch_mp4(self.workdir, name="clip1.mp4")
        self.add("sess", self.workdir)
        first = ST.handle(self.config, payload())
        self.assertEqual(first["decision"], "block")
        touch_mp4(self.workdir, name="clip2.mp4")
        second = ST.handle(self.config, payload())
        self.assertEqual(second["decision"], "block")

    def test_stop_blocks_again_when_existing_mp4_size_changes(self):
        touch_mp4(self.workdir, content=b"short")
        self.add("sess", self.workdir)
        first = ST.handle(self.config, payload())
        self.assertEqual(first["decision"], "block")
        touch_mp4(self.workdir, content=b"a much longer file content than before")
        second = ST.handle(self.config, payload())
        self.assertEqual(second["decision"], "block")

    def test_multiple_workdirs_only_incomplete_one_blocks(self):
        done = os.path.join(self.tmp.name, "done")
        pending = os.path.join(self.tmp.name, "pending")
        os.makedirs(done)
        os.makedirs(pending)
        touch_mp4(done)
        write_improve(os.path.join(done, "improve.md"))
        touch_mp4(pending)
        self.add("sess", done)
        self.add("sess", pending)
        result = ST.handle(self.config, payload())
        self.assertEqual(result["decision"], "block")
        self.assertIn(pending, result["reason"])
        self.assertNotIn(done, result["reason"])

    def test_blocks_when_rejection_line_has_no_reason(self):
        touch_mp4(self.workdir)
        write_improve(
            os.path.join(self.workdir, "improve.md"),
            overrides={"ユーザー判断": "- 却下：I-01（優先度が低い）"},
        )
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertEqual(result["decision"], "block")
        self.assertIn("却下", result["reason"])

    def test_allows_when_rejection_line_is_well_formed(self):
        touch_mp4(self.workdir)
        write_improve(
            os.path.join(self.workdir, "improve.md"),
            overrides={"ユーザー判断": "- 却下：I-01（理由：優先度が低い）"},
        )
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertIsNone(result)


# --- 完成動画ページ必須 / 予約確認必須（require_artifact / require_reservation_question） ---


def touch_artifact(workdir):
    with open(os.path.join(workdir, "artifact.json"), "w", encoding="utf-8") as f:
        f.write('{"url": "https://claude.ai/artifact/example"}')


def record_reservation_answer(session_id, question_text="使えると判断した本を予約投稿しますか？", answer="今回はしない"):
    from kirinuki_hooks import asks as asks_mod

    response = {
        "questions": [{"question": question_text, "header": "h", "options": []}],
        "answers": {question_text: answer},
    }
    asks_mod.record({"session_id": session_id, "tool_response": response}, now=1.0)


class StopRequireArtifactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.environ["KIRINUKI_STATE_DIR"] = self.state_dir
        self.workdir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.workdir)
        self.config = ST.StopConfig(
            skill_name="test-skill", required_improve_headings=HEADINGS, require_artifact=True
        )

    def tearDown(self):
        del os.environ["KIRINUKI_STATE_DIR"]
        self.tmp.cleanup()

    def add(self, session_id, workdir):
        S.add_workdir(self.config.skill_name, session_id, workdir)

    def test_blocks_when_improve_complete_but_artifact_json_missing(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertEqual(result["decision"], "block")
        self.assertIn("artifact.json", result["reason"])

    def test_allows_when_both_improve_complete_and_artifact_json_present(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        touch_artifact(self.workdir)
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertIsNone(result)

    def test_default_config_does_not_require_artifact(self):
        # Arrange: backward compatibility -- require_artifact defaults to False
        config = ST.StopConfig(skill_name="test-skill", required_improve_headings=HEADINGS)
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        S.add_workdir(config.skill_name, "sess", self.workdir)
        result = ST.handle(config, payload())
        self.assertIsNone(result)


class StopRequireReservationQuestionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.environ["KIRINUKI_STATE_DIR"] = self.state_dir
        self.workdir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.workdir)
        self.config = ST.StopConfig(
            skill_name="test-skill", required_improve_headings=HEADINGS, require_reservation_question=True
        )

    def tearDown(self):
        del os.environ["KIRINUKI_STATE_DIR"]
        self.tmp.cleanup()

    def add(self, session_id, workdir):
        S.add_workdir(self.config.skill_name, session_id, workdir)

    def test_blocks_when_reservation_question_never_asked(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertEqual(result["decision"], "block")
        self.assertIn("予約投稿しますか", result["reason"])

    def test_allows_when_reservation_question_was_answered_either_way(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        record_reservation_answer("sess", answer="今回はしない")
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertIsNone(result)

    def test_allows_when_answer_was_to_reserve(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        record_reservation_answer("sess", answer="予約する")
        self.add("sess", self.workdir)
        result = ST.handle(self.config, payload())
        self.assertIsNone(result)

    def test_default_config_does_not_require_reservation_question(self):
        config = ST.StopConfig(skill_name="test-skill", required_improve_headings=HEADINGS)
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        S.add_workdir(config.skill_name, "sess", self.workdir)
        result = ST.handle(config, payload())
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
