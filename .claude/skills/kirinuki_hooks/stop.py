# -*- coding: utf-8 -*-
"""Stop フックの共通実装。
mp4 を書き出した作業フォルダで改善フェーズ（DREAM.md）が完了しているか確認し、
未完了なら終了を差し戻す。

このフックは「改善フェーズを忘れる事故」を防ぐガードであり、
悪意ある回避を防ぐセキュリティ境界ではない。記録ファイルが読めない場合も
「未完了」として安全側（block）に倒す。

前回差し戻した時点から out/*.mp4 の集合（名前＋サイズ）が変わっていなければ、
同じ注意を繰り返さない（うるさくしすぎない）。
"""
import glob
import json
import os
import sys

from . import asks as asks_mod
from . import records
from . import state as state_mod

_RESERVATION_QUESTION_SUBSTRING = "予約投稿しますか"


class StopConfig:
    def __init__(
        self,
        skill_name,
        required_improve_headings,
        require_artifact=False,
        require_reservation_question=False,
    ):
        self.skill_name = skill_name
        self.required_improve_headings = required_improve_headings
        # 完成動画ページ必須: out/*.mp4 があるのに $workdir/artifact.json が無ければ差し戻す
        self.require_artifact = require_artifact
        # 予約確認必須: セッションのAskUserQuestionログに「予約投稿しますか」を含む
        # 質問への回答（採否は問わない）が無ければ差し戻す
        self.require_reservation_question = require_reservation_question


def _is_still_writing(path):
    """サイズ0バイトのファイルは「書き出し中」とみなして検査対象から除外する。
    更新時刻（mtime）は使わない：書き出し直後に終了すれば猶予内として素通りできてしまうため。
    """
    try:
        return os.path.getsize(path) == 0
    except OSError:
        return True  # 統計できない＝対象から除外（書き出し中扱い）


def _finished_mp4_fingerprint(workdir):
    """完成済み（0バイトでない）out/*.mp4 の [名前, サイズ] の一覧（名前順）を返す。"""
    fingerprint = []
    for path in sorted(glob.glob(os.path.join(workdir, "out", "*.mp4"))):
        if _is_still_writing(path):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        fingerprint.append([os.path.basename(path), size])
    return fingerprint


def _read_improve_text(path):
    """(text, 読み取り失敗の理由 or None) を返す。ファイルが無いのは失敗扱いしない。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), None
    except FileNotFoundError:
        return "", None
    except IsADirectoryError:
        return "", "%s はファイルではなくディレクトリです" % path
    except UnicodeDecodeError as exc:
        return "", "%s を UTF-8 として読み込めませんでした（%s）" % (path, exc)
    except OSError as exc:
        return "", "%s を読み込めませんでした（%s）" % (path, exc)


def _improve_reason(
    workdir, missing, status_ok, status_problem, read_error, rejection_problems,
    artifact_missing=False, reservation_missing=False,
):
    unmet = list(missing)
    if not status_ok:
        unmet.append(status_problem)
    if rejection_problems:
        unmet.append("理由の無い却下（%s）" % "／".join(rejection_problems))
    if artifact_missing:
        unmet.append("完成動画アーティファクト（%s/artifact.json）が未作成" % workdir)
    if reservation_missing:
        unmet.append("DREAM.md 1.5「予約投稿しますか」への回答が未記録")
    reason = (
        "切り抜きを書き出しました（%s）。終える前に、ユーザーにFBを聞き、"
        "DREAM.md の手順で改善フェーズを実施して %s/improve.md を 状態: 完了 にしてください。"
        "未充足：%s"
    ) % (workdir, workdir, "、".join(unmet))
    if rejection_problems:
        reason += "。理由の無い却下は『保留』に書いてください"
    if artifact_missing:
        reason += "。SKILL.md「完成動画アーティファクト」の手順でページを公開してください"
    if reservation_missing:
        reason += "。DREAM.md 1.5 のAskUserQuestionで確認してください（予約する／今回はしない、どちらでもよい）"
    if read_error:
        reason += "（%s）" % read_error
    return reason


def _block(reason):
    return {"decision": "block", "reason": reason}


def _check_workdirs(config, session_id, state):
    for workdir in state["workdirs"]:
        if not os.path.isdir(workdir):
            continue
        fingerprint = _finished_mp4_fingerprint(workdir)
        if not fingerprint:
            continue

        improve_path = os.path.join(workdir, "improve.md")
        text, read_error = _read_improve_text(improve_path)

        owner = records.record_skill(text)
        if owner is not None and owner != config.skill_name:
            continue  # 他スキルの記録：自スキルの検査対象ではない

        ok, missing, status_ok, status_problem = records.validate_record(
            text, config.required_improve_headings, "完了"
        )
        rejection_ok, rejection_problems = records.validate_improve_rejections(text)

        artifact_missing = config.require_artifact and not os.path.isfile(
            os.path.join(workdir, "artifact.json")
        )
        reservation_missing = config.require_reservation_question and asks_mod.find_answered_question(
            session_id, lambda entry: _RESERVATION_QUESTION_SUBSTRING in (entry.get("question") or "")
        ) is None

        if ok and rejection_ok and not artifact_missing and not reservation_missing:
            continue

        last_notified = state_mod.get_notified_fingerprint(config.skill_name, session_id, workdir)
        if last_notified == fingerprint:
            continue  # 前回差し戻した時点から集合が変わっていない → 繰り返し差し戻さない

        state_mod.set_notified_fingerprint(config.skill_name, session_id, workdir, fingerprint)
        return _block(_improve_reason(
            workdir, missing, status_ok, status_problem, read_error, rejection_problems,
            artifact_missing=artifact_missing, reservation_missing=reservation_missing,
        ))
    return None


def handle(config, data):
    """イベント(dict)を検査し、blockすべきなら出力用dictを、許可なら None を返す。"""
    if data.get("stop_hook_active"):
        return None

    session_id = data.get("session_id")
    if not session_id:
        return None

    try:
        state = state_mod.load_state(config.skill_name, session_id)
        return _check_workdirs(config, session_id, state)
    except Exception as exc:  # 想定外の例外は安全側（block）に倒す
        return _block("改善フェーズの確認中にエラーが発生しました。安全のため終了を差し戻します: %s" % exc)


def main(config, stdin=None):
    raw = (stdin or sys.stdin).read()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    result = handle(config, data)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))
