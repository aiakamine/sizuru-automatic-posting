# -*- coding: utf-8 -*-
"""scripts/qc_cuts.py の単体テスト（I-08 / 第1段階1-C）。
render.py が書き出す timeline.json（フレーム番号ベースの座標系）の読み取り、
検査点の組み立て、期待テロップの引き当て、正規化と判定を検証する。
ffmpeg を実際に呼ぶ統合テストは含まない（実データでの動作確認は手動で行い、
結果を別途報告する）。
"""
import json
import os
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import qc_cuts as Q  # noqa: E402


def find_point(points, cut, label):
    for p in points:
        if p["cut"] == cut and p["label"] == label:
            return p
    raise AssertionError("point not found: cut=%r label=%r (points=%r)" % (cut, label, points))


def make_timeline(cuts_info, flash_after=None):
    return {"cuts": cuts_info, "flash_after": flash_after}


class LoadTimelineTest(unittest.TestCase):
    def test_raises_timeline_missing_error_when_file_absent(self):
        # Arrange
        with tempfile.TemporaryDirectory() as d:
            # Act / Assert
            with self.assertRaises(Q.TimelineMissingError):
                Q.load_timeline(d, "a.mp4")

    def test_reads_json_written_by_render(self):
        # Arrange
        data = {"cuts": [], "flash_after": None, "total_frames": 0}
        with tempfile.TemporaryDirectory() as d:
            path = Q.timeline_path(d, "a.mp4")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            # Act
            loaded = Q.load_timeline(d, "a.mp4")
        # Assert
        self.assertEqual(loaded, data)


class BuildCheckpointsTest(unittest.TestCase):
    def setUp(self):
        # 3カット。flash_after=0（先頭カット→2番目のカットがフラッシュ）
        self.cuts_info = [
            {"index": 0, "src_start_frame": 1000, "src_end_frame": 1180, "n_frames": 180, "out_start_frame": 0},
            {"index": 1, "src_start_frame": 2000, "src_end_frame": 2300, "n_frames": 300, "out_start_frame": 170},
            {"index": 2, "src_start_frame": 3000, "src_end_frame": 3300, "n_frames": 300, "out_start_frame": 470},
        ]
        self.timeline = make_timeline(self.cuts_info, flash_after=0)
        self.points = Q.build_checkpoints(self.timeline)

    def test_head_and_head2_use_out_start_frame_and_src_start_frame(self):
        # Arrange / Act: カット2(フラッシュに隣接しない)の頭
        head = find_point(self.points, 2, "head")
        head2 = find_point(self.points, 2, "head2")
        # Assert
        self.assertEqual(head["out_frame"], 470)
        self.assertEqual(head["src_time"], 3000 / Q.FPSF)
        self.assertEqual(head2["out_frame"], 471)
        self.assertEqual(head2["src_time"], 3001 / Q.FPSF)

    def test_tail_and_tail2_use_last_frames_of_the_cut(self):
        # Arrange / Act: カット2の尻
        tail = find_point(self.points, 2, "tail")
        tail2 = find_point(self.points, 2, "tail2")
        # Assert: n_frames=300 → 最終フレームは out_start_frame+299
        self.assertEqual(tail["out_frame"], 470 + 299)
        self.assertEqual(tail["src_time"], (3000 + 299) / Q.FPSF)
        self.assertEqual(tail2["out_frame"], 470 + 298)

    def test_flash_boundary_excludes_tail_of_leading_cut_and_head_of_next_cut(self):
        # Arrange / Act / Assert: カット0の尻とカット1の頭はフラッシュ区間なので検査対象外
        with self.assertRaises(AssertionError):
            find_point(self.points, 0, "tail")
        with self.assertRaises(AssertionError):
            find_point(self.points, 0, "tail2")
        with self.assertRaises(AssertionError):
            find_point(self.points, 1, "head")
        with self.assertRaises(AssertionError):
            find_point(self.points, 1, "head2")
        # カット0の頭とカット1の尻は検査対象のまま
        find_point(self.points, 0, "head")
        find_point(self.points, 1, "tail")

    def test_no_flash_checks_every_cut_head_and_tail(self):
        # Arrange
        timeline = make_timeline(self.cuts_info, flash_after=None)
        # Act
        points = Q.build_checkpoints(timeline)
        # Assert: 3カット×4点、除外なし
        self.assertEqual(len(points), 12)

    def test_single_frame_cut_skips_second_checkpoint_without_negative_offset(self):
        # Arrange: n_frames=1のカットは head2/tail2 を作らない（負のオフセットを避ける）
        cuts_info = [{"index": 0, "src_start_frame": 100, "src_end_frame": 101,
                      "n_frames": 1, "out_start_frame": 0}]
        timeline = make_timeline(cuts_info, flash_after=None)
        # Act
        points = Q.build_checkpoints(timeline)
        # Assert
        labels = sorted(p["label"] for p in points)
        self.assertEqual(labels, ["head", "tail"])


class ExtractFrameCmdTest(unittest.TestCase):
    def test_selects_frame_by_number_directly(self):
        # Arrange / Act
        cmd = Q.extract_frame_cmd("out/a.mp4", 470, "tmp.png")
        # Assert
        self.assertIn("-i", cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], "out/a.mp4")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("select=eq(n\\,470)", vf)
        self.assertIn("crop=%d:%d:%d:%d" % (Q.BAND_W, Q.BAND_H, Q.BAND_X, Q.BAND_Y), vf)
        self.assertIn("-frames:v", cmd)
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "1")


class LoadTelopsTest(unittest.TestCase):
    def test_parses_fixed_width_lines_including_empty_text(self):
        # Arrange: subscan.py が実際に書き出す書式（先頭空白パディング・空テロップ行を含む）
        content = (
            " 630.563  631.798 f 18898- 18935 王然\n"
            " 634.501  637.303 f 19016- 19100 364日間=0契約\n"
            " 637.303  638.905 f 19100- 19148 364日間三0契約\n"
            " 638.905  639.806 f 19148- 19175 \n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = f.name
        try:
            # Act
            rows = Q.load_telops(path)
            # Assert
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0], (630.563, 631.798, "王然"))
            self.assertEqual(rows[1], (634.501, 637.303, "364日間=0契約"))
            self.assertEqual(rows[3], (638.905, 639.806, ""))
        finally:
            os.unlink(path)

    def test_expected_telop_at_returns_text_for_matching_time(self):
        # Arrange
        rows = [(630.563, 631.798, "王然"), (634.501, 637.303, "364日間=0契約")]
        # Act / Assert
        self.assertEqual(Q.expected_telop_at(rows, 635.0), "364日間=0契約")

    def test_expected_telop_at_returns_empty_when_no_row_matches(self):
        # Arrange
        rows = [(630.563, 631.798, "王然")]
        # Act / Assert
        self.assertEqual(Q.expected_telop_at(rows, 700.0), "")

    def test_expected_telop_at_boundary_time_belongs_to_next_row(self):
        # Arrange: 前の行の end と次の行の start が同じ時刻（subscan の行は隙間なく連続する）
        rows = [(634.501, 637.303, "364日間=0契約"), (637.303, 638.905, "次のテロップ")]
        # Act / Assert: 境界ちょうどは次の行（半開区間 start <= t < end）
        self.assertEqual(Q.expected_telop_at(rows, 637.303), "次のテロップ")


class LoadTelopFramesTest(unittest.TestCase):
    """2026-10-13 凪さん密着：カット頭（新しいテロップの最初のフレーム）で、閉区間検索が
    直前の行を期待値にしてしまい、毎回8〜11件の誤NGが出た。フレーム番号で引き直す。"""

    def _write(self, content):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_parses_frame_numbers_including_empty_text(self):
        path = self._write(
            " 794.193  794.394 f 23802- 23808 大阪に出る時の引っ越し代・家賃等\n"
            " 794.394  797.297 f 23808- 23895 そこを父親代わりにやってる自分がいて\n"
            " 797.297  800.099 f 23895- 23979 \n"
        )
        rows = Q.load_telop_frames(path)
        self.assertEqual(rows[0], (23802, 23808, "大阪に出る時の引っ越し代・家賃等"))
        self.assertEqual(rows[2], (23895, 23979, ""))

    def test_boundary_frame_belongs_to_next_row(self):
        rows = [(23802, 23808, "大阪に出る時の引っ越し代・家賃等"),
                (23808, 23895, "そこを父親代わりにやってる自分がいて")]
        # カット頭＝新しいテロップの最初のフレーム（23808）は次の行を期待する
        self.assertEqual(Q.expected_telop_at_frame(rows, 23808), "そこを父親代わりにやってる自分がいて")
        # 直前のフレームは前の行
        self.assertEqual(Q.expected_telop_at_frame(rows, 23807), "大阪に出る時の引っ越し代・家賃等")

    def test_frame_in_gap_between_rows_expects_no_telop(self):
        # subscan の行が途切れている区間（テロップ無し）は空文字列
        rows = [(14645, 14730, "看板"), (14976, 15383, "仕事中にサウナ行く")]
        self.assertEqual(Q.expected_telop_at_frame(rows, 14800), "")

    def test_checkpoint_carries_source_frame_number(self):
        timeline = make_timeline([
            {"index": 0, "src_start_frame": 23808, "out_start_frame": 0, "n_frames": 87},
        ])
        points = Q.build_checkpoints(timeline)
        self.assertEqual(find_point(points, 0, "head")["src_frame"], 23808)
        self.assertEqual(find_point(points, 0, "tail")["src_frame"], 23808 + 86)


class NormalizeTextTest(unittest.TestCase):
    def test_normalizes_confusable_symbols_and_fullwidth_and_whitespace(self):
        # Arrange / Act / Assert
        self.assertEqual(Q.normalize_text("364日間三0契約"), "364日間=0契約")
        self.assertEqual(Q.normalize_text(" ＡＢ 12 "), "AB12")


class JudgeTest(unittest.TestCase):
    def test_empty_expected_and_empty_actual_is_ok(self):
        ok, _ = Q.judge("", "")
        self.assertTrue(ok)

    def test_expected_present_but_actual_empty_is_ng(self):
        ok, _ = Q.judge("364日間=0契約", "")
        self.assertFalse(ok)

    def test_ocr_misreading_variant_is_ok(self):
        # Arrange / Act: 「三」と「＝」の読み揺れは正規化後に一致する
        ok, _ = Q.judge("364日間=0契約", "364日間三0契約")
        # Assert
        self.assertTrue(ok)

    def test_actual_longer_and_starting_with_expected_is_ok(self):
        # 実際のOCRが期待より長い（前後の要素が少し混ざる）場合は前方一致でOK
        ok, _ = Q.judge("周りからは", "周りからは坊主呼ばわりされてたんで")
        self.assertTrue(ok)

    def test_actual_truncated_but_at_least_half_length_is_ok(self):
        # Arrange: 実際が期待の前方一致かつ50%以上の長さ（クロップで後半が少し欠けた程度）
        ok, _ = Q.judge("364日間0契約から評価される", "364日間0契約から")
        # Assert
        self.assertTrue(ok)

    def test_actual_too_short_prefix_is_ng(self):
        # Arrange: 「Hello world」に対して「H」のような極端に短い前方一致はNG扱いにする
        ok, _ = Q.judge("Hello world", "H")
        # Assert
        self.assertFalse(ok)

    def test_different_telop_is_ng(self):
        ok, _ = Q.judge("364日間=0契約", "全く違うテロップです")
        self.assertFalse(ok)

    def test_expected_empty_but_actual_has_text_is_ng(self):
        ok, _ = Q.judge("", "映り込んでいるテロップ")
        self.assertFalse(ok)


class JudgePointTest(unittest.TestCase):
    """OCRプロセス失敗・結果欠落・抽出失敗を「空テロップ」として扱わないことを確認する。"""

    def test_missing_frame_extraction_is_ng(self):
        # Arrange: png=None（フレームの抽出に失敗）。期待が空でもNGにする
        ok, reason = Q.judge_point(expected="", png=None, ocr={}, ocr_failed=False)
        # Assert
        self.assertFalse(ok)
        self.assertIn("抽出", reason)

    def test_ocr_process_failure_is_ng_even_when_expected_is_empty(self):
        # Arrange: OCRプロセス自体が失敗。期待テロップが空でも「空テロップとしてOK」にしない
        ok, reason = Q.judge_point(expected="", png="frame.png", ocr={}, ocr_failed=True)
        # Assert
        self.assertFalse(ok)
        self.assertIn("OCR", reason)

    def test_missing_ocr_result_is_ng_even_when_expected_is_empty(self):
        # Arrange: OCRプロセスは成功したが、この画像の結果だけ辞書に無い（欠落）
        ok, reason = Q.judge_point(expected="", png="frame.png", ocr={"other.png": ""}, ocr_failed=False)
        # Assert
        self.assertFalse(ok)
        self.assertIn("欠落", reason)

    def test_normal_case_delegates_to_judge(self):
        # Arrange: 正常系はjudge()の結果をそのまま使う
        ok, reason = Q.judge_point(expected="364日間=0契約", png="frame.png",
                                    ocr={"frame.png": "364日間三0契約"}, ocr_failed=False)
        # Assert
        self.assertTrue(ok)


class RunOcrTest(unittest.TestCase):
    def setUp(self):
        self._original_run = Q.subprocess.run

    def tearDown(self):
        Q.subprocess.run = self._original_run

    def test_no_paths_returns_empty_and_not_failed(self):
        ocr, failed = Q.run_ocr("./ocrcli", [])
        self.assertEqual(ocr, {})
        self.assertFalse(failed)

    def test_parses_successful_output(self):
        class FakeResult:
            returncode = 0
            stdout = "a.png\tHello\nb.png\tWorld\n"

        Q.subprocess.run = lambda *a, **k: FakeResult()
        ocr, failed = Q.run_ocr("./ocrcli", ["a.png", "b.png"])
        self.assertEqual(ocr, {"a.png": "Hello", "b.png": "World"})
        self.assertFalse(failed)

    def test_nonzero_returncode_is_reported_as_failed(self):
        class FakeResult:
            returncode = 1
            stdout = ""

        Q.subprocess.run = lambda *a, **k: FakeResult()
        ocr, failed = Q.run_ocr("./ocrcli", ["a.png"])
        self.assertEqual(ocr, {})
        self.assertTrue(failed)


class ClipIdentifierTest(unittest.TestCase):
    def test_strips_directory_components(self):
        # Arrange / Act
        ident = Q.clip_identifier(0, "some/dir/01_A.mp4")
        # Assert: ディレクトリ成分（/）を含まない
        self.assertNotIn("/", ident)

    def test_includes_index_to_avoid_collisions(self):
        # Arrange: 同じbasenameでもindexが違えば衝突しない
        a = Q.clip_identifier(0, "dirA/01_A.mp4")
        b = Q.clip_identifier(1, "dirB/01_A.mp4")
        # Act / Assert
        self.assertNotEqual(a, b)


class CheckClipTest(unittest.TestCase):
    def test_raises_timeline_missing_error_when_render_not_run_yet(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp_dir:
            class Args:
                out = tmp_dir
                ocrcli = "./ocrcli"
            clip = {"file": "a.mp4", "cuts": [(10.0, 16.0)]}
            # Act / Assert
            with self.assertRaises(Q.TimelineMissingError):
                Q.check_clip(0, clip, Args(), [], tmp_dir)


if __name__ == "__main__":
    unittest.main()
