# -*- coding: utf-8 -*-
"""scripts/*.py から共通パッケージ kirinuki_core を見つけて sys.path に足す薄いヘルパー。
探索順は kirinuki_hooks（hooks/gate_hearing.py等）の _find_common_package_parent と同じ:
①スキルディレクトリの親（リポジトリ内配置） ②$CLAUDE_PROJECT_DIR ③~/.claude
見つからない場合はフックのように黙って素通りせず、案内を出して呼び出し元を止める
（align_check.py等はkirinuki_core無しでは動作できないため）。
"""
import os
import sys


def find_kirinuki_core_parent():
    """kirinuki_core の親ディレクトリ（sys.path に足す場所）を返す。見つからなければNone。"""
    # realpath でシンボリックリンクを解決する（install.sh は skill を symlink で
    # 導入するため、symlink越しでも「../../」がリポジトリ直下を指すようにする）
    here = os.path.dirname(os.path.realpath(__file__))
    candidates = [
        os.path.normpath(os.path.join(here, "..", "..")),
    ]
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidates.append(project_dir)
    candidates.append(os.path.expanduser("~/.claude"))
    for parent in candidates:
        if os.path.isfile(os.path.join(parent, "kirinuki_core", "__init__.py")):
            return parent
    return None


def ensure_kirinuki_core_on_path():
    """kirinuki_core をsys.pathに足す。見つからなければ案内を出してSystemExit(1)。"""
    parent = find_kirinuki_core_parent()
    if parent is None:
        print("kirinuki_core が見つかりません。install.sh を実行してください。", file=sys.stderr)
        raise SystemExit(1)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return parent
