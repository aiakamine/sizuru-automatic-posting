# -*- coding: utf-8 -*-
"""kirinuki_core.endings の単体テスト。
既存の分類（言い切り・引用止め・従来の継続形サフィックス）は
mitchaku-kirinuki/tests/test_cutpoints.py が import 経由で担保している。
ここでは対談の合成素材で「不明」に落ちてしまった継続形サフィックス
（「とかも」「とか」「けども」「けど」）の追加分だけを対象にする。
"""
import os
import sys
import unittest

CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(CORE_DIR))
from kirinuki_core import endings as E  # noqa: E402


class ClassifyEndingNewContinuingSuffixesTest(unittest.TestCase):
    def test_tokamo_is_continuing_form(self):
        # Arrange: 対談の合成素材で「不明」になっていた実例（2026-09-17）
        self.assertEqual(E.classify_ending("結果が出てるのに評価されてない人とかも"), "継続形")

    def test_toka_is_continuing_form(self):
        self.assertEqual(E.classify_ending("どうしても数字が出ない時とか"), "継続形")

    def test_kedomo_is_continuing_form(self):
        self.assertEqual(E.classify_ending("そう思うんですけども"), "継続形")

    def test_kedo_is_continuing_form(self):
        self.assertEqual(E.classify_ending("本当ですけど"), "継続形")

    def test_tte_is_still_quotation_style_not_continuing(self):
        # Arrange: 新しい継続形サフィックスの追加で「って」が誤って継続形に
        # 取られないことを確認する
        self.assertEqual(E.classify_ending("営業って"), "引用止め")

    def test_datte_is_still_quotation_style_not_continuing(self):
        self.assertEqual(E.classify_ending("そうだって"), "引用止め")


class ClassifyEndingTrailingWhitespaceTest(unittest.TestCase):
    """telops.txtの各行はload_telop_rows/load_telopsが.strip()した本文を渡す前提だが、
    classify_ending自体は末尾の空白を落とさない（body.endswith(suf)の厳密一致）。
    呼び出し側がstripし忘れた場合にどう分類されるか、現状の挙動を固定する。
    """

    def test_trailing_space_prevents_suffix_match_and_falls_back_to_unknown(self):
        # Arrange: 末尾に半角スペースが付くと「です」で終わっていないと判定される
        self.assertEqual(E.classify_ending("本当です "), "不明")

    def test_trailing_space_after_continuing_suffix_also_falls_back_to_unknown(self):
        self.assertEqual(E.classify_ending("そう思うんですけど "), "不明")

    def test_without_trailing_space_is_classified_normally(self):
        # Arrange: 比較対象。空白が無ければ従来どおり分類される
        self.assertEqual(E.classify_ending("本当です"), "言い切り")


if __name__ == "__main__":
    unittest.main()
