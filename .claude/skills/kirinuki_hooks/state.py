# -*- coding: utf-8 -*-
"""フックのセッション状態ファイルの読み書き（原子的書き込み・破損耐性）。
状態はスキルごとに分ける（skill_name でディレクトリを分ける）。
"""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

STATE_DIR_ENV = "KIRINUKI_STATE_DIR"

_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def state_root():
    """状態ディレクトリのルート（KIRINUKI_STATE_DIR があればそれ、無ければ既定のホーム配下）。
    スキル固有ディレクトリ（_state_dir）の親にあたる。asks.py 等、スキルをまたいだ
    共有ログ（例: "asks/"）を state_root() 配下の別名前空間に置くために公開している。
    """
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".claude" / "kirinuki-state"


def _state_dir(skill_name):
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".claude" / "kirinuki-state" / skill_name


def _session_filename(session_id, suffix=".json"):
    """session_id をそのままファイル名にできない場合はsha256でハッシュ化する。"""
    if _SAFE_SESSION_ID_RE.match(session_id):
        return "%s%s" % (session_id, suffix)
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return "%s%s" % (digest, suffix)


def _state_path(skill_name, session_id):
    return _state_dir(skill_name) / _session_filename(session_id)


def _empty_state():
    return {"workdirs": [], "notified": {}}


def load_state(skill_name, session_id):
    """{"workdirs": [...], "notified": {workdir: [[name,size],...]}} を返す。
    状態ファイルが無い/壊れている場合は空の状態を返す（落ちない）。
    """
    path = _state_path(skill_name, session_id)
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()

    workdirs = data.get("workdirs")
    if not isinstance(workdirs, list):
        workdirs = []
    workdirs = [w for w in workdirs if isinstance(w, str)]

    notified = data.get("notified")
    if not isinstance(notified, dict):
        notified = {}
    clean_notified = {}
    for key, value in notified.items():
        if isinstance(key, str) and isinstance(value, list):
            clean_notified[key] = value
    return {"workdirs": workdirs, "notified": clean_notified}


def _write_state(skill_name, session_id, state):
    """同ディレクトリの一時ファイル→os.replace で原子的に書く。"""
    path = _state_path(skill_name, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
        os.replace(tmp_path, path)
    except OSError:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def add_workdir(skill_name, session_id, workdir):
    """状態ファイルに workdir を重複なく追記する。"""
    state = load_state(skill_name, session_id)
    if workdir not in state["workdirs"]:
        state["workdirs"].append(workdir)
    _write_state(skill_name, session_id, state)


def get_notified_fingerprint(skill_name, session_id, workdir):
    """前回この workdir について差し戻した時点の out/*.mp4 の指紋（[[name,size],...]）を返す。
    記録が無ければ None。
    """
    state = load_state(skill_name, session_id)
    return state["notified"].get(workdir)


def set_notified_fingerprint(skill_name, session_id, workdir, fingerprint):
    """差し戻した時点の out/*.mp4 の指紋を記録する（次回、同じ集合なら差し戻さないため）。"""
    state = load_state(skill_name, session_id)
    state["notified"][workdir] = fingerprint
    _write_state(skill_name, session_id, state)
