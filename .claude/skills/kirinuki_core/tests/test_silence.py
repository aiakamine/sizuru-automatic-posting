# -*- coding: utf-8 -*-
"""kirinuki_core.silence の単体テスト。
parse_silences・find_silence_near の既存動作は mitchaku-kirinuki/tests/test_cutpoints.py が
import経由で担保している。ここでは ensure_silence_file のメタデータ検証（キャッシュ無効化）と、
新設 find_silence_interior（align_check.pyのis_silent_at用）だけを対象にする。
"""
import json
import os
import sys
import tempfile
import unittest

CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(CORE_DIR))
from kirinuki_core import silence as S  # noqa: E402


class EnsureSilenceFileMetaTest(unittest.TestCase):
    """既存ファイルを無条件に再利用せず、隣の<path>.meta.jsonでsrc/mtime/noise_db/
    min_durationが一致するときだけ再利用する（Codexレビューで検出：異なるnoise_db・
    min_durationやsrcの更新後に古いsilence30.txtを使い回してしまっていた）。
    """

    def setUp(self):
        self._run = S.subprocess.run
        self.run_calls = []

        def fake_run(cmd, **kwargs):
            self.run_calls.append(cmd)

            class R:
                returncode = 0
                stderr = "silence_start: 1.0\nsilence_end: 1.5 | silence_duration: 0.5\n"
            return R()
        S.subprocess.run = fake_run

    def tearDown(self):
        S.subprocess.run = self._run

    def test_reuses_when_meta_matches(self):
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.mp4")
            open(src_path, "w").close()
            path = os.path.join(d, "silence30.txt")
            S.ensure_silence_file(path, src_path)
            self.assertEqual(len(self.run_calls), 1)
            # Act: 同じ条件で再度呼ぶ
            S.ensure_silence_file(path, src_path)
            # Assert: ffmpegを再実行しない
            self.assertEqual(len(self.run_calls), 1)

    def test_regenerates_when_noise_db_differs(self):
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.mp4")
            open(src_path, "w").close()
            path = os.path.join(d, "silence30.txt")
            S.ensure_silence_file(path, src_path, noise_db=-30)
            S.ensure_silence_file(path, src_path, noise_db=-26)
            self.assertEqual(len(self.run_calls), 2)

    def test_regenerates_when_min_duration_differs(self):
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.mp4")
            open(src_path, "w").close()
            path = os.path.join(d, "silence30.txt")
            S.ensure_silence_file(path, src_path, min_duration=0.08)
            S.ensure_silence_file(path, src_path, min_duration=0.10)
            self.assertEqual(len(self.run_calls), 2)

    def test_regenerates_when_source_file_is_modified(self):
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.mp4")
            open(src_path, "w").close()
            os.utime(src_path, (1000, 1000))
            path = os.path.join(d, "silence30.txt")
            S.ensure_silence_file(path, src_path)
            os.utime(src_path, (2000, 2000))
            S.ensure_silence_file(path, src_path)
            self.assertEqual(len(self.run_calls), 2)

    def test_regenerates_when_meta_file_is_missing(self):
        # Arrange: メタ無しの既存ファイル（第3段階以前に作られたもの）
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.mp4")
            open(src_path, "w").close()
            path = os.path.join(d, "silence30.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("silence_start: 5.0\nsilence_end: 5.5 | silence_duration: 0.5\n")
            S.ensure_silence_file(path, src_path)
            self.assertEqual(len(self.run_calls), 1)

    def test_corrupted_meta_json_is_treated_as_missing_and_regenerates(self):
        """meta.jsonの中身が壊れたJSON（例えば書き込み中の中断や手編集ミス）でも、
        他の無効条件（meta無し・src不一致・noise_db/min_duration不一致）と同様に
        「無効なら黙って再生成する」（Codexレビューで検出：以前はjson.load()が
        JSONDecodeErrorを送出しensure_silence_fileが例外で落ちていた）。
        """
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.mp4")
            open(src_path, "w").close()
            path = os.path.join(d, "silence30.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("silence_start: 5.0\nsilence_end: 5.5 | silence_duration: 0.5\n")
            with open(path + ".meta.json", "w", encoding="utf-8") as f:
                f.write("{not valid json")
            S.ensure_silence_file(path, src_path)
            self.assertEqual(len(self.run_calls), 1)

    def test_meta_json_that_is_not_a_dict_is_treated_as_missing(self):
        # Arrange: meta.jsonが壊れてはいないが、中身がdictでない（配列等）場合も
        # 無効な形式として再生成する
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.mp4")
            open(src_path, "w").close()
            path = os.path.join(d, "silence30.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("silence_start: 5.0\nsilence_end: 5.5 | silence_duration: 0.5\n")
            with open(path + ".meta.json", "w", encoding="utf-8") as f:
                json.dump([1, 2, 3], f)
            S.ensure_silence_file(path, src_path)
            self.assertEqual(len(self.run_calls), 1)

    def test_writes_meta_file_alongside_silence_file(self):
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.mp4")
            open(src_path, "w").close()
            path = os.path.join(d, "silence30.txt")
            S.ensure_silence_file(path, src_path, noise_db=-30, min_duration=0.08)
            meta_path = path + ".meta.json"
            self.assertTrue(os.path.exists(meta_path))
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            self.assertEqual(meta["src"], os.path.abspath(src_path))
            self.assertEqual(meta["noise_db"], -30)
            self.assertEqual(meta["min_duration"], 0.08)


class FindSilenceInteriorTest(unittest.TestCase):
    """align_check.pyのis_silent_at用：区間の内側（両端からmargin秒以上離れている）に
    完全に収まっているかを見る（find_silence_nearの「区間の外でも近ければ無音扱い」
    という許容とは逆に、端に近すぎる場合は無音と言い切らない）。
    """

    def test_point_well_inside_interval_is_interior(self):
        self.assertIsNotNone(S.find_silence_interior([(1.0, 2.0)], 1.5, margin=0.015))

    def test_point_within_margin_of_start_is_not_interior(self):
        self.assertIsNone(S.find_silence_interior([(1.0, 2.0)], 1.005, margin=0.015))

    def test_point_within_margin_of_end_is_not_interior(self):
        self.assertIsNone(S.find_silence_interior([(1.0, 2.0)], 1.995, margin=0.015))

    def test_point_just_beyond_margin_from_edges_is_interior(self):
        self.assertIsNotNone(S.find_silence_interior([(1.0, 2.0)], 1.02, margin=0.015))
        self.assertIsNotNone(S.find_silence_interior([(1.0, 2.0)], 1.98, margin=0.015))

    def test_point_outside_any_interval_is_none(self):
        self.assertIsNone(S.find_silence_interior([(1.0, 2.0)], 5.0))


if __name__ == "__main__":
    unittest.main()
