# -*- coding: utf-8 -*-
"""tests/regression/run.py の純粋関数（ffmpeg等の実行を伴わないロジック）の単体テスト。
run.py 本体（python3 tests/regression/run.py）はffmpegの実行を伴い重いため
`unittest discover` には含めていないが、ここに切り出した純粋関数はコストゼロで
検査できるので、Codexレビュー指摘の回帰防止として通常のテストに含める。
使い方: python3 -m unittest tests.regression.test_pure -v
"""
import os
import sys
import tempfile
import unittest

REGRESSION_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(REGRESSION_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "mitchaku-kirinuki", "scripts"))
sys.path.insert(0, REGRESSION_DIR)
import run as RUN  # noqa: E402


class JudgeSourceCorrespondenceTest(unittest.TestCase):
    def test_ok_when_clearly_closest_to_expected(self):
        # Arrange: 対応フレームとの差が小さく、両隣とは明確に離れている
        # Act
        ok, reason = RUN.judge_source_correspondence(0.25, [28.7, 30.1])
        # Assert
        self.assertTrue(ok)
        self.assertIn("OK", reason)

    def test_ng_when_extraction_failed(self):
        # Act / Assert
        ok, _ = RUN.judge_source_correspondence(None, [1.0, 2.0])
        self.assertFalse(ok)

    def test_ng_when_out_diff_exceeds_max(self):
        # Act / Assert
        ok, _ = RUN.judge_source_correspondence(20.0, [25.0, 26.0])
        self.assertFalse(ok)

    def test_ng_when_a_neighbor_is_clearly_closer_than_expected_frame(self):
        # Arrange: Codexレビューで指摘された実例。out_diff=8だが隣(0)のほうが
        # 明確に近く、対応フレームがそもそも間違っている(頭尻ずれ)と判断すべき。
        # Act
        ok, reason = RUN.judge_source_correspondence(8.0, [0.0, 15.0])
        # Assert
        self.assertFalse(ok)
        self.assertIn("隣接フレームのほうが", reason)

    def test_ng_when_next_side_neighbor_is_closer(self):
        # Arrange: 逆方向(src+1側)が近い場合も検出できる（片側しか見ないと検出できない）
        # Act
        ok, reason = RUN.judge_source_correspondence(8.0, [15.0, 0.0])
        # Assert
        self.assertFalse(ok)

    def test_warns_when_static_scene_makes_it_indistinguishable(self):
        # Arrange: 対応フレームも隣接フレームもほぼ同じ絵（静止に近い場面）
        # Act
        ok, reason = RUN.judge_source_correspondence(0.42, [0.61, 0.90])
        # Assert
        self.assertIsNone(ok)
        self.assertIn("警告", reason)

    def test_warns_when_no_neighbor_comparable(self):
        # Arrange: 両隣とも抽出失敗（クリップ端など）
        # Act
        ok, reason = RUN.judge_source_correspondence(0.3, [None, None])
        # Assert
        self.assertIsNone(ok)

    def test_ignores_neighbor_that_could_not_be_extracted(self):
        # Arrange: 片方だけ抽出できた場合はそちらで判定する
        # Act
        ok, _ = RUN.judge_source_correspondence(0.25, [None, 28.7])
        # Assert
        self.assertTrue(ok)


class MeanAbsDiffTest(unittest.TestCase):
    def test_returns_none_for_mismatched_sizes(self):
        self.assertIsNone(RUN.mean_abs_diff(b"abc", b"ab"))

    def test_returns_none_for_empty_input(self):
        self.assertIsNone(RUN.mean_abs_diff(b"", b"abc"))

    def test_computes_average_absolute_difference(self):
        self.assertAlmostEqual(RUN.mean_abs_diff(bytes([0, 10]), bytes([5, 5])), 5.0)


class FindDuplicateIndicesTest(unittest.TestCase):
    def test_returns_empty_set_when_no_duplicates(self):
        self.assertEqual(RUN._find_duplicate_indices([{"index": 0}, {"index": 1}]), set())

    def test_finds_duplicate_indices(self):
        cuts = [{"index": 0}, {"index": 1}, {"index": 0}]
        self.assertEqual(RUN._find_duplicate_indices(cuts), {0})


class CompareTest(unittest.TestCase):
    BASE_EXPECTED = {
        "av_duration_diff_ms_max": 33.4,
        "lufs_target": -14.0,
        "lufs_tolerance": 0.5,
        "dup_frame_max": 3,
        "dup_frame_baseline": 1,
        "cuts": [
            {"index": 0, "n_frames": 212, "head_hash": "h0", "tail_hash": "t0"},
            {"index": 1, "n_frames": 383, "head_hash": "h1", "tail_hash": "t1"},
        ],
    }
    BASE_ACTUAL = {
        "av_duration_diff_ms": 0.0,
        "lufs": -14.0,
        "dup_frame_count_output": 0,
        "dup_frame_baseline": 1,
        "cuts": [
            {"index": 0, "n_frames": 212, "head_hash": "h0", "tail_hash": "t0"},
            {"index": 1, "n_frames": 383, "head_hash": "h1", "tail_hash": "t1"},
        ],
    }

    def test_passes_when_everything_matches(self):
        ok, problems = RUN.compare(dict(self.BASE_ACTUAL), dict(self.BASE_EXPECTED))
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_reports_cut_present_only_in_expected(self):
        # Arrange: expectedにカット2があるがactualには無い
        # （以前はactualのcutsしか回さず、この欠落を報告できていなかった）
        expected = dict(self.BASE_EXPECTED)
        expected["cuts"] = expected["cuts"] + [
            {"index": 2, "n_frames": 100, "head_hash": "h2", "tail_hash": "t2"}]
        # Act
        ok, problems = RUN.compare(dict(self.BASE_ACTUAL), expected)
        # Assert
        self.assertFalse(ok)
        self.assertTrue(any("実際の出力に存在しません" in p for p in problems))

    def test_reports_cut_present_only_in_actual(self):
        actual = dict(self.BASE_ACTUAL)
        actual["cuts"] = actual["cuts"] + [
            {"index": 2, "n_frames": 100, "head_hash": "h2", "tail_hash": "t2"}]
        ok, problems = RUN.compare(actual, dict(self.BASE_EXPECTED))
        self.assertFalse(ok)
        self.assertTrue(any("期待値に存在しません" in p for p in problems))

    def test_rejects_duplicate_index_in_actual(self):
        actual = dict(self.BASE_ACTUAL)
        actual["cuts"] = actual["cuts"] + [
            {"index": 0, "n_frames": 212, "head_hash": "h0", "tail_hash": "t0"}]
        ok, problems = RUN.compare(actual, dict(self.BASE_EXPECTED))
        self.assertFalse(ok)
        self.assertTrue(any("インデックス重複" in p for p in problems))


class CheckQcProcessOkTest(unittest.TestCase):
    def test_ok_when_returncode_zero(self):
        ok, reason = RUN.check_qc_process_ok(ng=0, rc=0)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_ok_when_ng_lines_explain_nonzero_returncode(self):
        # rc=1だが実際にNG行がある（正常な不合格判定）場合は「実行自体は失敗していない」
        ok, reason = RUN.check_qc_process_ok(ng=2, rc=1)
        self.assertTrue(ok)

    def test_ng_when_returncode_nonzero_without_any_ng_line(self):
        # Arrange: TimelineMissingError等の内部エラーはNG行を出さずrc=1を返す
        # Act
        ok, reason = RUN.check_qc_process_ok(ng=0, rc=1)
        # Assert
        self.assertFalse(ok)
        self.assertIsNotNone(reason)


class RequireFrameCountTest(unittest.TestCase):
    def test_passes_when_enough_frames(self):
        RUN.require_frame_count([1, 2, 3], 3, "context")  # 例外が出なければOK

    def test_raises_when_not_enough_frames(self):
        with self.assertRaises(RuntimeError):
            RUN.require_frame_count([1, 2], 3, "context")


class ResolveOcrcliPathTest(unittest.TestCase):
    def test_returns_none_when_not_specified(self):
        path, error = RUN.resolve_ocrcli_path(None)
        self.assertIsNone(path)
        self.assertIsNone(error)

    def test_absolutizes_existing_path(self):
        with tempfile.NamedTemporaryFile() as f:
            path, error = RUN.resolve_ocrcli_path(f.name)
            self.assertEqual(path, os.path.abspath(f.name))
            self.assertIsNone(error)

    def test_errors_when_explicitly_specified_path_does_not_exist(self):
        # Arrange: 以前は存在しないパスを指定すると黙ってスキップしていた
        # Act
        path, error = RUN.resolve_ocrcli_path("/no/such/ocrcli/binary")
        # Assert
        self.assertIsNone(path)
        self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
