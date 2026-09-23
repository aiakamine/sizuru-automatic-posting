# -*- coding: utf-8 -*-
"""PreToolUse（Bash）フック：薄いラッパ。
共通実装（../../kirinuki_hooks）を探して、taidan-kirinuki 固有の設定を渡すだけ。
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
        if os.path.isfile(os.path.join(parent, "kirinuki_hooks", "gate.py")):
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


def build_config(gate):
    """taidan-kirinuki 固有の設定を組み立てる（gate モジュールは呼び出し側から渡す）。"""
    return gate.GateConfig(
        skill_name="taidan-kirinuki",
        required_hearing_headings=(
            "本編URL",
            "目的とCTA",
            "本数",
            "公開チャンネル",
            "レイアウト実測に使う公開済みショートURL",
            "検索需要枠の製品名",
            "ターゲット視聴者",
            "NG（話題・人物・表現）",
            "フック方針",
            "尺",
            "納品先",
            "個人情報マスクの確認",
        ),
        production_rules=(
            # render.py <src> <workdir> <clips> --cta <cta> [--out out] [--only slug ...]
            gate.ProductionRule(
                ("render*.py",), workdir_index=1, required_positionals=3,
                option_arities={"--cta": 1, "--out": 1, "--only": "*"},
            ),
            # gen_docs.py <workdir> <clips> <dest> [--out out]
            gate.ProductionRule(
                ("gen_docs*.py",), workdir_index=0, required_positionals=3,
                option_arities={"--out": 1},
            ),
            # banner.py は位置引数でworkdirを取れないため、現在地(cdの累積)を使う
            gate.ProductionRule(("banner*.py",), workdir_index=None),
        ),
    )


def main():
    parent = _find_common_package_parent()
    if parent is None:
        _warn_not_found()
        return
    sys.path.insert(0, parent)
    from kirinuki_hooks import gate
    gate.main(build_config(gate))


if __name__ == "__main__":
    main()
