# -*- coding: utf-8 -*-
"""scripts/align_check.py（対談）の単体テスト（第3段階・対談スキルへの展開）。
判定ロジック自体（match_head/check_head等）はkirinuki_core.speechに共通化されており、
mitchaku-kirinukiの既存テストがimport経由で担保している。ここでは対談固有の部分
（telops.txtの読み取り、半フレーム先での期待テロップ引き、common.cut_rangeの利用、
clips.pyのcuts[{"src"}]読み込み、--only、フラッシュ境界の検査）を検証する。
Whisper CLI・ffmpegを実際に呼ぶ統合テストは含まない。
"""
import os
import sys
import tempfile
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
import align_check as A  # noqa: E402


def W(start, end, word):
    return {"start": start, "end": end, "word": word}


def write_telops(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for i, (s, e, text) in enumerate(lines):
            f.write("%3d %8.3f-%8.3f (%4.1fs) %s\n" % (i, s, e, e - s, text))


class LoadTelopsTest(unittest.TestCase):
    def test_parses_subtext_format(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "telops.txt")
            write_telops(path, [(0.0, 2.0, "結果が出るみたいな人って／絶対いるんですよ")])
            rows = A.load_telops(path)
        self.assertEqual(rows, [(0.0, 2.0, "結果が出るみたいな人って／絶対いるんですよ")])

    def test_sorts_by_start_time(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "telops.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("  1   10.000-  11.000 ( 1.0s) あとから\n")
                f.write("  0    1.000-   2.000 ( 1.0s) さきに\n")
            rows = A.load_telops(path)
        self.assertEqual([r[2] for r in rows], ["さきに", "あとから"])


class TelopAtTest(unittest.TestCase):
    def test_prefers_new_telop_exactly_at_shared_boundary(self):
        rows = [(0.0, 10.0, "旧テロップ"), (10.0, 20.0, "新テロップ")]
        self.assertEqual(A.telop_at(rows, 10.0), "新テロップ")

    def test_no_row_covers_time_returns_none(self):
        rows = [(0.0, 10.0, "本文")]
        self.assertIsNone(A.telop_at(rows, 900.0))

    def test_last_row_end_is_inclusive(self):
        rows = [(0.0, 10.0, "本文")]
        self.assertEqual(A.telop_at(rows, 10.0), "本文")

    def test_matches_new_row_despite_serialized_rounding(self):
        # Arrange: Codexレビューで検出した回帰。frame101@30fpsの境界は101/30=3.3666...7だが
        # subtext.pyはtelops.txtに"%.3f"で丸めて"3.367"と書く。半フレーム先の生の浮動小数
        # (丸めていない3.3666...7)のままだと、丸め後の新テロップ行のstart(3.367)を
        # わずかに下回り、1つ前の行(end=3.367)に誤って一致してしまっていた
        fps = 30.0
        boundary = 101 / fps
        rows = [(0.0, float("%.3f" % boundary), "旧テロップ"),
                (float("%.3f" % boundary), 999.0, "新テロップ")]
        self.assertEqual(A.telop_at(rows, boundary), "新テロップ")


class MakeIsSilentAtTest(unittest.TestCase):
    """align_check.pyのis_silent_atはcommon.is_silent_point（カット点を中心にした
    ±15msの窓自体を見る、より厳密な判定）を使う。common.has_silence_gap（前後どちらか
    が静かならOKという緩い判定）は、発話がカット点そのものにかかっている場合を
    見逃すため使わない（team-leadの実測、2026-09-17）。
    """

    RATE = 16000

    def _speech_starts_at(self, onset, total=6.0):
        n = int(total * self.RATE)
        samples = []
        for i in range(n):
            tt = i / float(self.RATE)
            amp = 20000 if tt >= onset else 0
            samples.append(amp if i % 2 == 0 else -amp)
        return tuple(samples)

    def test_cut_well_before_speech_onset_is_silent(self):
        # team-leadの実測例：カット5.355秒・発話は5.384秒から
        samples = self._speech_starts_at(5.384)
        is_silent_at = A.make_is_silent_at(samples)
        self.assertTrue(is_silent_at(5.355))

    def test_cut_landing_on_speech_onset_is_not_silent(self):
        # Arrange: 発話が0.99秒から始まりカット点が1.00秒（=本物の頭切れ）。
        # has_silence_gapの緩い判定だと前側の窓が静かなのでOKになってしまう
        samples = self._speech_starts_at(0.99, total=2.0)
        is_silent_at = A.make_is_silent_at(samples)
        self.assertFalse(is_silent_at(1.00))


class HeadQueryTimeTest(unittest.TestCase):
    def test_matches_the_telops_file_rounding_at_2997fps(self):
        # Arrange: Codexレビューで検出した回帰。frame135@30000/1001fpsでは、
        # cut_start(=(f0-0.5)/fps)に0.5/fpsを足し戻す多段の浮動小数演算が
        # "4.504"に丸まるが、subtext.pyがtelops.txtに書く135/fpsは"4.505"に丸まる。
        # 整数フレームを復元して1回の除算でやり直すと一致する
        fps = 30000.0 / 1001
        f0 = 135
        cut_start = (f0 - 0.5) / fps
        naive = cut_start + 0.5 / fps
        fixed = A.head_query_time(cut_start, fps)
        self.assertNotEqual("%.3f" % naive, "%.3f" % (f0 / fps))
        self.assertEqual("%.3f" % fixed, "%.3f" % (f0 / fps))

    def test_recovers_the_exact_frame_at_a_plain_fps(self):
        fps = 30.0
        f0 = 100
        cut_start = (f0 - 0.5) / fps
        self.assertAlmostEqual(A.head_query_time(cut_start, fps), f0 / fps)


class CheckCutTest(unittest.TestCase):
    def setUp(self):
        self._loader = A.load_words_for_boundary

    def tearDown(self):
        A.load_words_for_boundary = self._loader

    def test_expected_head_telop_is_looked_up_half_a_frame_after_cut_start(self):
        # Arrange: cut_startはrender.pyの規約通り(f0-0.5)/fps＝境界のちょうど半フレーム手前。
        # telops.txtの境界はf0/fpsぴったり（実ファイルと同じくsubtext.pyの"%.3f"丸めを通す）。
        # 半フレーム先で引かないと、前の（間違った）テロップと比較してしまい、
        # 実際は新テロップ通りに話しているのに誤ってNGになる
        fps = 30.0
        boundary_time = float("%.3f" % (100 / fps))
        cut_start = (100 - 0.5) / fps
        telop_rows = [(0.0, boundary_time, "旧テロップ"), (boundary_time, 999.0, "新しい発話です")]

        def fake_loader(src, boundary_time_, radius, cache_dir, wav_name, model=None, whisper_bin=None):
            if "_head_" in wav_name:
                return [W(cut_start, cut_start + 0.3, "新しい"), W(cut_start + 0.3, cut_start + 0.6, "発話です")]
            return []
        A.load_words_for_boundary = fake_loader
        # Act
        rows = A.check_cut("src.mp4", telop_rows, cut_start, cut_start + 5.0, "cache", "s1_cut00", fps)
        # Assert
        head_row = next(r for r in rows if r[2] == "head")
        self.assertFalse(head_row[0], head_row[4])  # NGでない(=head_row[0]がFalse)こと

    def test_skip_head_omits_head_row_and_does_not_call_whisper_for_it(self):
        calls = []

        def fake_loader(src, boundary_time, radius, cache_dir, wav_name, model=None, whisper_bin=None):
            calls.append(wav_name)
            return []
        A.load_words_for_boundary = fake_loader
        rows = A.check_cut("src.mp4", [(0.0, 999.0, "本文")], 1.0, 2.0, "cache", "s1_cut01",
                            30.0, skip_head=True)
        labels = [r[2] for r in rows]
        self.assertNotIn("head", labels)
        self.assertIn("tail", labels)
        self.assertTrue(all("_head_" not in name for name in calls))

    def test_skip_tail_omits_tail_and_gap_rows_and_does_not_call_whisper_for_it(self):
        calls = []

        def fake_loader(src, boundary_time, radius, cache_dir, wav_name, model=None, whisper_bin=None):
            calls.append(wav_name)
            return []
        A.load_words_for_boundary = fake_loader
        rows = A.check_cut("src.mp4", [(0.0, 999.0, "本文")], 1.0, 2.0, "cache", "s1_cut00",
                            30.0, skip_tail=True)
        labels = [r[2] for r in rows]
        self.assertNotIn("tail", labels)
        self.assertNotIn("gap", labels)
        self.assertTrue(all("_tail_" not in name for name in calls))

    def test_reports_ng_when_head_word_overlaps_and_ok_when_tail_clean(self):
        fps = 30.0

        def fake_loader(src, boundary_time, radius, cache_dir, wav_name, model=None, whisper_bin=None):
            if "_head_" in wav_name:
                return [W(810.5, 811.5, "結果")]
            return [W(818.0, 818.9, "から")]
        A.load_words_for_boundary = fake_loader
        rows = A.check_cut("src.mp4", [(811.177, 813.079, "結果が出るみたいな人って")],
                            811.20, 819.05, "cache", "s1_cut00", fps)
        head_row = next(r for r in rows if r[2] == "head")
        tail_row = next(r for r in rows if r[2] == "tail")
        self.assertTrue(head_row[0])
        self.assertFalse(tail_row[0])

    def test_is_silent_at_is_passed_through_and_suppresses_word_straddle_ng(self):
        # Arrange: team-leadの実測例（Whisperの時刻ジッタ）を再現。is_silent_atを渡すと
        # 頭切れ・尻切れの誤NGが無音のためのものとして扱われOKになる
        fps = 30000.0 / 1001

        def fake_loader(src, boundary_time, radius, cache_dir, wav_name, model=None, whisper_bin=None):
            if "_head_" in wav_name:
                return [W(0.84, 1.30, "結果"), W(1.30, 1.6, "が")]
            return [W(7.97, 8.89, "そう")]
        A.load_words_for_boundary = fake_loader
        rows = A.check_cut("src.mp4", [(0.0, 999.0, "結果が出るみたいな人って")],
                            0.984, 8.692, "cache", "s1_cut00", fps,
                            is_silent_at=lambda t: True)
        head_row = next(r for r in rows if r[2] == "head")
        tail_row = next(r for r in rows if r[2] == "tail")
        self.assertFalse(head_row[0])  # NGでない
        self.assertFalse(tail_row[0])  # NGでない
        self.assertIn("無音", head_row[4])
        self.assertIn("無音", tail_row[4])


class MainTest(unittest.TestCase):
    """clips.pyのcuts=[{"src":(s,e)}]読み込み、common.cut_range利用、--only、
    C1尻・C2頭（フラッシュ境界）も検査対象になることを確認する。
    """

    def setUp(self):
        self._loader = A.load_words_for_boundary
        self._load_pcm = A.C.load_pcm
        self.calls = []

        def fake_loader(src, boundary_time, radius, cache_dir, wav_name, model=None, whisper_bin=None):
            self.calls.append((wav_name, boundary_time))
            return []  # 語なし＝頭尻またぎ無し。頭は「telops.txtの行が無い」でNGになる
        A.load_words_for_boundary = fake_loader
        # 実際のffmpegを呼ばないよう空のPCMにする（is_silent_atは常にTrueになるが、
        # 既存のNGは語のまたぎではなく「本文比較」由来なので無音扱いでも変わらない）
        A.C.load_pcm = lambda src, rate=16000: ()

    def tearDown(self):
        A.load_words_for_boundary = self._loader
        A.C.load_pcm = self._load_pcm

    def _make_workdir(self, cuts_body, xfade=0.0):
        workdir = tempfile.mkdtemp()
        with open(os.path.join(workdir, "bounds.json"), "w") as f:
            f.write('{"fps": 30.0, "fps_str": "30/1", "bound": [], "gstart": {}}')
        with open(os.path.join(workdir, "telops.txt"), "w", encoding="utf-8") as f:
            f.write("  0    0.000- 999.000 (999.0s) 本文\n")
        clips_path = os.path.join(workdir, "clips.py")
        with open(clips_path, "w", encoding="utf-8") as f:
            f.write("XFADE = %r\n" % xfade)
            f.write("CLIPS = %r\n" % cuts_body)
        return workdir, clips_path

    def test_reads_cuts_with_src_key_from_clips_py(self):
        cuts_body = [{"slug": "s1", "cuts": [{"src": (1.0, 2.0)}]}]
        workdir, clips_path = self._make_workdir(cuts_body)
        A.main(["src.mp4", workdir, clips_path])
        # cuts[0]["src"]がcommon.cut_rangeに渡り、head/tailそれぞれ1回ずつWhisperを呼ぶ
        self.assertEqual(len(self.calls), 2)

    def test_only_filters_to_selected_slug(self):
        cuts_body = [{"slug": "s1", "cuts": [{"src": (1.0, 2.0)}]},
                     {"slug": "s2", "cuts": [{"src": (5.0, 6.0)}]}]
        workdir, clips_path = self._make_workdir(cuts_body)
        A.main(["src.mp4", workdir, clips_path, "--only", "s2"])
        wav_names = [c[0] for c in self.calls]
        self.assertTrue(all("s2_" in name for name in wav_names))

    def test_flash_boundary_c1_tail_and_c2_head_are_both_checked(self):
        # Arrange: xfade>0のクリップ。C1(cuts[0])の尻・C2(cuts[1])の頭も
        # qc.pyのOCR検査と違い、スキップせずに検査対象へ含める
        cuts_body = [{"slug": "s1", "xfade": 0.35,
                     "cuts": [{"src": (1.0, 2.0)}, {"src": (5.0, 6.0)}]}]
        workdir, clips_path = self._make_workdir(cuts_body, xfade=0.35)
        A.main(["src.mp4", workdir, clips_path])
        wav_names = [c[0] for c in self.calls]
        self.assertTrue(any("cut00_tail" in name for name in wav_names))
        self.assertTrue(any("cut01_head" in name for name in wav_names))

    def test_contiguous_cuts_skip_the_internal_boundary_check(self):
        # Arrange: 2カット目の頭が1カット目の尻とちょうど同じ時刻（地続き、実際には
        # 切っていない）。地続きの境界で同じ発話がまたがるのは正常であり、
        # chop.check_clipと同じ規約で頭切れ・尻切れとして誤検出しない
        # （Codexレビューで検出：地続きの境界を独立に検査して誤ってNGにしていた）
        cuts_body = [{"slug": "s1", "cuts": [{"src": (1.0, 2.0)}, {"src": (2.0, 3.0)}]}]
        workdir, clips_path = self._make_workdir(cuts_body)
        A.main(["src.mp4", workdir, clips_path])
        wav_names = [c[0] for c in self.calls]
        self.assertFalse(any("cut00_tail" in name for name in wav_names))
        self.assertFalse(any("cut01_head" in name for name in wav_names))
        # 外側の境界（1カット目の頭・2カット目の尻）は引き続き検査する
        self.assertTrue(any("cut00_head" in name for name in wav_names))
        self.assertTrue(any("cut01_tail" in name for name in wav_names))

    def test_flash_boundary_is_checked_even_if_source_times_happen_to_be_contiguous(self):
        # Arrange: C1(cuts[0])とC2(cuts[1])が源の時刻ではたまたま地続きだが、
        # xfade>0なのでrender.pyは実際にacrossfadeで音声を混ぜ合わせる。
        # 地続き判定だけでスキップすると、フラッシュで壊れうる境界を見逃す
        # （Codexレビューで検出した回帰）
        cuts_body = [{"slug": "s1", "xfade": 0.35,
                     "cuts": [{"src": (1.0, 2.0)}, {"src": (2.0, 3.0)}]}]
        workdir, clips_path = self._make_workdir(cuts_body, xfade=0.35)
        A.main(["src.mp4", workdir, clips_path])
        wav_names = [c[0] for c in self.calls]
        self.assertTrue(any("cut00_tail" in name for name in wav_names))
        self.assertTrue(any("cut01_head" in name for name in wav_names))

    def test_returns_nonzero_when_any_cut_is_ng(self):
        cuts_body = [{"slug": "s1", "cuts": [{"src": (1.0, 2.0)}]}]
        workdir, clips_path = self._make_workdir(cuts_body)
        rc = A.main(["src.mp4", workdir, clips_path])
        self.assertEqual(rc, 1)

    def test_only_with_a_slug_not_present_in_clips_checks_nothing_and_exits_ok(self):
        # Arrange: --onlyに指定したスラッグがclips.pyのどのCLIPSにも無い。
        # whichに一致するclipが1つも無いのでループが1回も走らず、Whisperを呼ばずに
        # 終了コード0で終わる（「見つからない」という警告は出ない。現状の挙動を固定する）
        cuts_body = [{"slug": "s1", "cuts": [{"src": (1.0, 2.0)}]}]
        workdir, clips_path = self._make_workdir(cuts_body)
        rc = A.main(["src.mp4", workdir, clips_path, "--only", "does-not-exist"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [])

    def test_clip_with_empty_cuts_list_is_checked_without_error(self):
        # Arrange: clips.pyのCLIPSにcuts=[]のクリップがある（誤ってカットを1つも
        # 定義していない等）。内側のループが0回になるだけで例外にはならず、
        # 全体としては（他にNGが無ければ）終了コード0のまま
        cuts_body = [{"slug": "s1", "cuts": []}]
        workdir, clips_path = self._make_workdir(cuts_body)
        rc = A.main(["src.mp4", workdir, clips_path])
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
