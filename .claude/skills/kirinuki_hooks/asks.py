# -*- coding: utf-8 -*-
"""AskUserQuestion の質問・回答ログ（セッションごとのJSONL）。

record_asks.py（PostToolUse/AskUserQuestion、プロジェクト全体で有効）が書き込み、
sns-schedule の submit ゲート・kirinuki の予約確認ゲート（Stop/Write）が読む、
「ユーザーがある質問にどう答えたか」を横断的に参照するための共通実装。

PostToolUse payload の実データ形（このリポジトリでの観測値）:
    tool_response = {
        "questions": [{"question": str, "header": str,
                        "options": [{"label": str, "description": str}, ...],
                        "multiSelect": bool}, ...],
        "answers": {"<質問文そのまま>": "<回答文字列>", ...},
        "annotations": {},
    }
多選択の回答は "A, B, C" のようにカンマ区切りで1つの文字列に結合されて来る。
「その他」の自由記入もそのままの文字列（改行を含みうる）で来る。
tool_response が無ければ tool_result → tool_output の順にフォールバックし、
値が JSON 文字列、または {"type":"text","text":"<json>"} の形（他ツールの
payload でよく見る形）であればデコードする。それでも質問一覧が取れなければ
tool_input.questions（AskUserQuestion 呼び出し時の入力）にフォールバックする。
"""
import json
import time

from . import state as state_mod


def _asks_path(session_id):
    filename = state_mod._session_filename(session_id, ".jsonl")
    return state_mod.state_root() / "asks" / filename


def _debug_path():
    return state_mod.state_root() / "asks" / "_debug_first_payload.json"


def _try_parse_json(text):
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_to_dict(value):
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return _coerce_to_dict(_try_parse_json(value["text"]))
        return value
    if isinstance(value, str):
        return _try_parse_json(value)
    return None


def extract_response(data):
    """PostToolUse payload(dict) から AskUserQuestion のレスポンス相当オブジェクトを
    取り出す。tool_response → tool_result → tool_output の順に見る。無ければ None。
    """
    for key in ("tool_response", "tool_result", "tool_output"):
        parsed = _coerce_to_dict(data.get(key))
        if parsed is not None:
            return parsed
    return None


def _extract_questions(data, response):
    questions = (response or {}).get("questions")
    if isinstance(questions, list):
        return questions
    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("questions"), list):
        return tool_input["questions"]
    return []


def build_entries(data, now=None):
    """PostToolUse payload から、記録すべきJSONLエントリ（1問1行）のリストを作る。
    質問が1つも取れなければ空リストを返す（例外は投げない）。
    """
    response = extract_response(data)
    questions = _extract_questions(data, response)
    answers = (response or {}).get("answers")
    if not isinstance(answers, dict):
        answers = {}
    ts = time.time() if now is None else now
    entries = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_text = question.get("question")
        if not isinstance(question_text, str):
            continue
        options = question.get("options")
        entries.append({
            "ts": ts,
            "question": question_text,
            "header": question.get("header"),
            "options": options if isinstance(options, list) else [],
            "answer": answers.get(question_text),
        })
    return entries


def _record_debug_snapshot_once(data):
    """初回だけ、生payloadのキー構成をファイルに残す（stdoutには一切出さない。
    PostToolUseの標準出力はhookプロトコルの一部なので、デバッグ用の余計な
    出力を混ぜない）。書き込みに失敗しても記録処理自体は失敗させない。
    """
    debug_path = _debug_path()
    if debug_path.exists():
        return
    try:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "top_level_keys": sorted(data.keys()),
            "tool_response_type": type(data.get("tool_response")).__name__,
            "tool_result_type": type(data.get("tool_result")).__name__,
            "tool_output_type": type(data.get("tool_output")).__name__,
        }
        debug_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def record(data, now=None):
    """PostToolUse(AskUserQuestion) の payload からエントリを作って追記する。
    session_id が無い、または記録すべきエントリが1つも無ければ何もしない。
    """
    session_id = data.get("session_id")
    if not session_id:
        return
    entries = build_entries(data, now=now)
    if not entries:
        return
    path = _asks_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _record_debug_snapshot_once(data)


def load_entries(session_id):
    """このセッションの質問・回答エントリを記録順で返す（壊れた行は無視する）。"""
    path = _asks_path(session_id)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def find_answered_question(session_id, predicate, min_ts=None):
    """`predicate(entry) -> bool` を満たし、回答が空でなく、`min_ts` が指定
    されていればその時刻以降に答えられたエントリを最初に見つけたものを返す
    （無ければ None）。"""
    for entry in load_entries(session_id):
        answer = entry.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            continue
        if min_ts is not None:
            ts = entry.get("ts")
            if not isinstance(ts, (int, float)) or ts < min_ts:
                continue
        if predicate(entry):
            return entry
    return None
