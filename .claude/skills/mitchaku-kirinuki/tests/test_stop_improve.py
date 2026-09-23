# -*- coding: utf-8 -*-
"""hooks/stop_improve.py（薄いラッパ）のテスト。
汎用のフレームワーク挙動は kirinuki_hooks/tests/ で検証済み。ここではラッパの委譲配線と、
mitchaku-kirinuki 固有の設定・S-1/S-2（差し戻しの重複抑制）の実データ的な確認を行う。
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SKILL_DIR)
HOOK = os.path.join(SKILL_DIR, "hooks", "stop_improve.py")
TEMPLATE = os.path.join(SKILL_DIR, "templates", "improve.md")

sys.path.insert(0, REPO_ROOT)
from kirinuki_hooks import stop as ST  # noqa: E402

spec = importlib.util.spec_from_file_location("mitchaku_stop_wrapper", HOOK)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)
CONFIG = wrapper.build_config(ST)


def run_hook(payload, state_dir):
    env = dict(os.environ, KIRINUKI_STATE_DIR=state_dir)
    return subprocess.run(
        [sys.executable, HOOK], input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    )


def stop_payload(session_id="sess-1", stop_hook_active=False, cwd="/tmp"):
    return {"session_id": session_id, "cwd": cwd, "hook_event_name": "Stop",
            "stop_hook_active": stop_hook_active}


def write_state(state_dir, session_id, workdirs):
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "%s.json" % session_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"workdirs": workdirs}))


def touch_mp4(workdir, name="clip1.mp4", content=b"fake mp4 bytes"):
    out_dir = os.path.join(workdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, name), "wb") as f:
        f.write(content)


def touch_artifact(workdir):
    """完成動画アーティファクト（$D/artifact.json）が作成済みであることを模す。"""
    with open(os.path.join(workdir, "artifact.json"), "w", encoding="utf-8") as f:
        f.write('{"url": "https://claude.ai/artifact/example"}')


def write_reservation_answer(state_dir, session_id, answer="今回はしない"):
    """DREAM.md 1.5「予約投稿しますか」への回答が記録済みであることを模す
    （kirinuki_hooks.asks が読むJSONLログを直接書く。KIRINUKI_STATE_DIR override時は
    state.py と同じくディレクトリを直接ルートとして扱う ―― write_state と同じ流儀）。
    """
    asks_dir = os.path.join(state_dir, "asks")
    os.makedirs(asks_dir, exist_ok=True)
    question = "使えると判断した本を予約投稿しますか？"
    entry = {"ts": 1.0, "question": question, "header": "h", "options": [], "answer": answer}
    with open(os.path.join(asks_dir, "%s.jsonl" % session_id), "w", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_improve(path, *, status="完了", overrides=None):
    overrides = overrides or {}
    headings = ("今回のFB", "詰まった点・QC指摘", "改善提案", "ユーザー判断", "反映結果", "テスト結果")
    lines = ["# 改善フェーズ記録", "", "skill: mitchaku-kirinuki", "", "状態: %s" % status, ""]
    for heading in headings:
        default = "記入済み" if heading in CONFIG.required_improve_headings else ""
        lines.append("## %s" % heading)
        lines.append(overrides.get(heading, default))
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


class ConfigTest(unittest.TestCase):
    def test_required_headings_are_subset_of_template_headings(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            headings = {line[3:].strip() for line in f if line.startswith("## ")}
        self.assertTrue(set(CONFIG.required_improve_headings).issubset(headings))

    def test_skill_name_matches_template_skill_line(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        from kirinuki_hooks import records
        self.assertEqual(records.record_skill(text), CONFIG.skill_name)


class WrapperDelegationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.workdir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.workdir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocks_when_output_exists_but_improve_missing(self):
        touch_mp4(self.workdir)
        write_state(self.state_dir, "sess", [self.workdir])
        proc = run_hook(stop_payload(session_id="sess"), self.state_dir)
        out = json.loads(proc.stdout)
        self.assertEqual(out["decision"], "block")

    def test_allows_when_improve_complete(self):
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        touch_artifact(self.workdir)
        write_reservation_answer(self.state_dir, "sess")
        write_state(self.state_dir, "sess", [self.workdir])
        proc = run_hook(stop_payload(session_id="sess"), self.state_dir)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_blocks_when_improve_complete_but_artifact_json_missing(self):
        # 完成動画アーティファクト必須（build_config の require_artifact=True）
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        write_reservation_answer(self.state_dir, "sess")
        write_state(self.state_dir, "sess", [self.workdir])
        proc = run_hook(stop_payload(session_id="sess"), self.state_dir)
        out = json.loads(proc.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("artifact.json", out["reason"])

    def test_blocks_when_improve_complete_but_reservation_question_unanswered(self):
        # 予約確認必須（build_config の require_reservation_question=True）
        touch_mp4(self.workdir)
        write_improve(os.path.join(self.workdir, "improve.md"))
        touch_artifact(self.workdir)
        write_state(self.state_dir, "sess", [self.workdir])
        proc = run_hook(stop_payload(session_id="sess"), self.state_dir)
        out = json.loads(proc.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("予約投稿しますか", out["reason"])

    def test_warns_when_common_package_not_found(self):
        with tempfile.TemporaryDirectory() as isolated:
            hooks_dir = os.path.join(isolated, "hooks")
            os.makedirs(hooks_dir)
            import shutil
            shutil.copy(HOOK, hooks_dir)
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
            env["HOME"] = os.path.join(isolated, "fake-home")
            proc = subprocess.run(
                [sys.executable, os.path.join(hooks_dir, "stop_improve.py")],
                input=json.dumps(stop_payload()), capture_output=True, text=True, env=env,
            )
            out = json.loads(proc.stdout)
            self.assertIn("見つからない", out["systemMessage"])


class RepeatedStopReproductionTest(unittest.TestCase):
    """S-1/S-2: 前回から out/*.mp4 の集合が変わらなければ、2回目は差し戻さない。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.workdir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.workdir)
        touch_mp4(self.workdir)
        write_state(self.state_dir, "sess", [self.workdir])

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_call_with_unchanged_output_is_allowed(self):
        first = run_hook(stop_payload(session_id="sess"), self.state_dir)
        self.assertEqual(json.loads(first.stdout)["decision"], "block")
        second = run_hook(stop_payload(session_id="sess"), self.state_dir)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout.strip(), "")

    def test_blocks_again_when_output_set_changes(self):
        first = run_hook(stop_payload(session_id="sess"), self.state_dir)
        self.assertEqual(json.loads(first.stdout)["decision"], "block")
        touch_mp4(self.workdir, name="clip2.mp4")
        second = run_hook(stop_payload(session_id="sess"), self.state_dir)
        self.assertEqual(json.loads(second.stdout)["decision"], "block")


if __name__ == "__main__":
    unittest.main()
