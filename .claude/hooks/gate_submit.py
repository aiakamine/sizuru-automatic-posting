#!/usr/bin/env python3
"""PreToolUse（Bash）フック: `schedule.py submit <plan.json> --yes` を検知し、
実際にBufferへ予約される前に2つの条件を強制する。

1. 本編リンク必須: plan.jsonの各クリップに有効な main_video_url があり、
   X/TikTok/Instagramの全キャプションに本編URLがそのまま含まれていること
   （TikTok/Instagramはリンクをクリック可能にできないが、プロフィールリンク
   は設定しない方針のため、キャプション本文にURLをテキストとして含める）
2. 本編URL提示＋承認必須: このセッションの AskUserQuestion ログ（kirinuki_hooks.asks）に、
   plan.json の更新時刻より後に答えられた質問で、本文にplanの全ての実効
   main_video_url を含み、選ばれた回答が「承認して予約」で始まるものがあること
   （plan.json を編集し直すと mtime が進むので、再承認が必要になる）

このフックは「未承認・未検証のまま実際に投稿予約が飛ぶ事故」を防ぐガードである。
自分の検査対象と判定した呼び出し（schedule.py submit --yes）については
安全側（deny）に倒すが、それ以外の無関係なBashコマンドの解析に失敗しても
検査せず許可する（systemMessageで知らせる）。
"""
import json
import os
import sys


def _find_skills_dir():
    """kirinuki_hooks と sns-schedule/scripts の両方を含む .claude/skills を探す。
    探索順: ①このファイルの親の隣（このリポジトリの実配置） ②$CLAUDE_PROJECT_DIR/.claude/skills
    ③~/.claude/skills
    """
    here = os.path.dirname(os.path.realpath(__file__))  # .../.claude/hooks
    candidates = [os.path.normpath(os.path.join(here, "..", "skills"))]
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidates.append(os.path.join(project_dir, ".claude", "skills"))
    candidates.append(os.path.expanduser("~/.claude/skills"))
    for parent in candidates:
        if os.path.isfile(os.path.join(parent, "kirinuki_hooks", "asks.py")) and os.path.isfile(
            os.path.join(parent, "sns-schedule", "scripts", "schedule_core.py")
        ):
            return parent
    return None


_skills_dir = _find_skills_dir()
if _skills_dir is not None:
    sys.path.insert(0, _skills_dir)
    sys.path.insert(0, os.path.join(_skills_dir, "sns-schedule", "scripts"))
    import schedule_core  # noqa: E402
    from kirinuki_hooks import (
        asks,  # noqa: E402
        gate,  # noqa: E402
    )
else:
    asks = None
    gate = None
    schedule_core = None

_APPROVAL_LABEL_PREFIX = "承認して予約"


# --- コマンド解析（gate.py の下位関数を再利用し、submit専用の抽出を上に足す） ---


def _extract_submit_positionals(tokens):
    """`submit` 直後のトークン列から (positionals, has_yes) を取り出す。
    --yes は値を取らない store_true フラグなので、gate.extract_positionals の
    汎用オプション処理（未知の --x は既定で1値消費）をそのまま使うと誤動作する
    （直後の plan.json パスを値として飲み込んでしまう）ため、ここだけ専用に書く。
    現状 submit のオプションは --yes のみ。
    """
    positionals = []
    has_yes = False
    for token in tokens:
        if token == "--yes":
            has_yes = True
            continue
        if token.startswith("--"):
            continue
        positionals.append(token)
    return positionals, has_yes


def _process_segment_for_submit(tokens, env, cwd):
    """gate.py の _process_segment と同じ構造（変数代入・cd追跡）で、
    `python... schedule.py submit ...` 呼び出しだけを見つける。
    戻り値: (新しい cwd, {"plan_path":.., "yes": bool} または None)
    """
    i, n = 0, len(tokens)
    while i < n:
        token = tokens[i]
        if token == "export":
            i += 1
            continue
        match = gate._VAR_ASSIGN_RE.match(token)
        if match:
            name = token.split("=", 1)[0]
            env[name] = gate._expand(match.group(1), env)
            i += 1
            continue
        break
    if i >= n:
        return cwd, None

    exe = tokens[i]
    if exe == "cd":
        target = tokens[i + 1] if i + 1 < n else None
        if target is None:
            return cwd, None
        return gate._resolve_path(target, cwd, env), None

    if not gate._is_python_executable(exe):
        return cwd, None

    j = i + 1
    script_index = None
    while j < n:
        tok = tokens[j]
        if tok.startswith("-"):
            j += 1
            continue
        if os.path.basename(tok) == "schedule.py":
            script_index = j
        break
    if script_index is None:
        return cwd, None
    if script_index + 1 >= n or tokens[script_index + 1] != "submit":
        return cwd, None

    positionals, has_yes = _extract_submit_positionals(tokens[script_index + 2:n])
    if not positionals:
        return cwd, None
    plan_path = gate._resolve_path(positionals[0], cwd, env)
    return cwd, {"plan_path": plan_path, "yes": has_yes}


def _find_submit_calls(command, cwd):
    """コマンド文字列から submit --yes 呼び出しを全て見つける。
    トークン化に失敗した場合（クォート閉じ忘れ等）は ValueError を送出する
    （このコマンドがそもそも submit を含むか判定できないため、呼び出し側は
    これを「許可」として扱う ―― 無関係なコマンドを壊さないため）。
    """
    tokens = gate._tokenize(command)
    segments = gate._split_segments(tokens)
    env = {}
    current_cwd = cwd
    calls = []
    for seg in segments:
        current_cwd, call = _process_segment_for_submit(seg, env, current_cwd)
        if call is not None:
            calls.append(call)
    return calls


# --- 本編リンク必須 ---


def _main_video_url_problems(plan):
    """plan.jsonの本編リンク要件の違反を集める。[(clip_no, message), ...]（空なら合格）。"""
    problems = []
    for clip in plan.get("clips", []):
        no = clip.get("no")
        main_video_url = schedule_core.resolve_main_video_url(plan, clip)
        if not main_video_url:
            problems.append((no, "main_video_url がありません（本編のYouTube URLを指定してください）"))
            continue
        for target in clip.get("targets", []):
            service = target.get("service")
            caption = target.get("caption") or ""
            if service in ("x", "tiktok", "instagram") and main_video_url not in caption:
                problems.append((no, f"{service}のキャプションに本編URLが含まれていません"))
    return problems


# --- 承認必須 ---


def _effective_urls(plan):
    urls = set()
    for clip in plan.get("clips", []):
        url = schedule_core.resolve_main_video_url(plan, clip)
        if url:
            urls.add(url)
    return urls


def _approval_ok(session_id, plan, plan_mtime):
    urls = _effective_urls(plan)
    if not urls:
        return False

    def predicate(entry):
        question = entry.get("question") or ""
        return all(url in question for url in urls)

    found = asks.find_answered_question(session_id, predicate, min_ts=plan_mtime)
    if found is None:
        return False
    answer = found.get("answer") or ""
    return answer.strip().startswith(_APPROVAL_LABEL_PREFIX)


# --- JSON 入出力 ---


def _deny(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _check_submit_call(plan_path, session_id):
    try:
        with open(plan_path, encoding="utf-8") as f:
            plan_text = f.read()
    except OSError as exc:
        return _deny(f"plan.json（{plan_path}）を読み込めませんでした: {exc}")
    try:
        plan = json.loads(plan_text)
    except ValueError as exc:
        return _deny(f"plan.json（{plan_path}）がJSONとして読み込めません: {exc}")
    if not isinstance(plan, dict) or not isinstance(plan.get("clips"), list):
        return _deny(f"plan.json（{plan_path}）の形式が不正です（clipsがありません）")

    problems = _main_video_url_problems(plan)
    if problems:
        lines = [f"クリップ{no}: {msg}" for no, msg in problems]
        return _deny(
            "本編（YouTube）への誘導が不足しています。sns-schedule/SKILL.md 手順4に従って"
            "plan.jsonのキャプション・main_video_urlを直し、validateで確認してから"
            "再実行してください:\n" + "\n".join(lines)
        )

    if not session_id:
        return _deny("session_idが取得できないため承認状況を確認できません")

    try:
        plan_mtime = os.path.getmtime(plan_path)
    except OSError as exc:
        return _deny(f"plan.json（{plan_path}）の更新時刻を取得できませんでした: {exc}")

    if not _approval_ok(session_id, plan, plan_mtime):
        return _deny(
            f"本編URLを含む内容についてAskUserQuestionで「{_APPROVAL_LABEL_PREFIX}」の承認を、"
            "plan.jsonの最終更新後に得てから submit --yes を実行してください"
            "（plan.jsonを編集した場合は再承認が必要です）"
        )
    return None


def handle(command, cwd, session_id):
    """コマンド(str)を検査し、denyすべきなら出力用dictを、許可なら None を返す。
    schedule.py submit --yes を含まないコマンドは常に None（許可）。
    """
    calls = _find_submit_calls(command, cwd)
    yes_calls = [c for c in calls if c["yes"]]
    if not yes_calls:
        return None
    for call in yes_calls:
        result = _check_submit_call(call["plan_path"], session_id)
        if result is not None:
            return result
    return None


def main(stdin=None):
    if _skills_dir is None:
        print(
            json.dumps(
                {
                    "systemMessage": (
                        "sns-schedule submitゲートの依存（kirinuki_hooks / sns-schedule）が"
                        "見つからないため、この呼び出しは検査していません。"
                    )
                },
                ensure_ascii=False,
            )
        )
        return

    raw = (stdin or sys.stdin).read()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    if data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    command = tool_input.get("command")
    if not isinstance(command, str):
        return
    cwd = data.get("cwd") or os.getcwd()
    session_id = data.get("session_id")

    try:
        result = handle(command, cwd, session_id)
    except Exception as exc:
        # ここに来るのは submit --yes だと確定する前（コマンド解析中）の失敗を含む。
        # 自分の検査対象かどうかも判定できていないので、無関係なコマンドを壊さない
        # よう許可する（denyしない）。systemMessageで検査していないことだけ知らせる。
        message = f"submitコマンドの解析に失敗したため、この呼び出しは検査していません: {exc}"
        print(json.dumps({"systemMessage": message}, ensure_ascii=False))
        return
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
