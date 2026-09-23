# -*- coding: utf-8 -*-
"""kirinuki_hooks/state.py の単体テスト。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from kirinuki_hooks import state as S  # noqa: E402

SKILL = "test-skill"


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["KIRINUKI_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        del os.environ["KIRINUKI_STATE_DIR"]
        self.tmp.cleanup()

    def test_load_state_when_missing_returns_empty(self):
        state = S.load_state(SKILL, "sess")
        self.assertEqual(state, {"workdirs": [], "notified": {}})

    def test_add_workdir_persists_and_dedupes(self):
        S.add_workdir(SKILL, "sess", "/a")
        S.add_workdir(SKILL, "sess", "/a")
        S.add_workdir(SKILL, "sess", "/b")
        state = S.load_state(SKILL, "sess")
        self.assertEqual(state["workdirs"].count("/a"), 1)
        self.assertIn("/b", state["workdirs"])

    def test_corrupted_state_file_is_treated_as_empty(self):
        path = os.path.join(self.tmp.name, "sess.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not valid json")
        state = S.load_state(SKILL, "sess")
        self.assertEqual(state, {"workdirs": [], "notified": {}})

    def test_workdirs_not_a_list_is_treated_as_empty(self):
        path = os.path.join(self.tmp.name, "sess.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"workdirs": "not-a-list"}))
        state = S.load_state(SKILL, "sess")
        self.assertEqual(state["workdirs"], [])

    def test_non_string_workdir_entries_are_dropped(self):
        path = os.path.join(self.tmp.name, "sess.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"workdirs": ["/a", 123, None, "/b"]}))
        state = S.load_state(SKILL, "sess")
        self.assertEqual(state["workdirs"], ["/a", "/b"])

    def test_session_id_with_unsafe_characters_is_hashed(self):
        unsafe = "../evil/id"
        S.add_workdir(SKILL, unsafe, "/a")
        entries = os.listdir(self.tmp.name)
        self.assertEqual(len(entries), 1)
        self.assertNotIn("evil", entries[0])

    def test_notified_fingerprint_roundtrip(self):
        self.assertIsNone(S.get_notified_fingerprint(SKILL, "sess", "/a"))
        S.set_notified_fingerprint(SKILL, "sess", "/a", [["x.mp4", 10]])
        self.assertEqual(S.get_notified_fingerprint(SKILL, "sess", "/a"), [["x.mp4", 10]])

    def test_notified_fingerprint_is_independent_per_workdir(self):
        S.set_notified_fingerprint(SKILL, "sess", "/a", [["x.mp4", 10]])
        self.assertIsNone(S.get_notified_fingerprint(SKILL, "sess", "/b"))

    def test_workdirs_and_notified_survive_together(self):
        S.add_workdir(SKILL, "sess", "/a")
        S.set_notified_fingerprint(SKILL, "sess", "/a", [["x.mp4", 10]])
        state = S.load_state(SKILL, "sess")
        self.assertEqual(state["workdirs"], ["/a"])
        self.assertEqual(state["notified"]["/a"], [["x.mp4", 10]])

    def test_different_skills_use_separate_state_files(self):
        # KIRINUKI_STATE_DIR は絶対的な上書きなので、ここでは既定パスの分離を
        # skill_name がディレクトリ名に含まれることで確認する（環境変数無しの場合）。
        del os.environ["KIRINUKI_STATE_DIR"]
        home = tempfile.mkdtemp()
        try:
            original_home = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                S.add_workdir("skill-a", "sess", "/a")
                S.add_workdir("skill-b", "sess", "/b")
                self.assertEqual(S.load_state("skill-a", "sess")["workdirs"], ["/a"])
                self.assertEqual(S.load_state("skill-b", "sess")["workdirs"], ["/b"])
            finally:
                if original_home is not None:
                    os.environ["HOME"] = original_home
        finally:
            os.environ["KIRINUKI_STATE_DIR"] = self.tmp.name


if __name__ == "__main__":
    unittest.main()
