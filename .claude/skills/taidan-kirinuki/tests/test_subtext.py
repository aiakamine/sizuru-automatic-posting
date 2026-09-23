# -*- coding: utf-8 -*-
"""scripts/subtext.py の単体テスト（第3段階：フェードイン対策・空行率/帯ずれ警告・
xargs依存の並列実行のconcurrent.futuresへの置き換え）。
ffmpeg/OCRを実際に呼ぶ統合テストは含まない。
"""
import json
import os
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import subtext as ST  # noqa: E402


class BuildSegmentsTest(unittest.TestCase):
    def test_drops_segments_shorter_than_min_duration(self):
        # fps=30: bound 0-5 は5/30=0.167s(短すぎ)、5-30は25/30=0.83s(採用)
        segs = ST.build_segments([0, 5, 30], fps=30.0, t0=0.0, t1=1e9, min_duration=0.4)
        self.assertEqual(segs, [(5, 30)])

    def test_filters_by_time_window(self):
        segs = ST.build_segments([0, 60, 120, 300], fps=30.0, t0=3.0, t1=5.0)
        # 開始時刻が範囲に入るのは120/30=4.0sの区間だけ(60/30=2.0sは範囲外)
        self.assertEqual(segs, [(120, 300)])

    def test_no_boundaries_returns_empty_list(self):
        self.assertEqual(ST.build_segments([], fps=30.0, t0=0.0, t1=1e9), [])

    def test_appends_final_segment_up_to_total_frames_when_given(self):
        # Arrange: bound=[30,161,261]、動画は381フレームまである（team-leadの合成素材の実例）。
        # 261フレーム目以降、動画の終わりまでのテロップがbuild_segmentsに含まれていなかった
        segs = ST.build_segments([30, 161, 261], fps=30000.0 / 1001, t0=0.0, t1=1e9,
                                  total_frames=381)
        self.assertEqual(segs, [(0, 30), (30, 161), (161, 261), (261, 381)])

    def test_appends_leading_segment_before_first_boundary_when_total_frames_given(self):
        # Arrange: 最初の境界(30)より前の区間(0,30)もbuild_segmentsに含まれていなかった
        segs = ST.build_segments([30, 161], fps=30000.0 / 1001, t0=0.0, t1=1e9, total_frames=261)
        self.assertEqual(segs, [(0, 30), (30, 161), (161, 261)])

    def test_leading_segment_respects_min_duration(self):
        # Arrange: 最初の境界が4フレーム目(=4/30=0.13s、短すぎ)なら先頭区間は作らない
        segs = ST.build_segments([4, 161], fps=30.0, t0=0.0, t1=1e9, total_frames=261,
                                  min_duration=0.4)
        self.assertEqual(segs, [(4, 161), (161, 261)])

    def test_single_segment_when_bound_is_empty_but_total_frames_given(self):
        # Arrange: 境界が1件も検出されない（テロップが全編通して同じ）場合、
        # (0, total_frames)を1区間にする（先頭と末尾を別々に足して2区間にしない）
        segs = ST.build_segments([], fps=30.0, t0=0.0, t1=1e9, total_frames=100)
        self.assertEqual(segs, [(0, 100)])

    def test_does_not_append_final_segment_when_total_frames_is_none(self):
        # Arrange: framesキーが無い古いbounds.json（後方互換）。従来どおり最後の境界までで打ち切る
        segs = ST.build_segments([30, 161, 261], fps=30.0, t0=0.0, t1=1e9, total_frames=None)
        self.assertEqual(segs, [(30, 161), (161, 261)])

    def test_final_segment_still_respects_min_duration(self):
        # Arrange: 最後の境界(261)からtotal_frames(265)までは4/30=0.13s(短すぎ)
        segs = ST.build_segments([30, 161, 261], fps=30.0, t0=0.0, t1=1e9,
                                  total_frames=265, min_duration=0.4)
        self.assertEqual(segs, [(0, 30), (30, 161), (161, 261)])

    def test_final_segment_respects_time_window(self):
        # Arrange: 最後の境界(261)の開始時刻(261/30=8.7s)がt1=5.0の範囲外
        segs = ST.build_segments([30, 161, 261], fps=30.0, t0=0.0, t1=5.0, total_frames=381)
        self.assertEqual(segs, [(0, 30), (30, 161)])

    def test_total_frames_equal_to_last_boundary_adds_no_final_segment(self):
        # Arrange: 動画がちょうど最後の境界で終わっている（余りのフレームが無い）
        segs = ST.build_segments([30, 161, 261], fps=30.0, t0=0.0, t1=1e9, total_frames=261)
        self.assertEqual(segs, [(0, 30), (30, 161), (161, 261)])

    def test_first_boundary_at_zero_adds_no_degenerate_leading_segment(self):
        # Arrange: 最初の境界がちょうど0フレーム目（動画の先頭からテロップが切り替わる）。
        # 先頭区間(0, bound[0])=(0, 0)は0フレーム幅なのでmin_duration未満として弾かれ、
        # 二重に(0,...)の区間が作られない
        segs = ST.build_segments([0, 100], fps=30.0, t0=0.0, t1=1e9, total_frames=200)
        self.assertEqual(segs, [(0, 100), (100, 200)])



class BandShotFilenameTest(unittest.TestCase):
    """区間の通し番号(s%04d)だけのファイル名だと、--fromを変えて再実行したとき
    別の時刻の画像が古いキャッシュとして使われてしまう（team-leadが確認したバグ）。
    ファイル名に中央フレーム番号と帯位置(y,h)を組み込むことで、時刻や帯位置が違えば
    別のキャッシュファイルになるようにする。
    """

    def test_different_frames_produce_different_filenames(self):
        a = ST.band_shot_filename("s", 100, 900, 170)
        b = ST.band_shot_filename("s", 200, 900, 170)
        self.assertNotEqual(a, b)

    def test_different_band_positions_produce_different_filenames(self):
        a = ST.band_shot_filename("s", 100, 900, 170)
        b = ST.band_shot_filename("s", 100, 860, 210)
        self.assertNotEqual(a, b)

    def test_same_frame_and_band_position_produce_the_same_filename(self):
        a = ST.band_shot_filename("s", 100, 900, 170)
        b = ST.band_shot_filename("s", 100, 900, 170)
        self.assertEqual(a, b)

    def test_prefix_distinguishes_mid_frame_shots_from_probe_shots(self):
        a = ST.band_shot_filename("s", 100, 900, 170)
        b = ST.band_shot_filename("probe", 100, 900, 170)
        self.assertNotEqual(a, b)


class RunExtractionsParallelTest(unittest.TestCase):
    def setUp(self):
        self._extract = ST.extract_band_png

    def tearDown(self):
        ST.extract_band_png = self._extract

    def test_runs_every_job(self):
        calls = []

        def fake_extract(src, t, band_y, band_h, out_png):
            calls.append(out_png)
        ST.extract_band_png = fake_extract
        jobs = [("src.mp4", float(i), 900, 170, "out_%d.png" % i) for i in range(12)]
        ST.run_extractions_parallel(jobs, max_workers=4)
        self.assertEqual(sorted(calls), sorted(j[4] for j in jobs))

    def test_works_with_very_long_output_paths(self):
        # Arrange: xargs -I{} は置換後255バイトの制限で壊れていた実例の再現。
        # concurrent.futuresはシェルを経由しないため、パス長に依らず動く
        long_dir = "a" * 500
        calls = []

        def fake_extract(src, t, band_y, band_h, out_png):
            calls.append(out_png)
        ST.extract_band_png = fake_extract
        jobs = [("src.mp4", 0.0, 900, 170, os.path.join(long_dir, "s0000.png"))]
        ST.run_extractions_parallel(jobs, max_workers=4)
        self.assertEqual(calls, [jobs[0][4]])

    def test_propagates_exception_from_a_failing_job(self):
        def fake_extract(src, t, band_y, band_h, out_png):
            if out_png == "bad.png":
                raise RuntimeError("ffmpeg failed")
        ST.extract_band_png = fake_extract
        jobs = [("src.mp4", 0.0, 900, 170, "ok.png"), ("src.mp4", 1.0, 900, 170, "bad.png")]
        with self.assertRaises(RuntimeError):
            ST.run_extractions_parallel(jobs, max_workers=2)

    def test_empty_job_list_does_nothing(self):
        ST.run_extractions_parallel([], max_workers=4)  # 例外が出なければOK


class MakeOcrTextAtTest(unittest.TestCase):
    def setUp(self):
        self._extract = ST.extract_band_png
        self._run_ocr = ST.run_ocr

    def tearDown(self):
        ST.extract_band_png = self._extract
        ST.run_ocr = self._run_ocr

    def test_extracts_at_frame_boundary_time_not_frame_seek_time(self):
        # Arrange: kirinuki_core.video.frame_seek_time（フレーム中央=(frame+0.5)/fps）は
        # 使わない。実測（実素材のffmpeg -ss input seek）でframe+0.5秒のシークが
        # 要求フレームの次のフレームを返すことがあったため（Codexレビューで検出）
        extracted = []
        ST.extract_band_png = lambda src, t, y, h, out: extracted.append((t, out))
        ST.run_ocr = lambda ocrcli, paths: ({paths[0]: "本文"}, False)
        ocr_text_at = ST.make_ocr_text_at("src.mp4", "./ocrcli", 900, 170, 30.0, "/tmp/probe")
        ocr_text_at(100)
        self.assertAlmostEqual(extracted[0][0], 100 / 30.0)

    def test_extracts_frame_and_returns_ocr_text(self):
        extracted = []
        ST.extract_band_png = lambda src, t, y, h, out: extracted.append((t, out))
        ST.run_ocr = lambda ocrcli, paths: ({paths[0]: "本文"}, False)
        ocr_text_at = ST.make_ocr_text_at("src.mp4", "./ocrcli", 900, 170, 30.0, "/tmp/probe")
        text = ocr_text_at(100)
        self.assertEqual(text, "本文")
        self.assertEqual(len(extracted), 1)

    def test_raises_when_ocr_process_fails(self):
        ST.extract_band_png = lambda src, t, y, h, out: None
        ST.run_ocr = lambda ocrcli, paths: ({}, True)
        ocr_text_at = ST.make_ocr_text_at("src.mp4", "./ocrcli", 900, 170, 30.0, "/tmp/probe")
        with self.assertRaises(RuntimeError):
            ocr_text_at(100)

    def test_raises_when_ocr_result_is_missing(self):
        ST.extract_band_png = lambda src, t, y, h, out: None
        ST.run_ocr = lambda ocrcli, paths: ({}, False)
        ocr_text_at = ST.make_ocr_text_at("src.mp4", "./ocrcli", 900, 170, 30.0, "/tmp/probe")
        with self.assertRaises(RuntimeError):
            ocr_text_at(100)


class MainTest(unittest.TestCase):
    def setUp(self):
        self._run_parallel = ST.run_extractions_parallel
        self._run_ocr = ST.run_ocr
        self._missing = ST.missing_ocr_results
        self._refine = ST.refine_segment_start
        self._make_ocr_text_at = ST.make_ocr_text_at
        ST.run_extractions_parallel = lambda jobs, max_workers: None

    def tearDown(self):
        ST.run_extractions_parallel = self._run_parallel
        ST.run_ocr = self._run_ocr
        ST.missing_ocr_results = self._missing
        ST.refine_segment_start = self._refine
        ST.make_ocr_text_at = self._make_ocr_text_at

    def _write_bounds(self, workdir, bound, fps=30.0, frames=None):
        d = {"fps": fps, "fps_str": "30/1", "bound": bound, "gstart": {}}
        if frames is not None:
            d["frames"] = frames
        with open(os.path.join(workdir, "bounds.json"), "w") as f:
            json.dump(d, f)

    def test_writes_segs_and_telops_with_no_correction_needed(self):
        with tempfile.TemporaryDirectory() as workdir:
            self._write_bounds(workdir, [0, 30, 60])
            ST.run_ocr = lambda ocrcli, paths: ({p: "本文%d" % i for i, p in enumerate(paths)}, False)
            ST.missing_ocr_results = lambda paths, ocr: []
            ST.refine_segment_start = lambda ocr_text_at, seg_start, seg_mid, center_text: seg_start
            rc = ST.main(["src.mp4", workdir, "./ocrcli", "--skip-band-check"])
            self.assertEqual(rc, 0)
            with open(os.path.join(workdir, "segs.json")) as f:
                segs = json.load(f)
            with open(os.path.join(workdir, "telops.txt"), encoding="utf-8") as f:
                telops = f.read()
        self.assertEqual(len(segs), 2)
        self.assertIn("本文0", telops)
        self.assertIn("本文1", telops)

    def test_applies_fade_in_correction_to_segment_start(self):
        with tempfile.TemporaryDirectory() as workdir:
            self._write_bounds(workdir, [0, 30, 60])
            ST.run_ocr = lambda ocrcli, paths: ({p: "本文" for p in paths}, False)
            ST.missing_ocr_results = lambda paths, ocr: []
            # 最初の区間だけ5フレーム分フェードインしていたことにする
            ST.refine_segment_start = (
                lambda ocr_text_at, seg_start, seg_mid, center_text:
                seg_start + 5 if seg_start == 0 else seg_start)
            old_stderr = sys.stderr
            captured = []

            class Capture:
                def write(self, s):
                    captured.append(s)

                def flush(self):
                    pass
            sys.stderr = Capture()
            try:
                rc = ST.main(["src.mp4", workdir, "./ocrcli", "--skip-band-check"])
            finally:
                sys.stderr = old_stderr
            self.assertEqual(rc, 0)
            with open(os.path.join(workdir, "segs.json")) as f:
                segs = json.load(f)
        self.assertAlmostEqual(segs[0][0], 5 / 30.0)
        # 補正が起きたことを標準エラーで知らせる（bounds.json/safe.txtには反映されない
        # ことに気づけるようにするため。Codexレビューで検出した限界の可視化）
        self.assertTrue(any("補正" in s for s in captured))

    def test_ocr_process_failure_does_not_write_output_files(self):
        with tempfile.TemporaryDirectory() as workdir:
            self._write_bounds(workdir, [0, 30, 60])
            ST.run_ocr = lambda ocrcli, paths: ({}, True)
            rc = ST.main(["src.mp4", workdir, "./ocrcli", "--skip-band-check"])
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.exists(os.path.join(workdir, "segs.json")))
            self.assertFalse(os.path.exists(os.path.join(workdir, "telops.txt")))

    def test_missing_ocr_results_does_not_write_output_files(self):
        with tempfile.TemporaryDirectory() as workdir:
            self._write_bounds(workdir, [0, 30, 60])
            ST.run_ocr = lambda ocrcli, paths: ({}, False)
            ST.missing_ocr_results = lambda paths, ocr: paths
            rc = ST.main(["src.mp4", workdir, "./ocrcli", "--skip-band-check"])
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.exists(os.path.join(workdir, "telops.txt")))

    def test_empty_rate_warning_is_printed_to_stderr(self):
        with tempfile.TemporaryDirectory() as workdir:
            self._write_bounds(workdir, [0, 30, 60])
            ST.run_ocr = lambda ocrcli, paths: ({p: "" for p in paths}, False)
            ST.missing_ocr_results = lambda paths, ocr: []
            ST.refine_segment_start = lambda ocr_text_at, seg_start, seg_mid, center_text: seg_start
            old_stderr = sys.stderr
            captured = []

            class Capture:
                def write(self, s):
                    captured.append(s)

                def flush(self):
                    pass
            sys.stderr = Capture()
            try:
                ST.main(["src.mp4", workdir, "./ocrcli", "--skip-band-check"])
            finally:
                sys.stderr = old_stderr
        self.assertTrue(any("空行率" in s for s in captured))

    def test_no_segments_in_time_window_still_writes_empty_output(self):
        with tempfile.TemporaryDirectory() as workdir:
            self._write_bounds(workdir, [0, 30, 60])
            ST.run_ocr = lambda ocrcli, paths: ({}, False)
            ST.missing_ocr_results = lambda paths, ocr: []
            rc = ST.main(["src.mp4", workdir, "./ocrcli", "--from", "1000", "--to", "2000",
                          "--skip-band-check"])
            self.assertEqual(rc, 0)
            with open(os.path.join(workdir, "segs.json")) as f:
                segs = json.load(f)
        self.assertEqual(segs, [])

    def test_includes_final_segment_up_to_bounds_frames_key(self):
        # Arrange: team-leadが合成素材（29.97fps・381フレーム、bound=[30,161,261]）で確認した
        # バグの再現。261フレーム目以降、動画の終わりまでのテロップがtelops.txt/segs.jsonに
        # 一切含まれていなかった。bounds.jsonの"frames"（総フレーム数）を読んで最後の区間も作る
        with tempfile.TemporaryDirectory() as workdir:
            self._write_bounds(workdir, [30, 161, 261], fps=30000.0 / 1001, frames=381)
            ST.run_ocr = lambda ocrcli, paths: (
                {p: "本文%d" % i for i, p in enumerate(paths)}, False)
            ST.missing_ocr_results = lambda paths, ocr: []
            ST.refine_segment_start = lambda ocr_text_at, seg_start, seg_mid, center_text: seg_start
            rc = ST.main(["src.mp4", workdir, "./ocrcli", "--skip-band-check"])
            self.assertEqual(rc, 0)
            with open(os.path.join(workdir, "segs.json")) as f:
                segs = json.load(f)
            with open(os.path.join(workdir, "telops.txt"), encoding="utf-8") as f:
                telops = f.read()
        fps = 30000.0 / 1001
        # (0,30)（先頭区間）・(30,161)・(161,261)・(261,381)（末尾区間）の4区間になる
        self.assertEqual(len(segs), 4)
        self.assertAlmostEqual(segs[0][0], 0 / fps)
        self.assertAlmostEqual(segs[3][0], 261 / fps)
        self.assertAlmostEqual(segs[3][1], 381 / fps)
        self.assertIn("本文3", telops)

    def test_final_segment_also_gets_fade_in_correction(self):
        # Arrange: 最後の区間（bounds.jsonのframesまで）にもフェードイン補正が効くこと
        with tempfile.TemporaryDirectory() as workdir:
            self._write_bounds(workdir, [30, 161, 261], fps=30000.0 / 1001, frames=381)
            ST.run_ocr = lambda ocrcli, paths: ({p: "本文" for p in paths}, False)
            ST.missing_ocr_results = lambda paths, ocr: []
            # 最後の区間(seg_start=261)だけ5フレーム分フェードインしていたことにする
            ST.refine_segment_start = (
                lambda ocr_text_at, seg_start, seg_mid, center_text:
                seg_start + 5 if seg_start == 261 else seg_start)
            rc = ST.main(["src.mp4", workdir, "./ocrcli", "--skip-band-check"])
            self.assertEqual(rc, 0)
            with open(os.path.join(workdir, "segs.json")) as f:
                segs = json.load(f)
        fps = 30000.0 / 1001
        self.assertAlmostEqual(segs[3][0], 266 / fps)

    def test_without_frames_key_stops_at_last_boundary_as_before(self):
        # Arrange: framesキーが無い古いbounds.json（後方互換）
        with tempfile.TemporaryDirectory() as workdir:
            self._write_bounds(workdir, [30, 161, 261])  # framesを渡さない
            ST.run_ocr = lambda ocrcli, paths: ({p: "本文" for p in paths}, False)
            ST.missing_ocr_results = lambda paths, ocr: []
            ST.refine_segment_start = lambda ocr_text_at, seg_start, seg_mid, center_text: seg_start
            rc = ST.main(["src.mp4", workdir, "./ocrcli", "--skip-band-check"])
            self.assertEqual(rc, 0)
            with open(os.path.join(workdir, "segs.json")) as f:
                segs = json.load(f)
        self.assertEqual(len(segs), 2)


if __name__ == "__main__":
    unittest.main()
