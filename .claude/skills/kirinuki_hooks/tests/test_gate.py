# -*- coding: utf-8 -*-
"""kirinuki_hooks/gate.py の単体テスト。
generic な合成スキル設定で、フレームワーク自体の挙動（状態行・見出し・コマンド解析・
複数呼び出し・skill:所有権チェックなど）を検証する。実スクリプト名（render.py等）を
使った固有の再現ケースは各スキルの tests/ 側で行う。
"""
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from kirinuki_hooks import gate as G  # noqa: E402

HEADINGS = ("目的", "本数")


def make_config(headings=HEADINGS, rules=None, skill_name="test-skill"):
    if rules is None:
        rules = (G.ProductionRule(("tool*.py",), workdir_index=0, required_positionals=1),)
    return G.GateConfig(skill_name=skill_name, required_hearing_headings=headings, production_rules=rules)


def write_hearing(path, *, status="確定", overrides=None, skill_line=None):
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


def event(command, cwd, session_id="sess-1", tool_name="Bash"):
    return {
        "session_id": session_id,
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


class GateTest(unittest.TestCase):
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

    def read_state(self, session_id="sess-1"):
        from kirinuki_hooks import state as S
        return S.load_state(self.config.skill_name, session_id)

    # --- 基本の許可条件 ---

    def test_non_bash_tool_is_allowed(self):
        result = G.handle(self.config, event("python3 tool.py %s" % self.workdir, self.tmp.name,
                                              tool_name="Read"))
        self.assertIsNone(result)

    def test_tool_input_not_object_is_allowed(self):
        data = {"tool_name": "Bash", "tool_input": "python3 tool.py"}
        self.assertIsNone(G.handle(self.config, data))

    def test_command_not_string_is_allowed(self):
        data = {"tool_name": "Bash", "tool_input": {"command": 123}}
        self.assertIsNone(G.handle(self.config, data))

    def test_non_production_command_is_allowed(self):
        result = G.handle(self.config, event("ls -la", self.tmp.name))
        self.assertIsNone(result)

    # --- hearing.md 検査 ---

    def test_denies_when_hearing_missing(self):
        cmd = "cd %s && python3 tool.py ." % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("hearing.md がありません", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_denies_when_heading_empty(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"), overrides={"本数": ""})
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("本数", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_denies_when_status_wrong(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"), status="未確定")
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allows_and_records_when_complete(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"))
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertIsNone(result)
        state = self.read_state()
        self.assertIn(os.path.normpath(self.workdir), state["workdirs"])

    def test_deny_output_shape(self):
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertEqual(set(result.keys()), {"hookSpecificOutput"})
        inner = result["hookSpecificOutput"]
        self.assertEqual(inner["hookEventName"], "PreToolUse")
        self.assertEqual(inner["permissionDecision"], "deny")
        self.assertIsInstance(inner["permissionDecisionReason"], str)

    # --- cd ベースの解決（workdir_index=None） ---

    def test_cd_based_resolution_when_no_positional(self):
        rules = (G.ProductionRule(("nocli*.py",), workdir_index=None),)
        config = make_config(rules=rules)
        write_hearing(os.path.join(self.workdir, "hearing.md"))
        cmd = "cd %s && python3 nocli_tool.py" % self.workdir
        result = G.handle(config, event(cmd, self.tmp.name))
        self.assertIsNone(result)

    # --- コマンド解析：変数・演算子 ---

    def test_semicolon_and_pipe_separators(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"))
        cmd = "echo hi | cat; python3 tool.py %s" % self.workdir
        self.assertIsNone(G.handle(self.config, event(cmd, self.tmp.name)))

    def test_export_assignment_and_brace_variable(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"))
        cmd = "export D=%s && python3 tool.py ${D}" % self.workdir
        self.assertIsNone(G.handle(self.config, event(cmd, self.tmp.name)))

    def test_tilde_expansion(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"))
        cmd = "python3 tool.py ~/work"
        with unittest.mock.patch.dict(os.environ, {"HOME": self.tmp.name}):
            result = G.handle(self.config, event(cmd, self.tmp.name, session_id="s-tilde"))
        self.assertIsNone(result)

    def test_undefined_variable_denies(self):
        cmd = "python3 tool.py $UNDEF"
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("作業フォルダを特定できません", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unterminated_quote_denies(self):
        cmd = 'python3 tool.py "unterminated'
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_hearing_is_directory_denies(self):
        os.makedirs(os.path.join(self.workdir, "hearing.md"))
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_hearing_not_utf8_denies(self):
        with open(os.path.join(self.workdir, "hearing.md"), "wb") as f:
            f.write(b"\xff\xfe\x00\x01")
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # --- 状態行の表記ゆれ・矛盾 ---

    def test_fullwidth_colon_status_line(self):
        path = os.path.join(self.workdir, "hearing.md")
        write_hearing(path)
        with open(path, encoding="utf-8") as f:
            text = f.read().replace("状態: 確定", "状態：確定")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        cmd = "python3 tool.py %s" % self.workdir
        self.assertIsNone(G.handle(self.config, event(cmd, self.tmp.name)))

    def test_conflicting_status_lines_deny(self):
        path = os.path.join(self.workdir, "hearing.md")
        write_hearing(path)
        with open(path, encoding="utf-8") as f:
            text = f.read() + "\n状態: 未確定\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # --- 複数の制作呼び出し ---

    def test_multiple_calls_all_pass_are_all_recorded(self):
        wa = os.path.join(self.tmp.name, "wa")
        wb = os.path.join(self.tmp.name, "wb")
        os.makedirs(wa)
        os.makedirs(wb)
        write_hearing(os.path.join(wa, "hearing.md"))
        write_hearing(os.path.join(wb, "hearing.md"))
        cmd = "python3 tool.py %s && python3 tool.py %s" % (wa, wb)
        result = G.handle(self.config, event(cmd, self.tmp.name, session_id="multi"))
        self.assertIsNone(result)
        state = self.read_state("multi")
        self.assertIn(os.path.normpath(wa), state["workdirs"])
        self.assertIn(os.path.normpath(wb), state["workdirs"])

    def test_multiple_calls_one_fails_denies_and_records_none(self):
        wa = os.path.join(self.tmp.name, "wa")
        wb = os.path.join(self.tmp.name, "wb")
        os.makedirs(wa)
        os.makedirs(wb)
        write_hearing(os.path.join(wa, "hearing.md"))
        cmd = "python3 tool.py %s && python3 tool.py %s" % (wa, wb)
        result = G.handle(self.config, event(cmd, self.tmp.name, session_id="multi-bad"))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        state = self.read_state("multi-bad")
        self.assertEqual(state["workdirs"], [])

    def test_reassigned_variable_does_not_affect_earlier_call(self):
        wa = os.path.join(self.tmp.name, "wa")
        wb = os.path.join(self.tmp.name, "wb")
        os.makedirs(wa)
        os.makedirs(wb)
        write_hearing(os.path.join(wa, "hearing.md"))
        write_hearing(os.path.join(wb, "hearing.md"))
        cmd = "D=%s && python3 tool.py $D && D=%s && python3 tool.py $D" % (wa, wb)
        result = G.handle(self.config, event(cmd, self.tmp.name, session_id="reassign"))
        self.assertIsNone(result)
        state = self.read_state("reassign")
        self.assertIn(os.path.normpath(wa), state["workdirs"])
        self.assertIn(os.path.normpath(wb), state["workdirs"])

    def test_relative_cd_chains_from_previous_cwd(self):
        rules = (G.ProductionRule(("nocli*.py",), workdir_index=None),)
        config = make_config(rules=rules)
        nested = os.path.join(self.workdir, "nested")
        os.makedirs(nested)
        write_hearing(os.path.join(nested, "hearing.md"))
        cmd = "cd work && cd nested && python3 nocli_tool.py"
        result = G.handle(config, event(cmd, self.tmp.name, session_id="nested"))
        self.assertIsNone(result)
        state = self.read_state("nested")
        self.assertIn(os.path.normpath(nested), state["workdirs"])

    def test_duplicate_workdir_recorded_once(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"))
        cmd = "python3 tool.py %s" % self.workdir
        G.handle(self.config, event(cmd, self.tmp.name, session_id="dup"))
        G.handle(self.config, event(cmd, self.tmp.name, session_id="dup"))
        state = self.read_state("dup")
        self.assertEqual(state["workdirs"].count(os.path.normpath(self.workdir)), 1)

    # --- G-4: skill: 所有権チェック ---

    def test_other_skill_record_is_allowed_and_not_recorded(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"), skill_line="other-skill")
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name, session_id="other"))
        self.assertIsNone(result)
        state = self.read_state("other")
        self.assertEqual(state["workdirs"], [])

    def test_matching_skill_record_is_checked_normally(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"), skill_line="test-skill")
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name, session_id="match"))
        self.assertIsNone(result)
        state = self.read_state("match")
        self.assertIn(os.path.normpath(self.workdir), state["workdirs"])

    def test_record_without_skill_line_is_backward_compatible(self):
        write_hearing(os.path.join(self.workdir, "hearing.md"), skill_line=None)
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name, session_id="nolabel"))
        self.assertIsNone(result)
        state = self.read_state("nolabel")
        self.assertIn(os.path.normpath(self.workdir), state["workdirs"])

    def test_other_skill_record_that_is_incomplete_is_still_allowed(self):
        # 他スキルの記録なら、たとえ未確定でも自スキルは口を出さない
        write_hearing(os.path.join(self.workdir, "hearing.md"), skill_line="other-skill", status="未確定")
        cmd = "python3 tool.py %s" % self.workdir
        result = G.handle(self.config, event(cmd, self.tmp.name, session_id="other2"))
        self.assertIsNone(result)


class ExtractPositionalsTest(unittest.TestCase):
    """G-3: taidan の argparse 実定義（--only は nargs='*'）に合わせた位置引数抽出。"""

    def _tokens(self, s):
        import shlex
        return shlex.split(s)

    def test_option_with_equals_does_not_consume_next_token(self):
        tokens = self._tokens("--out=report.txt WORKDIR clips.py dest")
        positionals = G.extract_positionals(tokens, 0, len(tokens), 3, {"--out": 1})
        self.assertEqual(positionals, ["WORKDIR", "clips.py", "dest"])

    def test_double_dash_terminator_makes_rest_positional(self):
        tokens = self._tokens("-- src.mp4 WORKDIR clips.py")
        positionals = G.extract_positionals(tokens, 0, len(tokens), 3, {})
        self.assertEqual(positionals, ["src.mp4", "WORKDIR", "clips.py"])

    def test_star_option_trailing_after_positionals(self):
        tokens = self._tokens("src.mp4 WORKDIR clips.py --only slug")
        positionals = G.extract_positionals(tokens, 0, len(tokens), 3, {"--only": "*"})
        self.assertEqual(positionals, ["src.mp4", "WORKDIR", "clips.py"])

    def test_star_option_leading_before_positionals_reserves_required_count(self):
        # 実データでの再現コマンド: --only が2値を取り、残り3つが位置引数になる
        tokens = self._tokens("--only a b src.mp4 WORKDIR clips.py --cta cta.mp4")
        positionals = G.extract_positionals(tokens, 0, len(tokens), 3, {"--only": "*", "--cta": 1})
        self.assertEqual(positionals, ["src.mp4", "WORKDIR", "clips.py"])

    def test_unknown_option_defaults_to_single_value_consumption(self):
        tokens = self._tokens("--unknown x src.mp4 WORKDIR clips.py")
        positionals = G.extract_positionals(tokens, 0, len(tokens), 3, {})
        self.assertEqual(positionals, ["src.mp4", "WORKDIR", "clips.py"])


class ProductionRuleGlobTest(unittest.TestCase):
    """G-1/G-2: basenameのglob一致（部分一致廃止・render_new.py等の亜種も一致）。"""

    def test_glob_matches_variant_names(self):
        rule = G.ProductionRule(("render*.py",))
        self.assertTrue(rule.matches("render_new.py"))
        self.assertTrue(rule.matches("render.py"))
        self.assertFalse(rule.matches("prerender.py"))
        self.assertFalse(rule.matches("other.py"))

    def test_inline_python_c_is_never_production(self):
        config = make_config()
        with tempfile.TemporaryDirectory() as tmp:
            result = G.handle(config, event('python3 -c "import tool; tool.render()"', tmp))
            self.assertIsNone(result)

    def test_viewing_script_with_cat_is_not_production(self):
        config = make_config()
        with tempfile.TemporaryDirectory() as tmp:
            result = G.handle(config, event("cat tool.py", tmp))
            self.assertIsNone(result)


class MalformedInputTest(unittest.TestCase):
    def test_malformed_json_is_allowed_via_main(self):
        config = make_config()
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            G.main(config, stdin=io.StringIO("{not json"))
        finally:
            sys.stdout = old_stdout
        self.assertEqual(out.getvalue().strip(), "")

    def test_top_level_array_is_allowed_via_main(self):
        config = make_config()
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            G.main(config, stdin=io.StringIO(json.dumps(["a", "b"])))
        finally:
            sys.stdout = old_stdout
        self.assertEqual(out.getvalue().strip(), "")


if __name__ == "__main__":
    unittest.main()
