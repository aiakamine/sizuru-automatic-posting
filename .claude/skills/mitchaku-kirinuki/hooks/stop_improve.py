# -*- coding: utf-8 -*-
"""Stop フック：薄いラッパ。
共通実装（../../kirinuki_hooks）を探して、mitchaku-kirinuki 固有の設定を渡すだけ。
見つからない場合は「検査していない」ことを必ず知らせる（黙って素通ししない）。
"""
import json
import os
import sys


def _find_common_package_parent():
    """kirinuki_hooks の親ディレクトリ（sys.path に足す場所）を探す。
    探索順: ①スキルディレクトリの親（リポジトリ内配置） ②$CLAUDE_PROJECT_DIR ③~/.claude
    """
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
        if os.path.isfile(os.path.join(parent, "kirinuki_hooks", "stop.py")):
            return parent
    return None


def _warn_not_found():
    message = (
        "kirinuki_hooks が見つからないため、この呼び出しは検査していません。"
        "install.sh を実行するか、リポジトリ配下から起動してください。"
    )
    try:
        print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    except Exception:
        print(message, file=sys.stderr)
        sys.exit(1)


def build_config(stop):
    """mitchaku-kirinuki 固有の設定を組み立てる（stop モジュールは呼び出し側から渡す）。"""
    return stop.StopConfig(
        skill_name="mitchaku-kirinuki",
        required_improve_headings=("今回のFB", "改善提案", "ユーザー判断", "反映結果", "テスト結果"),
        require_artifact=True,
        require_reservation_question=True,
    )


def main():
    parent = _find_common_package_parent()
    if parent is None:
        _warn_not_found()
        return
    sys.path.insert(0, parent)
    from kirinuki_hooks import stop
    stop.main(build_config(stop))


if __name__ == "__main__":
    main()
