# -*- coding: utf-8 -*-
"""語尾分類・要注意判定まわりの共通ロジック（mitchaku-kirinuki/scripts/cutpoints.py から移設）。"""
from kirinuki_core.speech import TRAILING_GAP_WARN_SEC

# 継続形（先に判定する：他の語尾候補と末尾が重なるため優先度を上げる）
# 「とかも」「とか」「けども」「けど」は対談の合成素材で「不明」に落ちていた実例から追加
# （「結果が出てるのに評価されてない人とかも」。「とか」を足すと言い切りの「か」より先に
# マッチする。2026-09-17）
_CONTINUING_SUFFIXES = ("と思うんですよね", "と思うんですよ", "ので", "し",
                        "とかも", "とか", "けども", "けど")
# 引用止め・テ形止め（「〜って」「〜だって」「〜するぞ」等。話が続いている疑いがある終わり方）
_QUOTATION_SUFFIXES = ("するぞ", "だって", "って", "ぞ")
# 言い切り（文の終わりとして自然な終助詞・活用形）
_DECLARATIVE_SUFFIXES = (
    "でしょうか", "ますか", "ですか", "ください",
    "ました", "でした", "です", "ます",
    "だね", "だよ", "だ", "ね", "か", "？", "?", "た", "す", "よ",
)


def classify_ending(text):
    """語尾を「継続形」「引用止め」「言い切り」「不明」に分類する。
    「／」で複数行が繋がれたテロップは、最後の行（＝カット終点に最も近い発話）だけを見る。
    サフィックスの厳密な文法解析ではなく、SKILL.md絶対ルール4・過去の罠から作った
    ヒューリスティックであることに注意（最終判断は必ず文字起こしで人が確認する）。
    """
    body = text.split("／")[-1] if text else ""
    for suf in _CONTINUING_SUFFIXES:
        if body.endswith(suf):
            return "継続形"
    for suf in _QUOTATION_SUFFIXES:
        if body.endswith(suf):
            return "引用止め"
    for suf in _DECLARATIVE_SUFFIXES:
        if body.endswith(suf):
            return "言い切り"
    return "不明" if body else "不明（無テロップ）"


def caution_reasons(ending_class, silence_hit, gap):
    """要注意にする理由のリストを返す（空なら要注意ではない）。"""
    reasons = []
    if ending_class not in ("言い切り",):
        reasons.append("語尾が%sで終えている" % ending_class)
    if silence_hit is None:
        reasons.append("終点付近に無音の谷が無い")
    if gap is not None and gap < TRAILING_GAP_WARN_SEC:
        reasons.append("終点後%.2fsで次の発話が始まっている" % gap)
    return reasons
