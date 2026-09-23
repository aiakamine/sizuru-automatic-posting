# -*- coding: utf-8 -*-
"""scripts/scan_qc.py の単体テスト（I-09 + Codexレビュー指摘の修正）。
OCR読み揺れの正規化・欠落フレームの扱い・動画識別子の衝突回避・バッチOCRを検証する。
"""
import os
import sys
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import scan_qc as Q  # noqa: E402


class NormalizeOcrTextTest(unittest.TestCase):
    def test_replaces_san_and_fullwidth_equals_with_halfwidth_equals(self):
        # Arrange / Act / Assert: 「三」「＝」はいずれも半角「=」に統一される
        self.assertEqual(Q.normalize_ocr_text("364日間三0契約"), "364日間=0契約")
        self.assertEqual(Q.normalize_ocr_text("364日間＝0契約"), "364日間=0契約")

    def test_converts_fullwidth_alnum_to_halfwidth(self):
        # Arrange / Act
        result = Q.normalize_ocr_text("ＡＢＣａｂｃ０１２")
        # Assert
        self.assertEqual(result, "ABCabc012")

    def test_strips_whitespace(self):
        # Arrange / Act
        result = Q.normalize_ocr_text(" 364 日間 = 0 契約 ")
        # Assert
        self.assertEqual(result, "364日間=0契約")

    def test_leaves_normal_text_unchanged(self):
        # Arrange / Act / Assert
        self.assertEqual(Q.normalize_ocr_text("周りからは坊主呼ばわりされてた"),
                          "周りからは坊主呼ばわりされてた")


class MergeSegmentsTest(unittest.TestCase):
    def test_ocr_misreading_of_equals_sign_is_merged_into_one_segment(self):
        # Arrange: 実データで実際に起きた誤検出の再現。
        # 「364日間=0契約」と「364日間三0契約」はOCRの読み揺れで同一テロップのはず
        frames = {0: "364日間=0契約", 1: "364日間=0契約", 2: "364日間三0契約"}
        # Act
        segs, missing = Q.merge_segments(frames, expected_indices=[0, 1, 2])
        # Assert: 正規化後は同一なので1セグメントにまとまる
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][0], 0)
        self.assertEqual(segs[0][1], 2)
        self.assertEqual(missing, [])

    def test_truly_different_text_stays_a_separate_segment(self):
        # Arrange: 明らかに異なるテロップは別セグメントのまま
        frames = {0: "364日間=0契約", 1: "364日間=0契約", 2: "全く違う文章です"}
        # Act
        segs, missing = Q.merge_segments(frames, expected_indices=[0, 1, 2])
        # Assert
        self.assertEqual(len(segs), 2)
        self.assertEqual(missing, [])

    def test_empty_text_frames_merge_together(self):
        # Arrange: 無テロップ区間（空文字列）も1セグメントにまとまる
        frames = {0: "", 1: "", 2: ""}
        # Act
        segs, missing = Q.merge_segments(frames, expected_indices=[0, 1, 2])
        # Assert
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][2], "")
        self.assertEqual(missing, [])

    def test_missing_frame_is_reported_and_does_not_bridge_segments(self):
        # Arrange: フレーム1がOCR結果欠落（frames_for_videoに無い）。
        # 前後は同じテロップに見えても、欠落を挟んで連結してはいけない
        frames = {0: "364日間=0契約", 2: "364日間=0契約"}
        # Act
        segs, missing = Q.merge_segments(frames, expected_indices=[0, 1, 2])
        # Assert
        self.assertEqual(missing, [1])
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0], [0, 0, "364日間=0契約"])
        self.assertEqual(segs[1], [2, 2, "364日間=0契約"])

    def test_multiple_missing_frames_are_all_reported(self):
        # Arrange
        frames = {0: "テキスト"}
        # Act
        _segs, missing = Q.merge_segments(frames, expected_indices=[0, 1, 2, 3])
        # Assert
        self.assertEqual(missing, [1, 2, 3])


class VideoIdentifierTest(unittest.TestCase):
    def test_includes_index_to_avoid_collisions_across_directories(self):
        # Arrange: 異なるディレクトリの同名ファイルでも衝突しない
        a = Q.video_identifier(0, "outA/01_alpha.mp4")
        b = Q.video_identifier(1, "outB/01_alpha.mp4")
        # Act / Assert
        self.assertNotEqual(a, b)

    def test_does_not_include_extension_or_directory(self):
        # Arrange / Act
        result = Q.video_identifier(3, "out/01_A_364日間0契約.mp4")
        # Assert
        self.assertEqual(result, "03_01_A_364日間0契約")


class ParseScanfFilenameTest(unittest.TestCase):
    def test_splits_identifier_and_sequence_even_with_underscores_in_identifier(self):
        # Arrange: 識別子自体にアンダースコアが含まれていても末尾の連番だけを切り出す
        m = Q.SCANF_FILENAME_RE.match("00_01_A_364日間0契約_0007.png")
        # Act / Assert
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "00_01_A_364日間0契約")
        self.assertEqual(m.group(2), "0007")

    def test_matches_sequence_numbers_that_are_not_four_digits(self):
        # Arrange: 4桁固定ではなく1桁以上にする（長い動画で桁数が増える場合に備える）
        m = Q.SCANF_FILENAME_RE.match("00_clip_12345.png")
        # Act / Assert
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "12345")


class RunOcrBatchesTest(unittest.TestCase):
    def setUp(self):
        self._original_run = Q.subprocess.run

    def tearDown(self):
        Q.subprocess.run = self._original_run

    def test_splits_into_batches_and_merges_results(self):
        # Arrange
        calls = []

        class FakeResult:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            files = cmd[1:]
            return FakeResult("".join("%s\ttext-%d\n" % (f, i) for i, f in enumerate(files)))

        Q.subprocess.run = fake_run
        files = ["f%d.png" % i for i in range(5)]
        # Act
        result = Q.run_ocr_batches("./ocrcli", files, batch_size=2)
        # Assert: 2件ずつ3バッチに分かれる
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result), 5)

    def test_failed_batch_is_excluded_from_results(self):
        # Arrange
        class FakeResult:
            returncode = 1
            stdout = ""

        Q.subprocess.run = lambda cmd, **kwargs: FakeResult()
        # Act
        result = Q.run_ocr_batches("./ocrcli", ["a.png", "b.png"], batch_size=2)
        # Assert: 失敗したバッチのファイルは結果に含まれない（欠落として扱われる）
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
