# -*- coding: utf-8 -*-
"""scripts/common.py の単体テスト（第3段階：fps自動化・cut_range一本化）。
render.py/qc.py/chop.py/pauses.py/gen_docs.py の5か所に重複していた
「カット区間の計算(f0-0.5)/fps・尻のgstartスナップ」を common.cut_range に一本化した。
"""
import array
import json
import os
import struct
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import common as C  # noqa: E402


def write_bounds(workdir, bound, gstart, fps=None, fps_str=None):
    d = {"bound": bound, "gstart": gstart}
    if fps is not None:
        d["fps"] = fps
    if fps_str is not None:
        d["fps_str"] = fps_str
    with open(os.path.join(workdir, "bounds.json"), "w") as f:
        json.dump(d, f)


class LoadBoundsTest(unittest.TestCase):
    def test_returns_empty_defaults_when_bounds_file_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            bounds = C.load_bounds(d)
        self.assertEqual(bounds["bound"], [])
        self.assertEqual(bounds["gstart"], {})
        self.assertEqual(bounds["fps_str"], C.DEFAULT_FPS_STR)

    def test_reads_fps_and_fps_str_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            write_bounds(d, [100], {"100": 90}, fps=25.0, fps_str="25/1")
            bounds = C.load_bounds(d)
        self.assertEqual(bounds["fps"], 25.0)
        self.assertEqual(bounds["fps_str"], "25/1")
        self.assertEqual(bounds["gstart"], {100: 90})

    def test_reads_fps_str_without_a_slash(self):
        # Arrange: fps_strが整数表記("25")のみで、fpsキー自体が無い場合
        with tempfile.TemporaryDirectory() as d:
            write_bounds(d, [100], {"100": 90}, fps_str="25")
            bounds = C.load_bounds(d)
        self.assertEqual(bounds["fps"], 25.0)
        self.assertEqual(bounds["fps_str"], "25")

    def test_reads_fps_str_with_a_slash_and_derives_fps_when_fps_key_is_absent(self):
        # Arrange: fpsキーが無くてもfps_strから導出する（"30/1"は30.0fps）
        with tempfile.TemporaryDirectory() as d:
            write_bounds(d, [100], {"100": 90}, fps_str="30/1")
            bounds = C.load_bounds(d)
        self.assertEqual(bounds["fps"], 30.0)
        self.assertEqual(bounds["fps_str"], "30/1")

    def test_raises_on_malformed_bounds_json(self):
        # Arrange: bounds.jsonの中身が壊れたJSON（手編集ミス・書き込み中断等）の場合、
        # load_bounds はそのまま json.JSONDecodeError を送出する（黙って既定値に
        # フォールバックしない。呼び出し側が気づけるようにするための現状の挙動）
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bounds.json"), "w") as f:
                f.write("{not valid json")
            with self.assertRaises(json.JSONDecodeError):
                C.load_bounds(d)

    def test_defaults_to_2997_and_warns_when_fps_str_is_missing(self):
        # Arrange: fps_strが無い古い作業フォルダ（第3段階以前に作られたbounds.json）
        with tempfile.TemporaryDirectory() as d:
            write_bounds(d, [100], {"100": 90}, fps=29.970029970029969)
            captured = {}
            old_stderr = C.sys.stderr

            class Capture:
                def write(self, s):
                    captured["msg"] = captured.get("msg", "") + s

                def flush(self):
                    pass
            C.sys.stderr = Capture()
            try:
                bounds = C.load_bounds(d)
            finally:
                C.sys.stderr = old_stderr
        self.assertEqual(bounds["fps_str"], C.DEFAULT_FPS_STR)
        self.assertIn("fps_str", captured.get("msg", ""))


class FramesTest(unittest.TestCase):
    def test_rounds_to_nearest_frame_at_given_fps(self):
        self.assertEqual(C.frames(1.0, 30.0), 30)

    def test_uses_the_given_fps_not_a_fixed_constant(self):
        # Arrange: 25fpsと29.97fpsで同じ秒数でも違うフレーム番号になることを確認する
        self.assertEqual(C.frames(2.0, 25.0), 50)
        self.assertEqual(C.frames(2.0, 30000.0 / 1001), 60)


class CutRangeTest(unittest.TestCase):
    def test_computes_in_out_without_gstart_snap(self):
        # Arrange: gstartに完了フレームのエントリが無い（尻はそのまま使う）
        bounds = {"fps": 30.0, "gstart": {}}
        cut = {"src": (10.0, 12.0)}
        S, E = C.cut_range(cut, bounds)
        f0, f1 = C.frames(10.0, 30.0), C.frames(12.0, 30.0)
        self.assertAlmostEqual(S, (f0 - 0.5) / 30.0)
        self.assertAlmostEqual(E, (f1 - 0.5) / 30.0)

    def test_snaps_tail_to_gstart_when_present(self):
        # Arrange: 尻のフレームがgstartに載っている（＝次のテロップへの切替開始フレームへ戻す）
        bounds = {"fps": 30.0, "gstart": {360: 355}}
        cut = {"src": (10.0, 12.0)}  # frames(12.0, 30.0) == 360
        S, E = C.cut_range(cut, bounds)
        self.assertAlmostEqual(E, (355 - 0.5) / 30.0)

    def test_works_at_25fps(self):
        bounds = {"fps": 25.0, "gstart": {}}
        cut = {"src": (4.0, 8.0)}
        S, E = C.cut_range(cut, bounds)
        self.assertAlmostEqual(S, (100 - 0.5) / 25.0)
        self.assertAlmostEqual(E, (200 - 0.5) / 25.0)

    def test_works_at_2997fps(self):
        fps = 30000.0 / 1001
        bounds = {"fps": fps, "gstart": {}}
        cut = {"src": (10.0, 12.0)}
        S, E = C.cut_range(cut, bounds)
        f0, f1 = C.frames(10.0, fps), C.frames(12.0, fps)
        self.assertAlmostEqual(S, (f0 - 0.5) / fps)
        self.assertAlmostEqual(E, (f1 - 0.5) / fps)

    def test_works_at_5994fps(self):
        # Arrange: 59.94fps(60000/1001)。29.97fps以外の比の悪いfpsでも同じ式で成り立つこと
        fps = 60000.0 / 1001
        bounds = {"fps": fps, "gstart": {}}
        cut = {"src": (10.0, 12.0)}
        S, E = C.cut_range(cut, bounds)
        f0, f1 = C.frames(10.0, fps), C.frames(12.0, fps)
        self.assertAlmostEqual(S, (f0 - 0.5) / fps)
        self.assertAlmostEqual(E, (f1 - 0.5) / fps)

    def test_gstart_snap_to_the_same_frame_is_a_no_op(self):
        # Arrange: gstartのエントリがf1自身にスナップする（切替が1フレームで完了しており
        # スナップしても値が変わらない）場合、通常のgstart無しと同じ尻の時刻になる
        bounds = {"fps": 30.0, "gstart": {360: 360}}
        cut = {"src": (10.0, 12.0)}  # frames(12.0, 30.0) == 360
        S, E = C.cut_range(cut, bounds)
        self.assertAlmostEqual(E, (360 - 0.5) / 30.0)

    def test_zero_frame_cut_when_start_and_end_land_on_the_same_frame(self):
        # Arrange: 開始・終了が同じフレームに丸まる（0フレームのカット、s==e）。
        # cut_rangeはガードせずそのまま同じ時刻を返す（呼び出し側が0秒カットとして
        # 扱う責任を持つ、という現状の挙動を固定する）
        bounds = {"fps": 30.0, "gstart": {}}
        cut = {"src": (1.0, 1.0)}
        S, E = C.cut_range(cut, bounds)
        self.assertEqual(S, E)


class SnapTailTimeTest(unittest.TestCase):
    def test_returns_plain_frame_time_when_not_in_gstart(self):
        bounds = {"fps": 30.0, "gstart": {}}
        self.assertAlmostEqual(C.snap_tail_time(90, bounds), (90 - 0.5) / 30.0)

    def test_snaps_to_gstart_value_when_present(self):
        bounds = {"fps": 30.0, "gstart": {90: 80}}
        self.assertAlmostEqual(C.snap_tail_time(90, bounds), (80 - 0.5) / 30.0)


class GetAudioStartTimeTest(unittest.TestCase):
    """音声ストリームの遅れは「フォーマット全体のstart_timeとの差」で返すことを確認する
    （Codexレビューで検出した回帰：ストリームの絶対start_timeをそのまま使うと、
    全ストリームが同じだけ後ろにずれている素材で実際には遅れが無いのに
    誤って大きな無音パディングを入れてしまっていた）。
    """

    def setUp(self):
        self._run = C.subprocess.run

    def tearDown(self):
        C.subprocess.run = self._run

    def _mock_probe(self, stream_out, format_out):
        def fake_run(cmd, **kwargs):
            is_format = any("format=start_time" in c for c in cmd)

            class R:
                stdout = format_out if is_format else stream_out
            return R()
        C.subprocess.run = fake_run

    def test_returns_relative_delay_when_only_audio_is_delayed(self):
        # Arrange: 映像は0秒から、音声だけ1.936秒遅れて始まる（フォーマット全体は0秒基準）
        self._mock_probe(stream_out="1.936000\n", format_out="0.000000\n")
        self.assertAlmostEqual(C.get_audio_start_time("src.mp4"), 1.936)

    def test_returns_zero_when_whole_container_shares_the_same_nonzero_baseline(self):
        # Arrange: 全ストリームが同じだけ後ろにずれている（実際の遅れは無い）
        self._mock_probe(stream_out="4.936000\n", format_out="4.936000\n")
        self.assertEqual(C.get_audio_start_time("src.mp4"), 0.0)

    def test_defaults_to_zero_when_not_available(self):
        self._mock_probe(stream_out="N/A\n", format_out="N/A\n")
        self.assertEqual(C.get_audio_start_time("src.mp4"), 0.0)

    def test_defaults_to_zero_on_empty_output(self):
        self._mock_probe(stream_out="", format_out="")
        self.assertEqual(C.get_audio_start_time("src.mp4"), 0.0)


class LoadPcmAndWindowDbTest(unittest.TestCase):
    def setUp(self):
        self._run = C.subprocess.run

    def tearDown(self):
        C.subprocess.run = self._run

    def test_load_pcm_returns_empty_tuple_on_empty_output(self):
        class R:
            returncode = 0
            stdout = b""
        C.subprocess.run = lambda *a, **k: R()
        self.assertEqual(list(C.load_pcm("src.mp4")), [])

    def test_load_pcm_returns_a_compact_array_not_a_tuple_of_python_ints(self):
        # Arrange: 30分の音声をPythonのintのタプルで持つと約1GBになる（Codexレビューで
        # 検出）。array('h')は16bit整数を2バイトのまま詰めて持つのでメモリ効率が良い
        class R:
            returncode = 0
            stdout = struct.pack("<3h", 100, -200, 300)
        C.subprocess.run = lambda *a, **k: R()
        samples = C.load_pcm("src.mp4")
        self.assertIsInstance(samples, array.array)
        self.assertEqual(samples.typecode, "h")
        self.assertEqual(list(samples), [100, -200, 300])

    def test_load_pcm_explicitly_maps_the_same_stream_probed_for_start_time(self):
        # Arrange: 複数音声トラックがある素材で、get_audio_start_timeが見ているa:0とは
        # 別のトラックをffmpegが自動選択すると、パディング量と実デコード内容がずれる
        # （Codexレビューで検出）。デコード側も明示的に同じストリームをmapする
        captured = {}

        def fake_run(cmd, **kwargs):
            if any("start_time" in c for c in cmd):
                class R:
                    stdout = "0.000000"
                return R()
            captured["cmd"] = cmd

            class R2:
                returncode = 0
                stdout = b""
            return R2()
        C.subprocess.run = fake_run
        C.load_pcm("src.mp4")
        self.assertIn("-map", captured["cmd"])
        self.assertEqual(captured["cmd"][captured["cmd"].index("-map") + 1], "0:a:0")

    def test_load_pcm_pads_leading_silence_when_audio_is_relatively_delayed(self):
        # Arrange: 音声トラックだけがフォーマット全体の基準時刻より遅れて始まる素材
        # （edit list等）。パディング無しだと、window_dbが配列インデックス=0を時刻0として
        # 扱ってしまい、実際は無音の音声開始前の時刻を「音声あり」と誤判定する
        # （Codexレビューで検出）
        def fake_run(cmd, **kwargs):
            if any("format=start_time" in c for c in cmd):
                class RF:
                    stdout = "0.000000"
                return RF()
            if any("stream=start_time" in c for c in cmd):
                class RS:
                    stdout = "1.5"
                return RS()

            class R2:
                returncode = 0
                stdout = struct.pack("<3h", 100, 200, 300)
            return R2()
        C.subprocess.run = fake_run
        samples = C.load_pcm("src.mp4", rate=10)
        # 1.5秒*10Hz=15サンプル分の無音が前に付き、そのあとに実際の3サンプルが続く
        self.assertEqual(list(samples), [0] * 15 + [100, 200, 300])

    def test_load_pcm_does_not_pad_when_audio_starts_at_zero(self):
        def fake_run(cmd, **kwargs):
            if any("start_time" in c for c in cmd):
                class R:
                    stdout = "0.000000"
                return R()

            class R2:
                returncode = 0
                stdout = struct.pack("<2h", 1, 2)
            return R2()
        C.subprocess.run = fake_run
        samples = C.load_pcm("src.mp4", rate=10)
        self.assertEqual(list(samples), [1, 2])

    def test_load_pcm_does_not_pad_when_whole_container_shares_nonzero_baseline(self):
        # Arrange: 全ストリームが同じだけ後ろにずれている素材では、音声に実際の遅れは
        # 無いのでパディングしない（Codexレビューで検出した回帰の再現）
        def fake_run(cmd, **kwargs):
            if any("start_time" in c for c in cmd):
                class R:
                    stdout = "4.936000"
                return R()

            class R2:
                returncode = 0
                stdout = struct.pack("<2h", 1, 2)
            return R2()
        C.subprocess.run = fake_run
        samples = C.load_pcm("src.mp4", rate=10)
        self.assertEqual(list(samples), [1, 2])

    def test_load_pcm_drops_a_trailing_odd_byte_without_raising(self):
        # Arrange: ffmpegのraw s16le出力が奇数バイト長になることは無い想定だが、
        # array('h').frombytesは奇数バイトのままだと例外を出すため、n=len(d)//2で
        # 偶数長に切り詰めてから渡している。末尾の半端な1バイトは黙って捨てられる
        class R:
            returncode = 0
            stdout = struct.pack("<2h", 100, 200) + b"\x01"
        C.subprocess.run = lambda *a, **k: R()
        samples = C.load_pcm("src.mp4")
        self.assertEqual(list(samples), [100, 200])

    def test_load_pcm_raises_when_ffmpeg_fails(self):
        # Arrange: srcが無い・音声ストリームが無い等でffmpegが失敗した場合、空のPCMを
        # 黙って返さない（無音と混同してcutpoints.py等が誤判定するのを防ぐため）
        class R:
            returncode = 1
            stdout = b""
            stderr = b"No such file or directory"
        C.subprocess.run = lambda *a, **k: R()
        with self.assertRaises(RuntimeError):
            C.load_pcm("missing.mp4")

    def test_window_db_of_silence_is_very_low(self):
        samples = (0,) * 1600  # 100ms of digital silence at 16kHz
        db = C.window_db(samples, 16000, 0.0, 0.06)
        self.assertLess(db, -80)

    def test_window_db_of_full_scale_tone_is_near_zero(self):
        samples = (32767, -32768) * 800
        db = C.window_db(samples, 16000, 0.0, 0.06)
        self.assertGreater(db, -1.0)

    def test_window_db_out_of_range_returns_very_low(self):
        samples = (100,) * 100
        db = C.window_db(samples, 16000, 10.0, 0.06)
        self.assertEqual(db, -99.0)


class HasSilenceGapTest(unittest.TestCase):
    """chop.pyと同じ絶対レベル判定（両側とも-26dBFS超なら谷なし＝ぶつ切りの疑い）。"""

    def _samples(self, loud_before, loud_after, rate=16000, t=1.0):
        # t秒を境に前後で振幅を変えた16kHzのPCM列を作る
        n = int(2.0 * rate)
        before_amp = 20000 if loud_before else 0
        after_amp = 20000 if loud_after else 0
        samples = []
        for i in range(n):
            tt = i / float(rate)
            amp = before_amp if tt < t else after_amp
            samples.append(amp if i % 2 == 0 else -amp)
        return tuple(samples)

    def test_both_sides_loud_has_no_silence_gap(self):
        samples = self._samples(True, True)
        self.assertFalse(C.has_silence_gap(samples, 16000, 1.0))

    def test_one_side_quiet_has_silence_gap(self):
        samples = self._samples(True, False)
        self.assertTrue(C.has_silence_gap(samples, 16000, 1.0))

    def test_both_sides_quiet_has_silence_gap(self):
        samples = self._samples(False, False)
        self.assertTrue(C.has_silence_gap(samples, 16000, 1.0))


class SilenceLevelsTest(unittest.TestCase):
    """has_silence_gapが内部で判定に使う実測dB値を、cutpoints.pyの候補表表示にも
    そのまま使えるように公開する（Codexレビューで検出：以前は谷の有無から固定長の
    無音区間を捏造して表示していたため、実測値だけを共有できるようにした）。
    """

    def test_returns_before_and_after_levels(self):
        samples = (0,) * 3200  # 100msの無音x2
        before, after = C.silence_levels(samples, 16000, 0.1)
        self.assertLess(before, -80)
        self.assertLess(after, -80)


class IsSilentPointTest(unittest.TestCase):
    """align_check.pyのis_silent_at用：has_silence_gap（前後どちらかが静かならOKという
    緩い判定）は、発話がカット点そのものにかかっている場合でも「前が静か」なら
    無音扱いにしてしまい、本物の頭切れ・尻切れを見逃す（team-leadの実測）。
    is_silent_pointはカット点を中心にした±15msの窓だけを見る、より厳密な判定。
    """

    RATE = 16000

    def _speech_starts_at(self, onset, rate=RATE, total=2.0):
        # onset秒より前は無音(0)、以降は発話（振幅20000の方形波）にしたPCM列を作る
        n = int(total * rate)
        samples = []
        for i in range(n):
            tt = i / float(rate)
            amp = 20000 if tt >= onset else 0
            samples.append(amp if i % 2 == 0 else -amp)
        return tuple(samples)

    def test_cut_well_before_speech_onset_is_silent(self):
        # team-leadの実測例1：カット5.355秒・発話は5.384秒から
        samples = self._speech_starts_at(5.384, total=6.0)
        self.assertTrue(C.is_silent_point(samples, self.RATE, 5.355))

    def test_another_cut_well_before_speech_onset_is_silent(self):
        # team-leadの実測例2：カット0.984秒・発話は1.107秒から
        samples = self._speech_starts_at(1.107, total=2.0)
        self.assertTrue(C.is_silent_point(samples, self.RATE, 0.984))

    def test_cut_landing_on_speech_onset_is_not_silent(self):
        # Arrange: 発話が0.99秒から始まり、カット点が1.00秒（=本物の頭切れ）の例。
        # has_silence_gapの緩い判定だと前側の窓が静かなのでOKになってしまうが、
        # is_silent_pointはカット点±15msの窓自体に発話がかかっているのでNGにする
        samples = self._speech_starts_at(0.99, total=2.0)
        self.assertFalse(C.is_silent_point(samples, self.RATE, 1.00))

    def test_cut_well_after_speech_onset_is_not_silent(self):
        samples = self._speech_starts_at(0.0, total=2.0)
        self.assertFalse(C.is_silent_point(samples, self.RATE, 1.0))

    def test_cut_point_at_time_zero_is_silent_when_audio_starts_silent(self):
        # Arrange: カット点が音声の先頭0秒（窓の前半[-0.015,0)は負の時刻になり
        # window_dbの範囲外クランプでその分は無視される。残りの前半窓が無音なら無音扱い）
        samples = self._speech_starts_at(5.0, total=6.0)
        self.assertTrue(C.is_silent_point(samples, self.RATE, 0.0))

    def test_cut_point_beyond_the_end_of_samples_is_treated_as_silent(self):
        # Arrange: カット点が音声の末尾を超える位置（窓が丸ごとはみ出す）。
        # window_dbは範囲外を-99.0（無音）とみなすため、is_silent_pointもTrueになる
        # （データが無い＝無音、という既定のフォールバック。実際に無音かは未検証）
        samples = self._speech_starts_at(0.0, total=1.0)
        self.assertTrue(C.is_silent_point(samples, self.RATE, 1000.0))

    def test_empty_samples_are_treated_as_silent(self):
        # Arrange: samplesが空（load_pcmが失敗せず空配列を返すことは無い想定だが、
        # is_silent_point自体は単独でも呼べるため、空配列でも例外を出さないことを確認する）
        self.assertTrue(C.is_silent_point((), self.RATE, 0.0))
        self.assertTrue(C.is_silent_point((), self.RATE, 1.0))


if __name__ == "__main__":
    unittest.main()
