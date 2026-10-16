# -*- coding: utf-8 -*-
"""scripts/render.py の単体テスト（I-10）。
各カットを別入力にして `-ss` でシークすることによる高速化のコマンド組み立てを検証する。
ffmpeg を実際に呼ぶ検証（旧版との同一性比較）は実データで手動確認し、別途数値で報告する。
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import render as R  # noqa: E402


def input_indices(cmd):
    """cmd 中の "-i" の位置一覧（何番目の -i か、ではなく単純に出現順の値）を返す。"""
    return [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-i"]


def ss_values(cmd):
    """"-i" の直前にある "-ss" の値を、入力順に並べて返す（無ければ None）。"""
    values = []
    i = 0
    while i < len(cmd):
        if cmd[i] == "-i":
            if i >= 2 and cmd[i - 2] == "-ss":
                values.append(cmd[i - 1])
            else:
                values.append(None)
        i += 1
    return values


class BuildFfmpegCmdTest(unittest.TestCase):
    def test_one_input_per_cut_plus_base_png(self):
        # Arrange
        cuts = [(10.0, 16.0), (20.0, 30.0), (40.0, 50.0)]
        # Act
        cmd, _total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4")
        # Assert: カット数3つ＋base_png分の入力
        self.assertEqual(input_indices(cmd), [R.SRC, R.SRC, R.SRC, "base.png"])

    def test_ss_uses_margin_before_cut_start_when_room_available(self):
        # Arrange
        cuts = [(10.0, 16.0)]
        # Act
        cmd, _total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4", seek_margin=2.0)
        # Assert
        self.assertEqual(ss_values(cmd), ["8.000", None])

    def test_ss_is_clamped_to_zero_when_cut_start_is_within_margin(self):
        # Arrange: a=1.0 はマージン2.0秒より前なので ss が負にならないようにする
        cuts = [(1.0, 5.0)]
        # Act
        cmd, _total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4", seek_margin=2.0)
        # Assert
        self.assertEqual(ss_values(cmd), ["0.000", None])

    def test_trim_offsets_reflect_seek_position(self):
        # Arrange: a=10,b=16,margin=2 → ss=8 → trimはss基準で2.000〜8.000だが、
        # -ssの丸め誤差でカット端の1フレームが落ちないよう半フレーム分前倒しする（I-12）。
        cuts = [(10.0, 16.0)]
        # Act
        cmd, _total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4", seek_margin=2.0)
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        # Assert
        self.assertIn("trim=start=%.3f:end=%.3f" % (2.0 - R.HALF_FRAME, 8.0 - R.HALF_FRAME),
                      filter_complex)
        self.assertIn("atrim=start=%.3f:end=%.3f" % (2.0 - R.HALF_FRAME, 8.0 - R.HALF_FRAME),
                      filter_complex)

    def test_trim_offset_when_seek_clamped_to_zero(self):
        # Arrange: a=1.0,b=5.0,margin=2 → ss=0 → offsetはaそのもの。半フレーム前倒しした上で
        # 負値にはクランプする
        cuts = [(1.0, 5.0)]
        # Act
        cmd, _total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4", seek_margin=2.0)
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        # Assert
        self.assertIn("trim=start=%.3f:end=%.3f" % (1.0 - R.HALF_FRAME, 5.0 - R.HALF_FRAME),
                      filter_complex)

    def test_trim_start_is_clamped_to_zero_when_half_frame_shift_would_go_negative(self):
        # Arrange: a=0.005,b=4.0,margin=2 → ss=0 → offset=a=0.005 は半フレーム(≈0.0167)より
        # 小さいので、前倒し後にstartが負にならずゼロでクランプされる
        cuts = [(0.005, 4.0)]
        # Act
        cmd, _total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4", seek_margin=2.0)
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        # Assert
        self.assertIn("trim=start=0.000:end=%.3f" % (4.0 - R.HALF_FRAME), filter_complex)

    def test_base_png_input_declares_matching_framerate(self):
        # Arrange: 背景PNGの -loop 1 入力が既定25fpsのままだと、overlay の出力タイミングが
        # それに支配されて本編側でフレーム重複が起きる（実データで確認済み・I-11）。
        # 本編と同じ FPS を明示的に -framerate で与える。
        cuts = [(10.0, 16.0)]
        # Act
        cmd, _total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4")
        base_i_index = cmd.index("base.png") - 1
        # Assert
        self.assertEqual(cmd[base_i_index], "-i")
        self.assertEqual(cmd[base_i_index - 1], "1")
        self.assertEqual(cmd[base_i_index - 2], "-loop")
        self.assertEqual(cmd[base_i_index - 3], R.FPS)
        self.assertEqual(cmd[base_i_index - 4], "-framerate")

    def test_base_png_overlay_references_index_after_last_cut(self):
        # Arrange: カットが3つなら base_png の入力インデックスは3
        cuts = [(10.0, 16.0), (20.0, 30.0), (40.0, 50.0)]
        # Act
        cmd, _total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4")
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        # Assert
        self.assertIn("[3:v][VC]overlay=0:552", filter_complex)

    def test_per_cut_video_and_audio_reference_their_own_input_index(self):
        # Arrange
        cuts = [(10.0, 16.0), (20.0, 30.0)]
        # Act
        cmd, _total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4")
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        # Assert
        self.assertIn("[0:v]trim=", filter_complex)
        self.assertIn("[0:a]atrim=", filter_complex)
        self.assertIn("[1:v]trim=", filter_complex)
        self.assertIn("[1:a]atrim=", filter_complex)

    def test_total_duration_matches_legacy_calculation_without_flash(self):
        # Arrange: flash_after=None（トランジション無し）は単純合計
        cuts = [(10.0, 16.0), (20.0, 30.0), (40.0, 50.0)]
        # Act
        _cmd, total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4", flash_after=None)
        # Assert
        self.assertAlmostEqual(total, 6.0 + 10.0 + 10.0)

    def test_total_duration_matches_legacy_calculation_with_flash(self):
        # Arrange: flash_after=0（既定）はフラッシュ秒だけ短くなる
        cuts = [(10.0, 16.0), (20.0, 30.0), (40.0, 50.0)]
        # Act
        _cmd, total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4", flash_after=0, flash_dur=0.35)
        # Assert
        self.assertAlmostEqual(total, 6.0 + 10.0 + 10.0 - 0.35)

    def test_output_encoding_flags_are_unchanged(self):
        # Arrange
        cuts = [(10.0, 16.0)]
        # Act
        cmd, total = R.build_ffmpeg_cmd(cuts, "base.png", "out.mp4")
        # Assert: 出力仕様（1080x1920はscale/overlay側、FPS・エンコード設定）は変えない
        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertIn("-pix_fmt", cmd)
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")
        self.assertIn("-r", cmd)
        self.assertEqual(cmd[cmd.index("-r") + 1], R.FPS)
        self.assertIn("-t", cmd)
        # -t は境界の丸めでカット尻の1フレームを削らないよう半フレーム分の余裕を持たせる（I-12）。
        # total（戻り値・音声フェード計算基準）自体はフレーム境界ちょうどのまま変えない。
        self.assertEqual(cmd[cmd.index("-t") + 1], "%.3f" % (total + R.HALF_FRAME))
        self.assertEqual(cmd[-1], "out.mp4")

    def test_render_calls_ffmpeg_with_built_command(self):
        # Arrange
        calls = []
        original_run = R.subprocess.run

        def fake_run(cmd, check=False):
            calls.append(cmd)

        R.subprocess.run = fake_run
        try:
            # Act
            total = R.render([(10.0, 16.0)], "base.png", "out.mp4")
        finally:
            R.subprocess.run = original_run
        # Assert
        self.assertEqual(len(calls), 1)
        self.assertIn("out.mp4", calls[0])
        self.assertAlmostEqual(total, 6.0)


class SnapFrameTest(unittest.TestCase):
    def test_snap_frame_rounds_to_nearest_frame(self):
        # Arrange / Act / Assert
        self.assertEqual(R.snap_frame(0.0), 0)
        self.assertEqual(R.snap_frame(1.0), round(1.0 * R.FPSF))

    def test_frame_time_is_inverse_of_snap_frame(self):
        # Arrange
        f = 1000
        # Act / Assert: frame_time(f) を snap_frame に通すと同じフレームに戻る
        self.assertEqual(R.snap_frame(R.frame_time(f)), f)


class SnapCutsTest(unittest.TestCase):
    def test_snaps_each_cut_to_frame_grid(self):
        # Arrange
        cuts = [(10.0, 16.0), (20.5, 25.5)]
        # Act
        result = R.snap_cuts(cuts)
        # Assert
        expected = [(R.snap_frame(10.0), R.snap_frame(16.0)),
                    (R.snap_frame(20.5), R.snap_frame(25.5))]
        self.assertEqual(result, expected)

    def test_raises_when_snap_collapses_cut_to_zero_frames(self):
        # Arrange: 1万分の1秒のカットは同じフレームに丸まる
        cuts = [(10.0000, 10.0001)]
        # Act / Assert
        with self.assertRaises(ValueError):
            R.snap_cuts(cuts)

    def test_raises_when_cuts_is_empty(self):
        # Act / Assert: 空のcutsを黙って空タイムラインにしない
        with self.assertRaises(ValueError):
            R.snap_cuts([])

    def test_raises_when_start_is_negative(self):
        # Act / Assert
        with self.assertRaises(ValueError):
            R.snap_cuts([(-1.0, 5.0)])

    def test_raises_when_start_is_not_finite(self):
        # Act / Assert
        with self.assertRaises(ValueError):
            R.snap_cuts([(float("nan"), 5.0)])

    def test_raises_when_end_is_not_finite(self):
        # Act / Assert
        with self.assertRaises(ValueError):
            R.snap_cuts([(1.0, float("inf"))])


class BuildTimelineTest(unittest.TestCase):
    def test_no_flash_is_plain_concatenation_in_frames(self):
        # Arrange: n_frames = 180, 300
        cut_frames = [(300, 480), (600, 900)]
        # Act
        cuts_info, total = R.build_timeline(cut_frames, flash_after=None, flash_frames=0)
        # Assert
        self.assertEqual(cuts_info[0]["out_start_frame"], 0)
        self.assertEqual(cuts_info[0]["n_frames"], 180)
        self.assertEqual(cuts_info[1]["out_start_frame"], 180)
        self.assertEqual(total, 480)

    def test_flash_shifts_group_b_back_by_flash_frames(self):
        # Arrange: n_frames = 180, 300, 300。flash_after=0（既定）
        cut_frames = [(300, 480), (600, 900), (1200, 1500)]
        # Act
        cuts_info, total = R.build_timeline(cut_frames, flash_after=0, flash_frames=10)
        # Assert
        self.assertEqual(cuts_info[0]["out_start_frame"], 0)
        self.assertEqual(cuts_info[1]["out_start_frame"], 180 - 10)
        self.assertEqual(cuts_info[2]["out_start_frame"], 180 - 10 + 300)
        self.assertEqual(total, 180 + 300 + 300 - 10)


class WriteTimelineTest(unittest.TestCase):
    def test_writes_expected_json_structure(self):
        # Arrange
        cuts_info = [{"index": 0, "src_start_frame": 300, "src_end_frame": 480,
                      "n_frames": 180, "out_start_frame": 0}]
        with tempfile.TemporaryDirectory() as d:
            out_path = os.path.join(d, "clip.mp4")
            # Act
            path = R.write_timeline(out_path, cuts_info, 180, 0, 0.334, 10)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        # Assert
        self.assertEqual(path, out_path + ".timeline.json")
        self.assertEqual(data["total_frames"], 180)
        self.assertEqual(data["flash_after"], 0)
        self.assertEqual(data["flash_frames"], 10)
        self.assertEqual(data["cuts"], cuts_info)


class ParseArgsTest(unittest.TestCase):
    def test_workdir_is_first_positional_argument(self):
        # Arrange / Act
        args = R.parse_args(["/some/dir", "clips_plan.py"])
        # Assert: gateフックが第1位置引数をworkdirとして判定するため
        self.assertEqual(args.workdir, "/some/dir")
        self.assertEqual(args.clips_plan, "clips_plan.py")
        self.assertEqual(args.out, "out")
        self.assertIsNone(args.only)

    def test_out_and_only_options(self):
        # Arrange / Act
        args = R.parse_args(["/d", "plan.py", "--out", "out2", "--only", "a.mp4", "b.mp4"])
        # Assert
        self.assertEqual(args.out, "out2")
        self.assertEqual(args.only, ["a.mp4", "b.mp4"])


class SelectClipsTest(unittest.TestCase):
    def test_returns_all_with_original_index_when_only_is_none(self):
        # Arrange
        clips = [{"file": "a.mp4"}, {"file": "b.mp4"}]
        # Act / Assert
        self.assertEqual(R.select_clips(clips, None), [(0, clips[0]), (1, clips[1])])

    def test_filters_by_file_name_keeping_original_index(self):
        # Arrange
        clips = [{"file": "a.mp4"}, {"file": "b.mp4"}]
        # Act / Assert
        self.assertEqual(R.select_clips(clips, ["b.mp4"]), [(1, clips[1])])

    def test_raises_when_only_references_unknown_file(self):
        # Arrange
        clips = [{"file": "a.mp4"}]
        # Act / Assert
        with self.assertRaises(ValueError):
            R.select_clips(clips, ["missing.mp4"])


class BaseImagePathTest(unittest.TestCase):
    def test_uses_one_based_two_digit_numbering(self):
        # Arrange / Act / Assert
        self.assertEqual(R.base_image_path(0), os.path.join("base", "base_01.png"))
        self.assertEqual(R.base_image_path(3), os.path.join("base", "base_04.png"))


class BaseImageContentHashTest(unittest.TestCase):
    def test_same_headings_produce_same_hash(self):
        # Arrange
        clip_a = {"l1": [("x", "w")], "l2": [("y", "y")], "quote": [("z", "r")]}
        clip_b = {"l1": [("x", "w")], "l2": [("y", "y")], "quote": [("z", "r")]}
        # Act / Assert
        self.assertEqual(R.base_image_content_hash(clip_a), R.base_image_content_hash(clip_b))

    def test_different_headings_produce_different_hash(self):
        # Arrange
        clip_a = {"l1": [("x", "w")], "l2": [], "quote": []}
        clip_b = {"l1": [("changed", "w")], "l2": [], "quote": []}
        # Act / Assert
        self.assertNotEqual(R.base_image_content_hash(clip_a), R.base_image_content_hash(clip_b))


class EnsureBaseImageTest(unittest.TestCase):
    def _fake_mkbase(self, calls):
        def fake_build(l1, l2, quote, out):
            calls.append(out)
            open(out, "a").close()
        return types.SimpleNamespace(build=fake_build)

    def test_builds_when_missing(self):
        # Arrange: 実際のPILレンダリングを避けるため mkbase をフェイクに差し替える
        calls = []
        sys.modules["mkbase"] = self._fake_mkbase(calls)
        try:
            with tempfile.TemporaryDirectory() as d:
                base_png = os.path.join(d, "base", "base_01.png")
                clip = {"l1": [], "l2": [], "quote": []}
                # Act
                R.ensure_base_image(clip, base_png)
                # Assert: 画像とハッシュのサイドカーが両方できる
                self.assertEqual(calls, [base_png])
                self.assertTrue(os.path.exists(base_png))
                self.assertTrue(os.path.exists(base_png + ".hash"))
        finally:
            del sys.modules["mkbase"]

    def test_reuses_when_content_unchanged(self):
        # Arrange
        calls = []
        sys.modules["mkbase"] = self._fake_mkbase(calls)
        try:
            with tempfile.TemporaryDirectory() as d:
                base_png = os.path.join(d, "base", "base_01.png")
                clip = {"l1": [("x", "w")], "l2": [], "quote": []}
                R.ensure_base_image(clip, base_png)
                # Act: 同じ内容のclipで2回目を呼ぶ
                R.ensure_base_image(clip, base_png)
                # Assert: 再生成されない
                self.assertEqual(calls, [base_png])
        finally:
            del sys.modules["mkbase"]

    def test_rebuilds_when_content_changed_at_same_path(self):
        # Arrange: 同じ位置番号(base_01.png)でも、見出しの文言が変わっていれば
        # 古い画像を黙って使い回さない（実運用でクリップの並べ替え・差し替え時に起こりうる事故）
        calls = []
        sys.modules["mkbase"] = self._fake_mkbase(calls)
        try:
            with tempfile.TemporaryDirectory() as d:
                base_png = os.path.join(d, "base", "base_01.png")
                clip_a = {"l1": [("旧見出し", "w")], "l2": [], "quote": []}
                clip_b = {"l1": [("新見出し", "w")], "l2": [], "quote": []}
                R.ensure_base_image(clip_a, base_png)
                # Act: 別の内容のclipを同じパスに対して呼ぶ
                R.ensure_base_image(clip_b, base_png)
                # Assert: 2回とも生成される
                self.assertEqual(calls, [base_png, base_png])
        finally:
            del sys.modules["mkbase"]

    def test_rebuilds_when_hash_sidecar_missing(self):
        # Arrange: 画像ファイルだけが存在し、ハッシュのサイドカーが無い
        # （手動で置かれた・旧バージョンで生成された等）場合は安全側で再生成する
        calls = []
        sys.modules["mkbase"] = self._fake_mkbase(calls)
        try:
            with tempfile.TemporaryDirectory() as d:
                base_png = os.path.join(d, "base", "base_01.png")
                os.makedirs(os.path.dirname(base_png), exist_ok=True)
                open(base_png, "a").close()
                clip = {"l1": [], "l2": [], "quote": []}
                # Act
                R.ensure_base_image(clip, base_png)
                # Assert
                self.assertEqual(calls, [base_png])
        finally:
            del sys.modules["mkbase"]


class MeasureLoudnormTest(unittest.TestCase):
    def test_parses_json_from_stderr(self):
        # Arrange
        class FakeResult:
            returncode = 0
            stderr = ("some ffmpeg logs\n{\n"
                      '  "input_i" : "-15.94",\n'
                      '  "input_tp" : "-1.41",\n'
                      '  "input_lra" : "9.50",\n'
                      '  "input_thresh" : "-26.88",\n'
                      '  "target_offset" : "1.05"\n'
                      "}\nmore logs")
        original = R.subprocess.run
        R.subprocess.run = lambda *a, **k: FakeResult()
        try:
            # Act
            stats = R.measure_loudnorm("out.mp4")
        finally:
            R.subprocess.run = original
        # Assert
        self.assertEqual(stats["input_i"], "-15.94")
        self.assertEqual(stats["target_offset"], "1.05")

    def test_raises_when_json_not_found_in_output(self):
        # Arrange
        class FakeResult:
            returncode = 0
            stderr = "no json here"
        original = R.subprocess.run
        R.subprocess.run = lambda *a, **k: FakeResult()
        try:
            # Act / Assert
            with self.assertRaises(RuntimeError):
                R.measure_loudnorm("out.mp4")
        finally:
            R.subprocess.run = original

    def test_raises_when_returncode_nonzero(self):
        # Arrange: stderrにJSONらしき断片が残っていても、終了コードが非0なら失敗として扱う
        # （returncodeを見ずJSONの有無だけで判定していないことを確認する）
        class FakeResult:
            returncode = 1
            stderr = ('fatal error after partial output\n{\n'
                      '  "input_i" : "-15.94",\n'
                      '  "input_tp" : "-1.41",\n'
                      '  "input_lra" : "9.50",\n'
                      '  "input_thresh" : "-26.88",\n'
                      '  "target_offset" : "1.05"\n}')
        original = R.subprocess.run
        R.subprocess.run = lambda *a, **k: FakeResult()
        try:
            # Act / Assert
            with self.assertRaises(RuntimeError):
                R.measure_loudnorm("out.mp4")
        finally:
            R.subprocess.run = original

    def test_raises_when_required_field_missing(self):
        # Arrange: JSONは解析できるが必須フィールド(input_tp)が欠けている
        class FakeResult:
            returncode = 0
            stderr = ('{\n  "input_i" : "-15.94"\n}')
        original = R.subprocess.run
        R.subprocess.run = lambda *a, **k: FakeResult()
        try:
            # Act / Assert
            with self.assertRaises(RuntimeError):
                R.measure_loudnorm("out.mp4")
        finally:
            R.subprocess.run = original


class ParseEbur128SummaryTest(unittest.TestCase):
    def test_parses_integrated_loudness_and_true_peak_from_summary(self):
        # Arrange: 実際のffmpeg出力を模す（frame毎のログ行と最終Summaryを両方含む）。
        # frame毎の行にも "I: ... LUFS" が出るため、Summary内の値だけを拾えるか確認する。
        text = (
            "[Parsed_ebur128_0 @ 0x0] t: 1.0  I: -20.0 LUFS  TARGET:-23 LUFS\n"
            "[Parsed_ebur128_0 @ 0x0] Summary:\n\n"
            "  Integrated loudness:\n"
            "    I:         -14.4 LUFS\n"
            "    Threshold: -24.9 LUFS\n\n"
            "  Loudness range:\n"
            "    LRA:         2.4 LU\n\n"
            "  True peak:\n"
            "    Peak:       -1.4 dBFS\n"
        )
        # Act
        summary = R.parse_ebur128_summary(text)
        # Assert
        self.assertEqual(summary["integrated"], -14.4)
        self.assertEqual(summary["true_peak"], -1.4)

    def test_true_peak_is_none_when_peak_measurement_not_present(self):
        # Arrange: peak=trueを付けなかった場合はTrue peakセクションが出ない
        text = ("[Parsed_ebur128_0 @ 0x0] Summary:\n\n"
                "  Integrated loudness:\n"
                "    I:         -14.4 LUFS\n")
        # Act
        summary = R.parse_ebur128_summary(text)
        # Assert
        self.assertIsNone(summary["true_peak"])

    def test_raises_when_summary_not_found(self):
        # Act / Assert
        with self.assertRaises(RuntimeError):
            R.parse_ebur128_summary("no summary here")


class MeasureEbur128Test(unittest.TestCase):
    def test_runs_ebur128_filter_and_parses_stderr(self):
        # Arrange
        class FakeResult:
            returncode = 0
            stderr = ("[Parsed_ebur128_0 @ 0x0] Summary:\n\n"
                      "  Integrated loudness:\n"
                      "    I:         -14.4 LUFS\n\n"
                      "  True peak:\n"
                      "    Peak:       -1.4 dBFS\n")
        calls = []

        def fake_run(cmd, **k):
            calls.append(cmd)
            return FakeResult()

        original = R.subprocess.run
        R.subprocess.run = fake_run
        try:
            # Act
            summary = R.measure_ebur128("out.mp4")
        finally:
            R.subprocess.run = original
        # Assert
        self.assertEqual(summary["integrated"], -14.4)
        self.assertIn("ebur128", calls[0][calls[0].index("-af") + 1])

    def test_raises_when_returncode_nonzero(self):
        # Arrange: stderrにSummaryらしき断片が残っていても、終了コードが非0なら失敗として扱う
        # （returncodeを見ずSummaryの有無だけで判定していないことを確認する）
        class FakeResult:
            returncode = 1
            stderr = ("fatal error after partial output\nSummary:\n\n"
                      "  Integrated loudness:\n"
                      "    I:         -14.4 LUFS\n\n"
                      "  True peak:\n"
                      "    Peak:       -1.4 dBFS\n")
        original = R.subprocess.run
        R.subprocess.run = lambda *a, **k: FakeResult()
        try:
            # Act / Assert
            with self.assertRaises(RuntimeError):
                R.measure_ebur128("out.mp4")
        finally:
            R.subprocess.run = original

    def test_raises_when_true_peak_missing(self):
        # Arrange: peak=trueを付けて呼んでいるのにTrue Peakが解析できない
        # （ffmpeg側の出力形式が変わった等）場合、Noneのまま黙って進めない
        class FakeResult:
            returncode = 0
            stderr = ("[Parsed_ebur128_0 @ 0x0] Summary:\n\n"
                      "  Integrated loudness:\n"
                      "    I:         -14.4 LUFS\n")
        original = R.subprocess.run
        R.subprocess.run = lambda *a, **k: FakeResult()
        try:
            # Act / Assert
            with self.assertRaises(RuntimeError):
                R.measure_ebur128("out.mp4")
        finally:
            R.subprocess.run = original


class NeedsLoudnessCorrectionTest(unittest.TestCase):
    def test_within_threshold_does_not_need_correction(self):
        # Arrange / Act / Assert: 目標-14.0に対し-14.15は0.2LU以内
        self.assertFalse(R.needs_loudness_correction(-14.15))

    def test_outside_threshold_needs_correction(self):
        # Arrange / Act / Assert: -14.4は0.2LUを超えている
        self.assertTrue(R.needs_loudness_correction(-14.4))

    def test_exactly_at_threshold_does_not_need_correction(self):
        # Arrange / Act / Assert: ちょうど0.2LUは「超えた」場合のみ補正する運用に揃える
        self.assertFalse(R.needs_loudness_correction(
            R.LOUDNESS_TARGET_I - R.LOUDNESS_CORRECTION_THRESHOLD))


class LoudnessResultOkTest(unittest.TestCase):
    """最終エンコード後の実測が許容範囲内かどうかの判定（純粋関数）。
    fix_loudnessはどの分岐（補正した/しなかった/打ち切った）でもここを通す。
    """

    def test_ok_when_integrated_within_tolerance_and_true_peak_safe(self):
        # Act / Assert
        ok, problems = R.loudness_result_ok(-14.3, -1.2)
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_ng_when_integrated_outside_tolerance(self):
        # Arrange: 目標-14.0から0.5LU超（LOUDNESS_TOLERANCE基準）
        # Act
        ok, problems = R.loudness_result_ok(-14.8, -1.2)
        # Assert
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)

    def test_ng_when_true_peak_exceeds_max_acceptable(self):
        # Arrange: Integratedは範囲内だがTrue Peakが上限を超えている
        # Act
        ok, problems = R.loudness_result_ok(-14.0, -0.5)
        # Assert
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)

    def test_reports_both_problems_when_both_outside_range(self):
        # Act
        ok, problems = R.loudness_result_ok(-16.0, 0.0)
        # Assert
        self.assertFalse(ok)
        self.assertEqual(len(problems), 2)

    def test_ok_when_true_peak_unknown(self):
        # Arrange: True Peakが測れていない場合はTP側の判定はできないので通す
        # (measure_ebur128自体がTrue Peak欠落ならRuntimeErrorで落ちるため、
        # ここに来る時点でNoneは基本的に来ない想定だが、防御的に確認する)
        # Act
        ok, problems = R.loudness_result_ok(-14.0, None)
        # Assert
        self.assertTrue(ok)
        self.assertEqual(problems, [])


class GainToTargetTest(unittest.TestCase):
    def test_returns_positive_gain_when_quieter_than_target(self):
        # Arrange / Act / Assert
        self.assertAlmostEqual(R.gain_to_target(-15.0), 1.0)

    def test_returns_negative_gain_when_louder_than_target(self):
        # Arrange / Act / Assert
        self.assertAlmostEqual(R.gain_to_target(-12.0), -2.0)


class ClampGainForTruePeakTest(unittest.TestCase):
    def test_clamps_gain_that_would_exceed_true_peak_ceiling(self):
        # Arrange: true_peak=-2.0, ceiling=-1.5 → 余裕は0.5dBなので3.0dBは0.5dBに抑える
        # Act / Assert
        self.assertAlmostEqual(R.clamp_gain_for_true_peak(3.0, -2.0), 0.5)

    def test_keeps_gain_unchanged_when_within_true_peak_headroom(self):
        # Arrange / Act / Assert: 余裕(3.5dB)の範囲内なのでそのまま
        self.assertAlmostEqual(R.clamp_gain_for_true_peak(1.0, -5.0), 1.0)

    def test_keeps_gain_unchanged_when_true_peak_unknown(self):
        # Arrange / Act / Assert: True Peak実測が無ければクランプしようがない
        self.assertAlmostEqual(R.clamp_gain_for_true_peak(3.0, None), 3.0)

    def test_floors_boost_at_zero_when_true_peak_already_over_ceiling(self):
        # Arrange: true_peak=-1.4, ceiling=-1.5 → 既に0.1dB超過している状態。
        # ここでさらにブースト(+0.4dB)しようとすると超過が悪化するので0dBに抑える
        # （マイナス側に反転させて余計なカットはしない。既存の程度を維持するだけ）。
        # Act / Assert
        self.assertAlmostEqual(R.clamp_gain_for_true_peak(0.4, -1.4), 0.0)

    def test_still_cuts_when_negative_gain_and_true_peak_over_ceiling(self):
        # Arrange: 元々音量を下げる方向(-0.5dB)なら、超過分もあわせてさらに下げてよい
        # Act / Assert: headroom=-1.5-(-1.4)=-0.1 の方がより安全側(小さい)なのでそちらを採用
        self.assertAlmostEqual(R.clamp_gain_for_true_peak(-0.5, -1.4), -0.5)


class ProbeVideoFrameCountTest(unittest.TestCase):
    def test_returns_frame_count_from_ffprobe(self):
        # Arrange
        class FakeResult:
            returncode = 0
            stdout = "180\n"
        original = R.subprocess.run
        R.subprocess.run = lambda *a, **k: FakeResult()
        try:
            # Act
            n = R.probe_video_frame_count("out.mp4")
        finally:
            R.subprocess.run = original
        # Assert
        self.assertEqual(n, 180)


class ProbeVideoDurationSecondsTest(unittest.TestCase):
    def test_converts_frame_count_to_seconds(self):
        # Arrange: probe_video_duration_secondsはprobe_video_frame_countの結果を秒に変換する
        original = R.probe_video_frame_count
        R.probe_video_frame_count = lambda path: 180
        try:
            # Act
            duration = R.probe_video_duration_seconds("out.mp4")
        finally:
            R.probe_video_frame_count = original
        # Assert
        self.assertAlmostEqual(duration, 180 / R.FPSF)


class FixLoudnessTest(unittest.TestCase):
    """判定・検証はebur128（測定バイアスの無い方）に統一する。loudnormのprint_format=json
    は同じ素材でもebur128実測より最大0.3LU程度低く出る系統的バイアスがあり、これを判定に
    使うと「補正済みのつもりが実は目標より低いまま」になる（2026-09-17実測）。
    """

    def _patch(self, measure_ebur128, measure_loudnorm=None):
        calls = []
        originals = {
            "measure_ebur128": R.measure_ebur128,
            "measure_loudnorm": R.measure_loudnorm,
            "probe_video_duration_seconds": R.probe_video_duration_seconds,
            "subprocess.run": R.subprocess.run,
            "os.replace": R.os.replace,
        }
        R.measure_ebur128 = measure_ebur128
        if measure_loudnorm is not None:
            R.measure_loudnorm = measure_loudnorm
        else:
            R.measure_loudnorm = lambda path: self.fail("measure_loudnormは呼ばれないはず")
        R.probe_video_duration_seconds = lambda path: 6.006
        R.subprocess.run = lambda cmd, **k: calls.append(cmd)
        R.os.replace = lambda a, b: calls.append(("replace", a, b))
        return calls, originals

    def _restore(self, originals):
        R.measure_ebur128 = originals["measure_ebur128"]
        R.measure_loudnorm = originals["measure_loudnorm"]
        R.probe_video_duration_seconds = originals["probe_video_duration_seconds"]
        R.subprocess.run = originals["subprocess.run"]
        R.os.replace = originals["os.replace"]

    def test_within_threshold_only_fixes_duration_without_touching_loudness(self):
        # Arrange: 目標-14.0に対し-14.15はLOUDNESS_CORRECTION_THRESHOLD(0.2LU)以内。
        # それでも音声の長さは常に映像に合わせてremuxし、再エンコード後の状態を測り直す。
        measurements = iter([
            {"integrated": -14.15, "true_peak": -1.4},  # 初回判定（補正不要）
            {"integrated": -14.15, "true_peak": -1.4},  # 長さ合わせのremux後の最終確認
        ])
        calls, originals = self._patch(measure_ebur128=lambda path: next(measurements))
        try:
            # Act
            result = R.fix_loudness("out.mp4")
        finally:
            self._restore(originals)
        # Assert
        self.assertFalse(result["applied"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["integrated"], -14.15)
        ffmpeg_cmd = calls[0]
        af = ffmpeg_cmd[ffmpeg_cmd.index("-af") + 1]
        self.assertNotIn("loudnorm", af)
        self.assertNotIn("volume", af)
        # apad: 音声が映像より短くても -t だけでは伸びない（上限にしかならない）ため、
        # 無音パディングしてから -t で正確な長さに揃える
        self.assertIn("apad", af)
        self.assertEqual(ffmpeg_cmd[ffmpeg_cmd.index("-c:v") + 1], "copy")
        self.assertEqual(ffmpeg_cmd[ffmpeg_cmd.index("-t") + 1], "6.006000")
        self.assertEqual(len([c for c in calls if isinstance(c, list)]), 1)

    def test_one_loudnorm_pass_fixes_it_within_threshold(self):
        # Arrange: 初回-14.5(0.5LUずれ)→loudnorm補正1回で-14.05(0.05LUずれ)に収まる
        stats = {"input_i": "-14.5", "input_tp": "-1.41", "input_lra": "9.50",
                  "input_thresh": "-26.88", "target_offset": "0.5"}
        measurements = iter([
            {"integrated": -14.5, "true_peak": -1.4},
            {"integrated": -14.05, "true_peak": -1.4},
        ])
        calls, originals = self._patch(
            measure_ebur128=lambda path: next(measurements),
            measure_loudnorm=lambda path: stats)
        try:
            # Act
            result = R.fix_loudness("out.mp4")
        finally:
            self._restore(originals)
        # Assert: loudnormベースの補正が1回だけ行われ、それ以上は補正しない
        self.assertTrue(result["applied"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["integrated"], -14.05)
        ffmpeg_calls = [c for c in calls if isinstance(c, list)]
        self.assertEqual(len(ffmpeg_calls), 1)
        af = ffmpeg_calls[0][ffmpeg_calls[0].index("-af") + 1]
        self.assertIn("measured_I=-14.5", af)
        self.assertIn("linear=true", af)
        self.assertIn("apad", af)
        self.assertEqual(ffmpeg_calls[0][ffmpeg_calls[0].index("-t") + 1], "6.006000")

    def test_falls_back_to_volume_correction_when_still_outside_after_loudnorm_pass(self):
        # Arrange: 1回目のloudnorm補正後もまだ0.2LUを超えてずれている場合、
        # 2回目はvolume(dB)で直接ebur128実測とのずれを当てる（最大2回まで）
        stats = {"input_i": "-14.5", "input_tp": "-1.41", "input_lra": "9.50",
                  "input_thresh": "-26.88", "target_offset": "0.5"}
        measurements = iter([
            {"integrated": -14.5, "true_peak": -1.4},   # 初回判定
            {"integrated": -14.3, "true_peak": -5.0},   # 1回目補正後（まだ0.2LU超）
            {"integrated": -14.05, "true_peak": -5.0},  # 2回目補正後
        ])
        calls, originals = self._patch(
            measure_ebur128=lambda path: next(measurements),
            measure_loudnorm=lambda path: stats)
        try:
            # Act
            result = R.fix_loudness("out.mp4")
        finally:
            self._restore(originals)
        # Assert
        self.assertTrue(result["applied"])
        self.assertTrue(result["ok"])
        ffmpeg_calls = [c for c in calls if isinstance(c, list)]
        self.assertEqual(len(ffmpeg_calls), 2)
        af0 = ffmpeg_calls[0][ffmpeg_calls[0].index("-af") + 1]
        self.assertIn("measured_I=-14.5", af0)
        af1 = ffmpeg_calls[1][ffmpeg_calls[1].index("-af") + 1]
        # -14.3 → 目標-14.0まで+0.3dB、true_peak=-5.0なので余裕がありクランプされない
        self.assertIn("volume=0.30dB", af1)
        self.assertIn("apad", af1)
        self.assertNotIn("loudnorm", af1)

    def test_skips_second_pass_when_clamped_gain_is_negligible(self):
        # Arrange: 1回目のloudnorm補正後、True Peakが既にceiling付近(-1.2)で
        # クランプ後のゲインがほぼ0dBになる場合、2回目のvolumeパスは行わない。
        # 実データで確認: 無音等価(0dB)のvolumeパスでも音声を再エンコードし直すと、
        # AACの世代重ねでTrue Peakがさらに悪化した（-1.2dBFS→+0.4dBFSに増加）ため、
        # 得るもの(ラウドネス改善)が無い補正は再エンコードごと省く。
        # -14.3/-1.2は最終的にok(許容0.5LU・TP上限-1.0)の範囲内なので"ok"はTrueになる。
        stats = {"input_i": "-15.5", "input_tp": "-1.3", "input_lra": "5.20",
                  "input_thresh": "-25.94", "target_offset": "0.59"}
        measurements = iter([
            {"integrated": -15.5, "true_peak": -1.3},  # 初回判定
            {"integrated": -14.3, "true_peak": -1.2},  # 1回目補正後（まだ0.2LU超だがTPに余裕なし）
        ])
        calls, originals = self._patch(
            measure_ebur128=lambda path: next(measurements),
            measure_loudnorm=lambda path: stats)
        try:
            # Act
            result = R.fix_loudness("out.mp4")
        finally:
            self._restore(originals)
        # Assert: 1回目の補正は適用されるが、2回目は(再エンコードも含めて)スキップする
        self.assertTrue(result["applied"])
        self.assertTrue(result["ok"])
        ffmpeg_calls = [c for c in calls if isinstance(c, list)]
        self.assertEqual(len(ffmpeg_calls), 1)

    def test_uses_true_peak_limited_dynamic_loudnorm_when_headroom_blocks_volume(self):
        # Arrange: 2026-10-12 凪さん密着02の実データ。linearのloudnormでは-15.1のまま上がらず、
        # True Peakに余裕が無いのでvolumeのゲインもほぼ0dBにクランプされ、許容(±0.5LU)外の
        # まま打ち切られていた。手動でloudnormの動的モード(TPで頭を抑えつつ持ち上げる)を
        # 掛けたら-14.2に収まった。この場合だけ動的モードの補正を1回だけ追加する。
        stats = {"input_i": "-15.17", "input_tp": "-1.37", "input_lra": "3.90",
                 "input_thresh": "-25.85", "target_offset": "0.56"}
        measurements = iter([
            {"integrated": -15.17, "true_peak": -1.37},  # 初回判定
            {"integrated": -15.10, "true_peak": -1.40},  # linear後（上がらない・TPに余裕なし）
            {"integrated": -14.20, "true_peak": -1.10},  # 動的モード後
        ])
        calls, originals = self._patch(
            measure_ebur128=lambda path: next(measurements),
            measure_loudnorm=lambda path: stats)
        try:
            # Act
            result = R.fix_loudness("out.mp4")
        finally:
            self._restore(originals)
        # Assert: linear → (volumeはクランプで省略) → 動的モード の2回の再エンコードで収まる
        self.assertTrue(result["applied"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["integrated"], -14.20)
        ffmpeg_calls = [c for c in calls if isinstance(c, list)]
        self.assertEqual(len(ffmpeg_calls), 2)
        af1 = ffmpeg_calls[1][ffmpeg_calls[1].index("-af") + 1]
        self.assertIn("loudnorm", af1)
        self.assertIn("linear=false", af1)
        self.assertIn("apad", af1)
        self.assertEqual(ffmpeg_calls[1][ffmpeg_calls[1].index("-t") + 1], "6.006000")

    def test_stops_after_max_attempts_and_reports_unresolved_when_still_outside_tolerance(self):
        # Arrange: 2回補正してもなお外れている場合、3回目は行わない（無限に叩き直さない）。
        # 最終値-14.6は目標±0.5LU(LOUDNESS_TOLERANCE)を外れるため、ok=Falseで報告する。
        stats = {"input_i": "-16.0", "input_tp": "-1.41", "input_lra": "9.50",
                  "input_thresh": "-26.88", "target_offset": "2.0"}
        measurements = iter([
            {"integrated": -16.0, "true_peak": -1.4},
            {"integrated": -15.0, "true_peak": -5.0},
            {"integrated": -14.6, "true_peak": -5.0},
        ])
        calls, originals = self._patch(
            measure_ebur128=lambda path: next(measurements),
            measure_loudnorm=lambda path: stats)
        try:
            # Act
            result = R.fix_loudness("out.mp4")
        finally:
            self._restore(originals)
        # Assert: 2回で打ち切り、それでも範囲外なのでok=False・problemsに理由が入る
        self.assertTrue(result["applied"])
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["problems"]), 1)
        self.assertEqual(result["integrated"], -14.6)
        ffmpeg_calls = [c for c in calls if isinstance(c, list)]
        self.assertEqual(len(ffmpeg_calls), 2)

    def test_reports_unresolved_when_true_peak_still_exceeds_max_acceptable(self):
        # Arrange: Integratedは範囲内に収まったが、True Peakが上限(-1.0)を超えたまま
        # 打ち切られた場合もokはFalseになる。
        stats = {"input_i": "-16.0", "input_tp": "0.0", "input_lra": "9.50",
                  "input_thresh": "-26.88", "target_offset": "2.0"}
        measurements = iter([
            {"integrated": -16.0, "true_peak": -1.4},
            {"integrated": -14.0, "true_peak": -0.5},  # Integratedは目標だがTPが上限超え
        ])
        calls, originals = self._patch(
            measure_ebur128=lambda path: next(measurements),
            measure_loudnorm=lambda path: stats)
        try:
            # Act
            result = R.fix_loudness("out.mp4")
        finally:
            self._restore(originals)
        # Assert
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("True Peak", result["problems"][0])


class RenderClipTest(unittest.TestCase):
    def test_orchestrates_snap_render_timeline_and_loudness_fix(self):
        # Arrange
        calls = {}
        fake_loudness_result = {"applied": False, "ok": True, "problems": [],
                                 "integrated": -14.0, "true_peak": -1.4}

        def fake_render(cuts, base_png, out, flash_after=0, flash_dur=0.35):
            calls["render"] = (cuts, base_png, out, flash_after, flash_dur)

        def fake_fix_loudness(path):
            calls["fix_loudness"] = path
            return fake_loudness_result

        original_render = R.render
        original_fix_loudness = R.fix_loudness
        original_cwd = os.getcwd()
        R.render = fake_render
        R.fix_loudness = fake_fix_loudness
        tmp_dir = tempfile.mkdtemp()
        try:
            os.chdir(tmp_dir)
            os.makedirs("base", exist_ok=True)
            clip = {"file": "a.mp4", "l1": [], "l2": [], "quote": [],
                    "cuts": [(10.0, 16.0), (20.0, 30.0)]}
            base_png = os.path.join("base", "base_01.png")
            # ensure_base_imageがキャッシュ命中で再利用するよう、画像とハッシュの
            # サイドカーを両方前もって置いておく（mkbaseはこのテストでは差し替えない）
            open(base_png, "a").close()
            with open(base_png + ".hash", "w", encoding="utf-8") as f:
                f.write(R.base_image_content_hash(clip))
            os.makedirs("out", exist_ok=True)
            # Act
            out_path, loudness_result = R.render_clip(0, clip, "out")
            # Assert
            self.assertEqual(out_path, os.path.join("out", "a.mp4"))
            self.assertEqual(calls["fix_loudness"], out_path)
            self.assertEqual(loudness_result, fake_loudness_result)
            cuts_arg = calls["render"][0]
            expected_cuts = [
                (R.frame_time(R.snap_frame(10.0)), R.frame_time(R.snap_frame(16.0))),
                (R.frame_time(R.snap_frame(20.0)), R.frame_time(R.snap_frame(30.0))),
            ]
            self.assertEqual(cuts_arg, expected_cuts)
            with open(out_path + ".timeline.json", encoding="utf-8") as f:
                timeline = json.load(f)
            self.assertEqual(timeline["cuts"][0]["n_frames"],
                              R.snap_frame(16.0) - R.snap_frame(10.0))
        finally:
            os.chdir(original_cwd)
            R.render = original_render
            R.fix_loudness = original_fix_loudness
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_raises_when_cut_before_flash_not_longer_than_flash_frames(self):
        # Arrange: 先頭カットが0.05秒(≈1.5フレーム)しか無く、フラッシュ長(≈10フレーム)以下
        original_cwd = os.getcwd()
        tmp_dir = tempfile.mkdtemp()
        try:
            os.chdir(tmp_dir)
            os.makedirs("base", exist_ok=True)
            clip = {"file": "a.mp4", "l1": [], "l2": [], "quote": [],
                    "cuts": [(10.0, 10.05), (20.0, 30.0)]}
            base_png = os.path.join("base", "base_01.png")
            open(base_png, "a").close()
            with open(base_png + ".hash", "w", encoding="utf-8") as f:
                f.write(R.base_image_content_hash(clip))
            os.makedirs("out", exist_ok=True)
            # Act / Assert（render()呼び出し前に検証で落ちるので、ffmpegは呼ばれない）
            with self.assertRaises(ValueError):
                R.render_clip(0, clip, "out")
        finally:
            os.chdir(original_cwd)
            shutil.rmtree(tmp_dir, ignore_errors=True)


class MainTest(unittest.TestCase):
    def test_calls_render_clip_for_each_selected_clip_and_restores_cwd(self):
        # Arrange
        original_render_clip = R.render_clip
        original_cwd = os.getcwd()
        calls = []
        ok_result = {"ok": True, "problems": []}

        def fake_render_clip(idx, clip, out_dir):
            calls.append((idx, clip["file"], out_dir))
            return os.path.join(out_dir, clip["file"]), ok_result

        R.render_clip = fake_render_clip
        tmp_dir = tempfile.mkdtemp()
        try:
            plan_path = os.path.join(tmp_dir, "clips_plan.py")
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write("CLIPS=[{'file': 'a.mp4', 'cuts': [(1.0, 2.0)]},"
                        " {'file': 'b.mp4', 'cuts': [(1.0, 2.0)]}]")
            # Act
            rc = R.main([tmp_dir, plan_path])
            # Assert
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [(0, "a.mp4", "out"), (1, "b.mp4", "out")])
            self.assertTrue(os.path.isdir(os.path.join(tmp_dir, "out")))
            # main()内でworkdirへchdirするが、呼び出し元のcwdへ必ず戻す（I-13）。
            # run.pyが同一プロセスでrender.main()を呼ぶため、戻さないと相対パスの
            # --ocrci等が壊れる。
            self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(original_cwd))
        finally:
            os.chdir(original_cwd)
            R.render_clip = original_render_clip
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_restores_cwd_even_when_render_clip_raises(self):
        # Arrange: 検証エラー(ValueError)で早期returnする経路でもcwdを戻す
        original_render_clip = R.render_clip
        original_cwd = os.getcwd()
        R.render_clip = lambda idx, clip, out_dir: (_ for _ in ()).throw(ValueError("boom"))
        tmp_dir = tempfile.mkdtemp()
        try:
            plan_path = os.path.join(tmp_dir, "clips_plan.py")
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write("CLIPS=[{'file': 'a.mp4', 'cuts': [(1.0, 2.0)]}]")
            # Act
            rc = R.main([tmp_dir, plan_path])
            # Assert
            self.assertEqual(rc, 1)
            self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(original_cwd))
        finally:
            os.chdir(original_cwd)
            R.render_clip = original_render_clip
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_returns_exit_code_2_when_loudness_unresolved(self):
        # Arrange: レンダリング自体は成功するが、ラウドネスが最終的に許容範囲外
        original_render_clip = R.render_clip
        original_cwd = os.getcwd()
        unresolved_result = {"ok": False, "problems": ["Integrated loudness ..."]}
        R.render_clip = lambda idx, clip, out_dir: (
            os.path.join(out_dir, clip["file"]), unresolved_result)
        tmp_dir = tempfile.mkdtemp()
        try:
            plan_path = os.path.join(tmp_dir, "clips_plan.py")
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write("CLIPS=[{'file': 'a.mp4', 'cuts': [(1.0, 2.0)]}]")
            # Act
            rc = R.main([tmp_dir, plan_path])
            # Assert
            self.assertEqual(rc, 2)
        finally:
            os.chdir(original_cwd)
            R.render_clip = original_render_clip
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_only_with_unknown_file_returns_error_without_rendering(self):
        # Arrange
        original_render_clip = R.render_clip
        original_cwd = os.getcwd()
        R.render_clip = lambda idx, clip, out_dir: self.fail("render_clip は呼ばれないはず")
        tmp_dir = tempfile.mkdtemp()
        try:
            plan_path = os.path.join(tmp_dir, "clips_plan.py")
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write("CLIPS=[{'file': 'a.mp4', 'cuts': [(1.0, 2.0)]}]")
            # Act
            rc = R.main([tmp_dir, plan_path, "--only", "missing.mp4"])
            # Assert
            self.assertEqual(rc, 1)
        finally:
            os.chdir(original_cwd)
            R.render_clip = original_render_clip
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
