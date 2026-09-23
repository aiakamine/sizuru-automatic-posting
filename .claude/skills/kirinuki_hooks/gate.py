# -*- coding: utf-8 -*-
"""PreToolUse（Bash）フックの共通実装。
制作コマンド（スキルごとに定義された制作スクリプト）の実行前に、対象の作業フォルダの
hearing.md が「状態: 確定」かつ必須見出しが全て埋まっていることを強制する。

このフックは「ヒアリング/改善フェーズを忘れる事故」を防ぐガードであり、
悪意ある回避を防ぐセキュリティ境界ではない。判定できない場合は安全側（deny）に倒す。
"""
import fnmatch
import json
import os
import re
import shlex
import sys

from . import records
from . import state as state_mod

_OPERATORS = ("&&", "&", "||", ";", "|")
_VAR_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(.*)$")
_VAR_REF_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_PYTHON_EXE_RE = re.compile(r"^python\d*(\.\d+)?$")

HEARING_GUIDANCE = "references/hearing.md の手順でヒアリングし、templates/hearing.md をコピーして記録してください"


class GateError(Exception):
    """このメッセージのまま deny 理由として使う、読み取り失敗など既知の不合格。"""


class ProductionRule:
    """制作コマンドとして扱うスクリプトの定義。
    basename_globs: 実行スクリプトのbasenameに対する fnmatch パターン群（例: "render*.py"）
    workdir_index: 位置引数のうち作業フォルダを表すインデックス（0始まり）。
                   None なら位置引数からは取れない（cd の累積＝現在地を使う）
    required_positionals: そのスクリプトが要求する位置引数の総数（workdir_indexがNoneなら無視）
    option_arities: {"--opt": 1} は固定1値、{"--opt": "*"} はargparseのnargs='*'相当
    """

    def __init__(self, basename_globs, workdir_index=None, required_positionals=0, option_arities=None):
        self.basename_globs = tuple(basename_globs)
        self.workdir_index = workdir_index
        self.required_positionals = required_positionals
        self.option_arities = option_arities or {}

    def matches(self, basename):
        return any(fnmatch.fnmatch(basename, pattern) for pattern in self.basename_globs)


class GateConfig:
    def __init__(self, skill_name, required_hearing_headings, production_rules):
        self.skill_name = skill_name
        self.required_hearing_headings = required_hearing_headings
        self.production_rules = production_rules


# --- コマンド文字列の解析 ---

def _tokenize(command):
    """shlex でトークン化する。クォート未閉じ等は ValueError を送出する（呼び出し側で処理）。"""
    lex = shlex.shlex(command, posix=True, punctuation_chars="&;|")
    lex.whitespace_split = True
    return list(lex)


def _split_segments(tokens):
    """`&&` `;` `|` 等の演算子でトークン列をセグメントに分ける。"""
    segments = []
    current = []
    for token in tokens:
        if token in _OPERATORS:
            segments.append(current)
            current = []
        else:
            current.append(token)
    segments.append(current)
    return [seg for seg in segments if seg]


def _expand(value, env):
    def repl(match):
        name = match.group(1) or match.group(2)
        return env.get(name, match.group(0))
    return _VAR_REF_RE.sub(repl, value)


def _resolve_path(raw, cwd, env):
    """変数展開・`~`展開のうえ、相対パスなら cwd 基準で絶対化する。"""
    expanded = os.path.expanduser(_expand(raw, env))
    if os.path.isabs(expanded):
        return expanded
    return os.path.normpath(os.path.join(cwd, expanded))


def _is_python_executable(token):
    return bool(_PYTHON_EXE_RE.match(os.path.basename(token)))


def extract_positionals(tokens, start, end, required_count, option_arities):
    """[start, end) からオプションを除いた位置引数を、必要数ぶんだけ順序通りに取り出す。

    argparse の nargs='*' は「後続の必須位置引数のために残りが十分あるか」で貪欲さが
    決まる（十分な残数が無ければ位置引数を優先する）。ここでも同じ規則で近似する。
    `--opt=value` は次のトークンを消費しない。単独の `--` 以降は全て位置引数。
    未知の `--x` は既定で1値消費する（安全側）。
    """
    items = []  # ("bare"|"star", token)
    i = start
    force_positional = False
    active_star = False
    while i < end:
        token = tokens[i]
        if not force_positional and token == "--":
            force_positional = True
            active_star = False
            i += 1
            continue
        if not force_positional and token.startswith("--"):
            name = token.split("=", 1)[0]
            arity = option_arities.get(name, 1)
            if arity == "*":
                active_star = True
                i += 1
                continue
            active_star = False
            if "=" in token:
                i += 1  # 値はトークンに埋め込み済み
            else:
                i += 1
                if i < end and not tokens[i].startswith("--"):
                    i += 1  # 直後の値トークンも読み飛ばす
            continue
        items.append(("star" if active_star else "bare", token))
        i += 1

    total = len(items)
    positionals = []
    remaining_needed = required_count
    remaining_items = total
    for kind, token in items:
        remaining_items -= 1
        if remaining_needed == 0:
            continue
        slack = remaining_items - remaining_needed
        if kind == "star" and slack >= 0:
            continue  # まだ余裕がある間は貪欲オプション側に譲る
        positionals.append(token)
        remaining_needed -= 1
    return positionals


def _find_production_call(tokens, exe_index, seg_end, production_rules):
    """python 実行セグメントが制作コマンドかどうかを判定する。
    戻り値: (rule, workdir_raw) または (None, None)。
    `-c` の中身は基準にしない（部分一致で無関係なコマンドを拒否する事故を避けるため）。
    """
    i = exe_index + 1
    while i < seg_end:
        token = tokens[i]
        if token.startswith("-"):
            i += 1
            continue
        base = os.path.basename(token)
        for rule in production_rules:
            if rule.matches(base):
                if rule.workdir_index is None:
                    return rule, None
                positionals = extract_positionals(
                    tokens, i + 1, seg_end, rule.required_positionals, rule.option_arities
                )
                if len(positionals) > rule.workdir_index:
                    return rule, positionals[rule.workdir_index]
                return rule, None
        return None, None  # 実行スクリプトが見つかったが、どのルールにも一致しない
    return None, None


def _process_segment(tokens, env, cwd, production_rules):
    """セグメントを評価する。env は破壊的に更新する。
    戻り値: (新しい cwd, 制作呼び出し {"workdir":..} または None)
    """
    i, n = 0, len(tokens)
    while i < n:
        token = tokens[i]
        if token == "export":
            i += 1
            continue
        match = _VAR_ASSIGN_RE.match(token)
        if match:
            name = token.split("=", 1)[0]
            env[name] = _expand(match.group(1), env)
            i += 1
            continue
        break
    if i >= n:
        return cwd, None  # 代入だけのセグメント

    exe = tokens[i]
    if exe == "cd":
        target = tokens[i + 1] if i + 1 < n else None
        if target is None:
            return cwd, None
        return _resolve_path(target, cwd, env), None

    if not _is_python_executable(exe):
        return cwd, None

    rule, workdir_raw = _find_production_call(tokens, i, n, production_rules)
    if rule is None:
        return cwd, None

    workdir = cwd if workdir_raw is None else _resolve_path(workdir_raw, cwd, env)
    return cwd, {"workdir": workdir}


# --- hearing.md の読み取りと検査 ---

def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        raise GateError("%s がありません。%s" % (path, HEARING_GUIDANCE))
    except IsADirectoryError:
        raise GateError("%s はファイルではなくディレクトリです。%s" % (path, HEARING_GUIDANCE))
    except UnicodeDecodeError as exc:
        raise GateError("%s を UTF-8 として読み込めませんでした（%s）。%s" % (path, exc, HEARING_GUIDANCE))
    except OSError as exc:
        raise GateError("%s を読み込めませんでした（%s）。%s" % (path, exc, HEARING_GUIDANCE))


def _hearing_reason(workdir, missing, status_ok, status_problem):
    problems = []
    if not status_ok:
        problems.append(status_problem)
    if missing:
        problems.append("未入力の見出し: %s" % "、".join(missing))
    return "%s/hearing.md の記録が未確定です（%s）。%s" % (
        workdir, " / ".join(problems), HEARING_GUIDANCE
    )


def _check_production_call(config, call):
    """1件の制作呼び出しを検査する。合格なら workdir、他スキルの記録なら None、
    不合格なら GateError を送出する。
    """
    workdir = call["workdir"]
    if "$" in workdir:
        raise GateError(
            "作業フォルダを特定できません。"
            "同じコマンド内で D=/絶対パス を定義するか絶対パスで書いてください"
        )
    hearing_path = os.path.join(workdir, "hearing.md")
    text = _read_text(hearing_path)

    owner = records.record_skill(text)
    if owner is not None and owner != config.skill_name:
        return None  # 他スキルの記録：自スキルの検査対象ではない

    ok, missing, status_ok, status_problem = records.validate_record(
        text, config.required_hearing_headings, "確定"
    )
    if not ok:
        raise GateError(_hearing_reason(workdir, missing, status_ok, status_problem))
    return workdir


def _evaluate_command(config, command, cwd):
    """コマンド全体を評価し、合格した制作呼び出しの workdir 一覧を返す。
    不合格・解析失敗の場合は GateError / ValueError を送出する。
    """
    tokens = _tokenize(command)
    segments = _split_segments(tokens)
    env = {}
    current_cwd = cwd
    passed = []
    for seg in segments:
        current_cwd, call = _process_segment(seg, env, current_cwd, config.production_rules)
        if call is not None:
            workdir = _check_production_call(config, call)
            if workdir is not None:
                passed.append(workdir)
    return passed


# --- JSON 入出力 ---

def _deny(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def handle(config, data):
    """イベント(dict)を検査し、denyすべきなら出力用dictを、許可なら None を返す。
    ここに来る前に、data が dict であることは呼び出し側で確認済みとする。
    """
    if data.get("tool_name") != "Bash":
        return None

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None

    cwd = data.get("cwd") or os.getcwd()
    try:
        passed = _evaluate_command(config, command, cwd)
    except GateError as exc:
        return _deny(str(exc))
    except ValueError as exc:
        return _deny("コマンドを解析できませんでした（クォートの閉じ忘れ等）: %s" % exc)
    except Exception as exc:  # 想定外の例外は安全側（deny）に倒す
        return _deny("制作コマンドの検査中にエラーが発生しました: %s" % exc)

    if not passed:
        return None

    session_id = data.get("session_id")
    if not session_id:
        return None
    try:
        for workdir in passed:
            state_mod.add_workdir(config.skill_name, session_id, workdir)
    except OSError as exc:
        return _deny("状態ファイルへの記録に失敗しました: %s" % exc)
    return None


def main(config, stdin=None):
    raw = (stdin or sys.stdin).read()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return  # 壊れたJSONは許可（このフックでは判断できない）
    if not isinstance(data, dict):
        return
    result = handle(config, data)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))
