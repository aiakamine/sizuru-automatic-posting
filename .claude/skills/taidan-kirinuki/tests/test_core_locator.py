# -*- coding: utf-8 -*-
"""scripts/_core.py（kirinuki_core を探すヘルパー）のテスト。
探索順は kirinuki_hooks（hooks/gate_hearing.py の _find_common_package_parent）と同じ:
①スキルディレクトリの親（リポジトリ内配置） ②$CLAUDE_PROJECT_DIR ③~/.claude。
実際のリポジトリ配置に依存せず検証するため、_core.py を隔離した一時ディレクトリに
コピーして候補ごとに再現する（test_stop_improve.py の
test_warns_when_common_package_not_found と同じ手法）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_LOCATOR = os.path.join(SKILL_DIR, "scripts", "_core.py")


def _place_locator(scripts_dir):
    os.makedirs(scripts_dir, exist_ok=True)
    shutil.copy(CORE_LOCATOR, os.path.join(scripts_dir, "_core.py"))


def _make_kirinuki_core(parent):
    os.makedirs(os.path.join(parent, "kirinuki_core"), exist_ok=True)
    open(os.path.join(parent, "kirinuki_core", "__init__.py"), "w").close()


def _base_env(isolated, project_dir=None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["HOME"] = os.path.join(isolated, "fake-home")  # ~/.claude を無効化する
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    return env


def _run_find(scripts_dir, env):
    """_core.find_kirinuki_core_parent() の結果を標準出力から読む
    （見つからなければ空行＝Noneとして扱う）。
    """
    driver = os.path.join(scripts_dir, "_driver_find.py")
    with open(driver, "w", encoding="utf-8") as f:
        f.write("import _core\nprint(_core.find_kirinuki_core_parent() or '')\n")
    proc = subprocess.run([sys.executable, driver], capture_output=True, text=True,
                           env=env, cwd=scripts_dir)
    return proc


def _run_ensure(scripts_dir, env):
    """_core.ensure_kirinuki_core_on_path() を呼ぶドライバ。成否をreturncodeで見る。"""
    driver = os.path.join(scripts_dir, "_driver_ensure.py")
    with open(driver, "w", encoding="utf-8") as f:
        f.write("import _core\n_core.ensure_kirinuki_core_on_path()\nprint('ok')\n")
    proc = subprocess.run([sys.executable, driver], capture_output=True, text=True,
                           env=env, cwd=scripts_dir)
    return proc


class FindKirinukiCoreParentTest(unittest.TestCase):
    def test_finds_via_skill_directory_parent(self):
        # Arrange: <isolated>/some-skill/scripts/_core.py から見て
        # ../../ = <isolated> が候補1（スキルディレクトリの親）
        with tempfile.TemporaryDirectory() as isolated:
            scripts_dir = os.path.join(isolated, "some-skill", "scripts")
            _place_locator(scripts_dir)
            _make_kirinuki_core(isolated)
            # Act
            proc = _run_find(scripts_dir, _base_env(isolated))
            # Assert
            self.assertEqual(proc.stdout.strip(), os.path.realpath(isolated))

    def test_finds_via_claude_project_dir_when_skill_relative_parent_misses(self):
        # Arrange: スキル相対の候補1にはkirinuki_coreが無く、$CLAUDE_PROJECT_DIRにだけある
        with tempfile.TemporaryDirectory() as isolated:
            scripts_dir = os.path.join(isolated, "some-skill", "scripts")
            _place_locator(scripts_dir)
            project_dir = os.path.join(isolated, "project")
            os.makedirs(project_dir)
            _make_kirinuki_core(project_dir)
            # Act
            proc = _run_find(scripts_dir, _base_env(isolated, project_dir=project_dir))
            # Assert
            self.assertEqual(proc.stdout.strip(), project_dir)

    def test_finds_via_home_claude_dir_as_last_resort(self):
        # Arrange: 候補1・2のどちらにも無く、~/.claude にだけある
        with tempfile.TemporaryDirectory() as isolated:
            scripts_dir = os.path.join(isolated, "some-skill", "scripts")
            _place_locator(scripts_dir)
            fake_home = os.path.join(isolated, "fake-home")
            claude_dir = os.path.join(fake_home, ".claude")
            _make_kirinuki_core(claude_dir)
            # Act
            proc = _run_find(scripts_dir, _base_env(isolated))
            # Assert: 候補2・3はHOME/CLAUDE_PROJECT_DIRの文字列をそのまま使う
            # （realpathで解決するのは候補1＝スキルディレクトリの親だけ）
            self.assertEqual(proc.stdout.strip(), claude_dir)

    def test_returns_none_when_not_found_anywhere(self):
        with tempfile.TemporaryDirectory() as isolated:
            scripts_dir = os.path.join(isolated, "some-skill", "scripts")
            _place_locator(scripts_dir)
            # Act: kirinuki_coreをどこにも置かない
            proc = _run_find(scripts_dir, _base_env(isolated))
            # Assert
            self.assertEqual(proc.stdout.strip(), "")


class EnsureKirinukiCoreOnPathTest(unittest.TestCase):
    def test_exits_with_guidance_when_not_found(self):
        with tempfile.TemporaryDirectory() as isolated:
            scripts_dir = os.path.join(isolated, "some-skill", "scripts")
            _place_locator(scripts_dir)
            # Act
            proc = _run_ensure(scripts_dir, _base_env(isolated))
            # Assert
            self.assertEqual(proc.returncode, 1)
            self.assertIn("kirinuki_core", proc.stderr)
            self.assertIn("install.sh", proc.stderr)

    def test_succeeds_and_can_import_kirinuki_core_when_found(self):
        with tempfile.TemporaryDirectory() as isolated:
            scripts_dir = os.path.join(isolated, "some-skill", "scripts")
            _place_locator(scripts_dir)
            _make_kirinuki_core(isolated)
            # Act
            proc = _run_ensure(scripts_dir, _base_env(isolated))
            # Assert
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
