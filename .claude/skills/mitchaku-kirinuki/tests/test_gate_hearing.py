# -*- coding: utf-8 -*-
"""hooks/gate_hearing.py（薄いラッパ）のテスト。
汎用のフレームワーク挙動（状態行・見出し・コマンド解析等）は kirinuki_hooks/tests/ で
検証済み。ここではラッパの委譲配線と、mitchaku-kirinuki 固有の設定・実データ再現を確認する。
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
HOOK = os.path.join(SKILL_DIR, "hooks", "gate_hearing.py")
TEMPLATE = os.path.join(SKILL_DIR, "templates", "hearing.md")

sys.path.insert(0, REPO_ROOT)
from kirinuki_hooks import gate as G  # noqa: E402

spec = importlib.util.spec_from_file_location("mitchaku_gate_wrapper", HOOK)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)
CONFIG = wrapper.build_config(G)


def run_hook(payload, state_dir, extra_env=None):
    env = dict(os.environ, KIRINUKI_STATE_DIR=state_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, HOOK], input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    )


def pretool_payload(command, cwd, session_id="sess-1"):
    return {
        "session_id": session_id, "cwd": cwd, "hook_event_name": "PreToolUse",
        "tool_name": "Bash", "tool_input": {"command": command},
    }


def write_hearing(path, *, status="確定", overrides=None):
    overrides = overrides or {}
    lines = ["# ヒアリング記録", "", "skill: mitchaku-kirinuki", "", "状態: %s" % status, ""]
    for heading in CONFIG.required_hearing_headings:
        lines.append("## %s" % heading)
        lines.append(overrides.get(heading, "記入済み"))
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def read_state(state_dir, session_id):
    from kirinuki_hooks import state as S
    original = os.environ.get(S.STATE_DIR_ENV)
    os.environ[S.STATE_DIR_ENV] = state_dir
    try:
        return S.load_state(CONFIG.skill_name, session_id)
    finally:
        if original is None:
            del os.environ[S.STATE_DIR_ENV]
        else:
            os.environ[S.STATE_DIR_ENV] = original


class ConfigTest(unittest.TestCase):
    def test_required_headings_match_template(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            headings = [line[3:].strip() for line in f if line.startswith("## ")]
        self.assertEqual(list(CONFIG.required_hearing_headings), headings)

    def test_skill_name_matches_template_skill_line(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            text = f.read()
        from kirinuki_hooks import records
        self.assertEqual(records.record_skill(text), CONFIG.skill_name)


class WrapperDelegationTest(unittest.TestCase):
    """ラッパがkirinuki_hooksを見つけて委譲していることを、実プロセス経由で確認する。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.workdir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.workdir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_denies_when_hearing_missing(self):
        command = "cd %s && python3 scripts/mkbase.py" % self.workdir
        proc = run_hook(pretool_payload(command, self.tmp.name), self.state_dir)
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allows_and_records_when_complete(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"))
        command = "cd %s && python3 scripts/mkbase.py" % self.workdir
        proc = run_hook(pretool_payload(command, self.tmp.name, session_id="sess-ok"), self.state_dir)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        state = read_state(self.state_dir, "sess-ok")
        self.assertIn(os.path.normpath(self.workdir), state["workdirs"])

    def test_denies_when_honpen_url_left_empty(self):
        # 本編URLは必須見出し（sns-scheduleでの予約投稿の誘導先に使う）。他の見出しが
        # 全て埋まっていても、本編URLが空なら production は拒否される。
        write_hearing(os.path.join(self.workdir, "hearing.md"), overrides={"本編URL": ""})
        command = "cd %s && python3 scripts/mkbase.py" % self.workdir
        proc = run_hook(pretool_payload(command, self.tmp.name), self.state_dir)
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("本編URL", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_non_production_command_is_allowed(self):
        proc = run_hook(pretool_payload("python3 scripts/subscan.py src.mp4", self.tmp.name), self.state_dir)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_finds_common_package_through_symlinked_skill_directory(self):
        # install.sh は skill ディレクトリを symlink で導入する。symlink越しに
        # 起動しても、realpath解決により ../../kirinuki_hooks を正しく見つけられること。
        with tempfile.TemporaryDirectory() as fake_dest:
            linked_skill = os.path.join(fake_dest, "mitchaku-kirinuki")
            os.symlink(SKILL_DIR, linked_skill)
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
            env["HOME"] = os.path.join(fake_dest, "fake-home")  # ~/.claude/kirinuki_hooks に頼らない
            env["KIRINUKI_STATE_DIR"] = self.state_dir
            command = "cd %s && python3 scripts/mkbase.py" % self.workdir
            proc = subprocess.run(
                [sys.executable, os.path.join(linked_skill, "hooks", "gate_hearing.py")],
                input=json.dumps(pretool_payload(command, self.tmp.name)),
                capture_output=True, text=True, env=env,
            )
            out = json.loads(proc.stdout)
            # kirinuki_hooksが見つかっていれば通常のdeny（hearing.md無し）になる。
            # 見つからない場合のsystemMessageにはならないはず。
            self.assertIn("hookSpecificOutput", out)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_warns_when_common_package_not_found(self):
        # スキルだけをリポジトリ外にコピーして起動し、H-1（未検出を必ず知らせる）を確認する
        with tempfile.TemporaryDirectory() as isolated:
            hooks_dir = os.path.join(isolated, "hooks")
            os.makedirs(hooks_dir)
            import shutil
            shutil.copy(HOOK, hooks_dir)
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
            env["HOME"] = os.path.join(isolated, "fake-home")
            proc = subprocess.run(
                [sys.executable, os.path.join(hooks_dir, "gate_hearing.py")],
                input=json.dumps(pretool_payload("python3 scripts/mkbase.py", isolated)),
                capture_output=True, text=True, env=env,
            )
            out = json.loads(proc.stdout)
            self.assertIn("見つからない", out["systemMessage"])


class RealDataReproductionTest(unittest.TestCase):
    """レビュー指摘の再現ケース（実スクリプト名・実CLI形状を使う）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.workdir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.workdir)
        write_hearing(os.path.join(self.workdir, "hearing.md"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_g1_inline_python_c_is_not_production(self):
        # G-1: render の部分一致で無関係なコマンドを拒否していた
        proc = run_hook(pretool_payload('python3 -c "print(\'rendering done\')"', self.tmp.name),
                         self.state_dir)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_g2_render_variant_name_is_production(self):
        # G-2: render_new.py のような亜種は素通りしていた
        command = "python3 render_new.py %s clips_plan.py" % self.workdir
        proc = run_hook(pretool_payload(command, self.tmp.name, session_id="g2"), self.state_dir)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        state = read_state(self.state_dir, "g2")
        self.assertIn(os.path.normpath(self.workdir), state["workdirs"])

    def test_g4_other_skill_hearing_is_allowed(self):
        # G-4: taidan の記録が mitchaku の見出しで誤って拒否されない（逆方向の確認）
        path = os.path.join(self.workdir, "hearing.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# ヒアリング記録\n\nskill: taidan-kirinuki\n\n状態: 未確定\n")
        command = "python3 render_new.py %s clips_plan.py" % self.workdir
        proc = run_hook(pretool_payload(command, self.tmp.name, session_id="g4"), self.state_dir)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        state = read_state(self.state_dir, "g4")
        self.assertEqual(state["workdirs"], [])


if __name__ == "__main__":
    unittest.main()
