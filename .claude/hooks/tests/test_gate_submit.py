""".claude/hooks/gate_submit.py の単体テスト。

PreToolUse(Bash) で `schedule.py submit <plan.json> --yes` を検知し、
(1) 本編リンク必須（main_video_url / Xキャプション / TikTok・IGキャプション）、
(2) 承認必須（asksログに、plan.json更新後の「承認して予約」回答があるか）
の2条件を検査する。
"""
import json
import os
import sys
import tempfile
import time
import unittest

HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.normpath(os.path.join(HOOKS_DIR, "..", "skills"))
sys.path.insert(0, HOOKS_DIR)
sys.path.insert(0, SKILLS_DIR)
sys.path.insert(0, os.path.join(SKILLS_DIR, "sns-schedule", "scripts"))

import gate_submit  # noqa: E402
from kirinuki_hooks import asks  # noqa: E402


def _plan(main_video_url="https://youtu.be/abc123", clips=None):
    # Both the TikTok and X captions must contain the *normalized* URL
    # (SKILL.md step 1 normalizes before writing captions) -- gate_submit's
    # check compares against schedule_core.resolve_main_video_url()'s (also
    # normalized) output.
    normalized = gate_submit.schedule_core.normalize_youtube_url(main_video_url) if main_video_url else None
    tiktok_caption = "本編はこちら " + normalized if normalized else "本編はこちら"
    x_caption = "see " + normalized if normalized else "see nothing"
    return {
        "main_video_url": main_video_url,
        "clips": clips
        if clips is not None
        else [
            {
                "no": 1,
                "file": "/abs/01.mp4",
                "title": "t",
                "targets": [
                    {"service": "tiktok", "caption": tiktok_caption, "due_at": "2099-01-01T00:00:00+09:00"},
                    {"service": "x", "caption": x_caption, "due_at": "2099-01-01T00:00:00+09:00"},
                ],
            }
        ],
    }


def _write_plan(tmp_dir, plan):
    path = os.path.join(tmp_dir, "plan.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(plan, ensure_ascii=False))
    return path


def _approve(session_id, plan, ts, label="承認して予約 (推奨)"):
    urls = sorted({gate_submit.schedule_core.resolve_main_video_url(plan, c) for c in plan["clips"]})
    question = "この内容（本編URL: {}）で予約してよいですか？".format(" / ".join(u for u in urls if u))
    response = {
        "questions": [{"question": question, "header": "承認", "options": []}],
        "answers": {question: label},
    }
    asks.record({"session_id": session_id, "tool_response": response}, now=ts)


class MainVideoUrlProblemsTest(unittest.TestCase):
    def test_no_problems_for_a_valid_plan(self):
        plan = _plan()
        self.assertEqual(gate_submit._main_video_url_problems(plan), [])

    def test_missing_main_video_url_is_a_problem(self):
        plan = _plan(main_video_url=None)
        plan["clips"][0].pop("main_video_url", None)
        problems = gate_submit._main_video_url_problems(plan)
        self.assertTrue(any("main_video_url" in msg for _, msg in problems))

    def test_x_caption_without_url_is_a_problem(self):
        plan = _plan()
        plan["clips"][0]["targets"][1]["caption"] = "本編はこちらから見てね"
        problems = gate_submit._main_video_url_problems(plan)
        self.assertTrue(any("X" in msg or "x" in msg for _, msg in problems))

    def test_tiktok_caption_without_url_is_a_problem(self):
        plan = _plan()
        plan["clips"][0]["targets"][0]["caption"] = "見てね"  # missing the 本編URL
        problems = gate_submit._main_video_url_problems(plan)
        self.assertTrue(any("本編URL" in msg for _, msg in problems))

    def test_clip_level_override_is_used(self):
        plan = _plan(main_video_url="https://youtu.be/plan-level")
        plan["clips"][0]["main_video_url"] = "https://youtu.be/clip-override"
        plan["clips"][0]["targets"][0]["caption"] = "本編はこちら https://www.youtube.com/watch?v=clip-override"
        plan["clips"][0]["targets"][1]["caption"] = "see https://www.youtube.com/watch?v=clip-override"
        self.assertEqual(gate_submit._main_video_url_problems(plan), [])

    def test_tracking_params_in_main_video_url_do_not_cause_a_false_mismatch(self):
        # Arrange: main_video_url still has the raw pasted share link, but
        # the caption already contains the normalized form (as SKILL.md
        # step 1 instructs) -- resolve_main_video_url normalizes before
        # comparing, so this must not be flagged as "URL missing from caption"
        plan = _plan(main_video_url="https://youtu.be/MCmsezNx-Os?si=rwDOsGT0rpujKWYP")
        plan["clips"][0]["targets"][1]["caption"] = "see https://www.youtube.com/watch?v=MCmsezNx-Os"
        self.assertEqual(gate_submit._main_video_url_problems(plan), [])


class ApprovalOkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["KIRINUKI_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        del os.environ["KIRINUKI_STATE_DIR"]
        self.tmp.cleanup()

    def test_true_when_approved_after_plan_mtime_with_matching_urls(self):
        plan = _plan()
        _approve("sess-1", plan, ts=200.0)
        self.assertTrue(gate_submit._approval_ok("sess-1", plan, plan_mtime=100.0))

    def test_false_when_no_approval_recorded(self):
        plan = _plan()
        self.assertFalse(gate_submit._approval_ok("sess-1", plan, plan_mtime=100.0))

    def test_false_when_approval_is_before_plan_mtime(self):
        # Arrange: approved, then plan.json was edited afterward (later mtime)
        plan = _plan()
        _approve("sess-1", plan, ts=100.0)
        self.assertFalse(gate_submit._approval_ok("sess-1", plan, plan_mtime=200.0))

    def test_false_when_answer_does_not_start_with_the_approval_label(self):
        plan = _plan()
        _approve("sess-1", plan, ts=200.0, label="修正する")
        self.assertFalse(gate_submit._approval_ok("sess-1", plan, plan_mtime=100.0))

    def test_false_when_question_is_missing_one_of_the_urls(self):
        # Arrange: two clips with different main_video_url overrides, but the
        # approval question only mentions one of them
        plan = _plan()
        plan["clips"].append(
            {
                "no": 2,
                "file": "/abs/02.mp4",
                "title": "t2",
                "main_video_url": "https://youtu.be/second",
                "targets": [{"service": "x", "caption": "see https://youtu.be/second", "due_at": "2099-01-01T00:00:00+09:00"}],
            }
        )
        # missing the second clip's URL (normalized form)
        question = "この内容（本編URL: https://www.youtube.com/watch?v=abc123）で予約してよいですか？"
        response = {"questions": [{"question": question, "header": "h", "options": []}], "answers": {question: "承認して予約"}}
        asks.record({"session_id": "sess-1", "tool_response": response}, now=200.0)

        self.assertFalse(gate_submit._approval_ok("sess-1", plan, plan_mtime=100.0))


class HandleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["KIRINUKI_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        del os.environ["KIRINUKI_STATE_DIR"]
        self.tmp.cleanup()

    def test_non_submit_command_is_allowed(self):
        result = gate_submit.handle("echo hello", "/tmp", "sess-1")
        self.assertIsNone(result)

    def test_submit_without_yes_is_allowed(self):
        # schedule.py itself already rejects this (exit 2); nothing to gate
        with tempfile.TemporaryDirectory() as d:
            plan_path = _write_plan(d, _plan())
            command = f'python3 schedule.py submit "{plan_path}"'
            result = gate_submit.handle(command, "/tmp", "sess-1")
        self.assertIsNone(result)

    def test_denies_when_main_video_url_missing(self):
        with tempfile.TemporaryDirectory() as d:
            plan = _plan(main_video_url=None)
            plan["clips"][0].pop("main_video_url", None)
            plan_path = _write_plan(d, plan)
            command = f'python3 schedule.py submit "{plan_path}" --yes'
            result = gate_submit.handle(command, "/tmp", "sess-1")
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_denies_when_not_approved(self):
        with tempfile.TemporaryDirectory() as d:
            plan = _plan()
            plan_path = _write_plan(d, plan)
            command = f'python3 schedule.py submit "{plan_path}" --yes'
            result = gate_submit.handle(command, "/tmp", "sess-1")
        self.assertIsNotNone(result)
        self.assertIn("承認", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_allows_when_valid_and_approved(self):
        with tempfile.TemporaryDirectory() as d:
            plan = _plan()
            plan_path = _write_plan(d, plan)
            time.sleep(0.01)
            _approve("sess-1", plan, ts=time.time() + 10)
            command = f'python3 schedule.py submit "{plan_path}" --yes'
            result = gate_submit.handle(command, "/tmp", "sess-1")
        self.assertIsNone(result)

    def test_resolves_plan_path_relative_to_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            plan = _plan()
            _write_plan(d, plan)
            time.sleep(0.01)
            _approve("sess-1", plan, ts=time.time() + 10)
            command = 'python3 schedule.py submit "plan.json" --yes'
            result = gate_submit.handle(command, d, "sess-1")
        self.assertIsNone(result)

    def test_denies_when_plan_file_missing(self):
        result = gate_submit.handle(
            'python3 schedule.py submit "/no/such/plan.json" --yes', "/tmp", "sess-1"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_other_schedule_py_subcommands_are_not_gated(self):
        result = gate_submit.handle('python3 schedule.py validate "plan.json"', "/tmp", "sess-1")
        self.assertIsNone(result)

    def test_var_assignment_and_cd_are_resolved_like_gate_py(self):
        with tempfile.TemporaryDirectory() as d:
            plan = _plan()
            _write_plan(d, plan)
            time.sleep(0.01)
            _approve("sess-1", plan, ts=time.time() + 10)
            command = f'D={d} && python3 schedule.py submit "$D/plan.json" --yes'
            result = gate_submit.handle(command, "/tmp", "sess-1")
        self.assertIsNone(result)


class MainAllowsUnparseableUnrelatedCommandsTest(unittest.TestCase):
    """main()レベル: 検出フェーズ（このコマンドがsubmitかどうか）で失敗したら、
    denyせず許可する（systemMessageで知らせるだけ）ことを確認する。
    """

    def test_malformed_quoting_is_allowed_not_denied(self):
        import io

        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": 'echo "unclosed quote'},
                "cwd": "/tmp",
                "session_id": "sess-1",
            }
        )
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            gate_submit.main(stdin=io.StringIO(payload))
        finally:
            sys.stdout = old_stdout
        printed = out.getvalue().strip()
        if printed:
            result = json.loads(printed)
            self.assertIn("systemMessage", result)
            self.assertNotIn("hookSpecificOutput", result)

    def test_non_bash_tool_prints_nothing(self):
        import io

        payload = json.dumps({"tool_name": "Read", "session_id": "sess-1"})
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            gate_submit.main(stdin=io.StringIO(payload))
        finally:
            sys.stdout = old_stdout
        self.assertEqual(out.getvalue().strip(), "")


if __name__ == "__main__":
    unittest.main()
