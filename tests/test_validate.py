"""Tests for sizuru_post.domain.validate: caption/video validation rules."""

import json
import subprocess
import unicodedata
from pathlib import Path

import pytest

from sizuru_post.domain.models import Target, VideoInfo
from sizuru_post.domain.validate import (
    caption_length,
    generate_thumbnail,
    parse_ffprobe,
    probe_video,
    validate_caption,
    validate_video,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestValidateCaptionX:
    def test_full_width_140_chars_is_ok(self) -> None:
        # Arrange: full-width chars weigh 2 each -> 140 * 2 = 280 (limit)
        target = Target(service="x", caption="あ" * 140)

        # Act
        issues = validate_caption(target)

        # Assert
        assert issues == []

    def test_full_width_141_chars_is_error(self) -> None:
        # Arrange: 141 * 2 = 282 > 280
        target = Target(service="x", caption="あ" * 141)

        # Act
        issues = validate_caption(target)

        # Assert
        assert len(issues) == 1
        assert issues[0].service == "x"
        assert issues[0].level == "error"

    def test_url_counts_as_23_regardless_of_length(self) -> None:
        # Arrange: 1 url (weight 23) + 257 ascii chars (weight 1 each) = 280 (limit, OK)
        url = "https://example.com/very/long/path/that/would/otherwise/blow/the/budget"
        caption = url + " " + "a" * 256

        # Act
        issues = validate_caption(Target(service="x", caption=caption))

        # Assert
        assert issues == []

        # Act: one more char pushes it over the limit
        issues_over = validate_caption(Target(service="x", caption=caption + "a"))

        # Assert
        assert len(issues_over) == 1
        assert issues_over[0].level == "error"

    def test_half_width_280_chars_is_ok(self) -> None:
        # Arrange: ASCII chars weigh 1 each -> 280 * 1 = 280 (limit)
        target = Target(service="x", caption="a" * 280)

        # Act
        issues = validate_caption(target)

        # Assert
        assert issues == []

    def test_half_width_281_chars_is_error(self) -> None:
        # Arrange: 281 * 1 = 281 > 280
        target = Target(service="x", caption="a" * 281)

        # Act
        issues = validate_caption(target)

        # Assert
        assert len(issues) == 1
        assert issues[0].level == "error"

    def test_mixed_ascii_and_japanese_at_boundary(self) -> None:
        # Arrange: 130 full-width (weight 2 -> 260) + 20 ascii (weight 1 -> 20) = 280 (limit, OK)
        caption = "あ" * 130 + "a" * 20

        # Act
        issues = validate_caption(Target(service="x", caption=caption))

        # Assert
        assert issues == []

        # Act: one more ascii char pushes it to 281
        issues_over = validate_caption(Target(service="x", caption=caption + "a"))

        # Assert
        assert len(issues_over) == 1
        assert issues_over[0].level == "error"

    def test_emoji_counts_as_weight_2(self) -> None:
        # Arrange: an astral-plane emoji is a single Python code point, weighted
        # like other non-ASCII chars -> 140 * 2 = 280 (limit)
        target = Target(service="x", caption="😀" * 140)

        # Act
        issues = validate_caption(target)

        # Assert
        assert issues == []

        # Act: one more emoji pushes it over the limit
        issues_over = validate_caption(Target(service="x", caption="😀" * 141))

        # Assert
        assert len(issues_over) == 1
        assert issues_over[0].level == "error"

    def test_multiple_urls_each_count_23(self) -> None:
        # Arrange: 2 urls (weight 23 each -> 46) + 2 separator spaces + 232 ascii
        # chars (weight 1 each) = 46 + 234 = 280 (limit, OK)
        url_a = "https://example.com/a"
        url_b = "https://example.org/b/c/d/e/f/g/h"
        caption = f"{url_a} {url_b} " + "a" * 232

        # Act
        issues = validate_caption(Target(service="x", caption=caption))

        # Assert
        assert issues == []

        # Act: one more ascii char pushes it over the limit
        issues_over = validate_caption(Target(service="x", caption=caption + "a"))

        # Assert
        assert len(issues_over) == 1
        assert issues_over[0].level == "error"

    def test_u2010_to_u201f_punctuation_counts_as_weight_1(self) -> None:
        # Arrange: U+2019 (RIGHT SINGLE QUOTATION MARK) is in the 0x2010-0x201F
        # weight-1 range, distinct from the base 0x0000-0x10FF range.
        target = Target(service="x", caption="’" * 280)

        # Act
        issues = validate_caption(target)

        # Assert
        assert issues == []

        # Act: one more char pushes it over the limit
        issues_over = validate_caption(Target(service="x", caption="’" * 281))

        # Assert
        assert len(issues_over) == 1
        assert issues_over[0].level == "error"


class TestValidateCaptionTikTokInstagram:
    @pytest.mark.parametrize("service", ["tiktok", "instagram"])
    def test_caption_within_2200_chars_is_ok(self, service: str) -> None:
        # Act
        issues = validate_caption(Target(service=service, caption="a" * 2200))  # type: ignore[arg-type]

        # Assert
        assert issues == []

    @pytest.mark.parametrize("service", ["tiktok", "instagram"])
    def test_caption_over_2200_chars_is_error(self, service: str) -> None:
        # Act
        issues = validate_caption(Target(service=service, caption="a" * 2201))  # type: ignore[arg-type]

        # Assert
        assert len(issues) == 1
        assert issues[0].level == "error"

    def test_instagram_31_hashtags_is_error(self) -> None:
        # Arrange
        caption = " ".join(f"#tag{i}" for i in range(31))

        # Act
        issues = validate_caption(Target(service="instagram", caption=caption))

        # Assert
        assert any(i.level == "error" and "ハッシュタグ" in i.message for i in issues)

    def test_instagram_30_hashtags_is_ok(self) -> None:
        # Arrange
        caption = " ".join(f"#tag{i}" for i in range(30))

        # Act
        issues = validate_caption(Target(service="instagram", caption=caption))

        # Assert
        assert issues == []

    def test_instagram_full_width_hashtag_is_counted(self) -> None:
        # Arrange
        caption = " ".join(f"＃タグ{i}" for i in range(31))

        # Act
        issues = validate_caption(Target(service="instagram", caption=caption))

        # Assert
        assert any(i.level == "error" and "ハッシュタグ" in i.message for i in issues)

    def test_instagram_30_full_width_hashtags_is_ok(self) -> None:
        # Arrange
        caption = " ".join(f"＃タグ{i}" for i in range(30))

        # Act
        issues = validate_caption(Target(service="instagram", caption=caption))

        # Assert
        assert issues == []

    def test_tiktok_hashtags_are_not_limited(self) -> None:
        # Arrange
        caption = " ".join(f"#tag{i}" for i in range(50))

        # Act
        issues = validate_caption(Target(service="tiktok", caption=caption))

        # Assert
        assert issues == []


class TestValidateVideo:
    def _video(self, **overrides: object) -> VideoInfo:
        defaults: dict[str, object] = {
            "duration_sec": 30.0,
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
        }
        defaults.update(overrides)
        return VideoInfo(**defaults)  # type: ignore[arg-type]

    def test_x_duration_over_140s_is_error(self) -> None:
        # Arrange
        video = self._video(duration_sec=140.1)
        target = Target(service="x", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert any(i.level == "error" and i.service == "x" for i in issues)

    def test_x_duration_at_140s_is_ok(self) -> None:
        # Arrange
        video = self._video(duration_sec=140.0)
        target = Target(service="x", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert issues == []

    def test_x_duration_just_over_140s_is_error(self) -> None:
        # Arrange: 140.01s is only fractionally over the limit
        video = self._video(duration_sec=140.01)
        target = Target(service="x", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert any(i.level == "error" and i.service == "x" for i in issues)

    def test_tiktok_duration_over_180s_is_warning_not_error(self) -> None:
        # Arrange
        video = self._video(duration_sec=181.0)
        target = Target(service="tiktok", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert len(issues) == 1
        assert issues[0].level == "warning"

    def test_x_duration_over_180s_is_only_x_error_not_double_warning(self) -> None:
        # Arrange: only "x" target -> should not also get a >180s warning
        video = self._video(duration_sec=200.0)
        target = Target(service="x", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert len(issues) == 1
        assert issues[0].level == "error"

    def test_non_9_16_aspect_ratio_is_warning(self) -> None:
        # Arrange: 16:9 landscape instead of 9:16
        video = self._video(width=1920, height=1080)
        target = Target(service="tiktok", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert any("縦横比" in i.message and i.level == "warning" for i in issues)

    def test_9_16_within_1_percent_tolerance_is_ok(self) -> None:
        # Arrange: 1080x1920 is exactly 9:16
        video = self._video(width=1080, height=1920)
        target = Target(service="tiktok", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert not any("縦横比" in i.message for i in issues)

    def test_aspect_ratio_at_1_percent_tolerance_boundary_is_ok(self) -> None:
        # Arrange: 909x1600 deviates from 9:16 by ~1.0% (right at the tolerance edge)
        video = self._video(width=909, height=1600)
        target = Target(service="tiktok", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert not any("縦横比" in i.message for i in issues)

    def test_aspect_ratio_just_over_1_percent_tolerance_is_warning(self) -> None:
        # Arrange: 910x1600 deviates from 9:16 by ~1.1%, just past the tolerance edge
        video = self._video(width=910, height=1600)
        target = Target(service="tiktok", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert any("縦横比" in i.message and i.level == "warning" for i in issues)

    def test_non_h264_codec_is_warning(self) -> None:
        # Arrange
        video = self._video(video_codec="hevc")
        target = Target(service="tiktok", caption="hi")

        # Act
        issues = validate_video(video, [target])

        # Assert
        assert any("コーデック" in i.message and i.level == "warning" for i in issues)


class TestParseFfprobe:
    def test_parses_fixture(self) -> None:
        # Arrange
        data = json.loads((FIXTURES / "ffprobe_sample.json").read_text())

        # Act
        info = parse_ffprobe(data)

        # Assert
        assert info.duration_sec == pytest.approx(12.345)
        assert info.width == 1080
        assert info.height == 1920
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"

    def test_missing_video_stream_raises(self) -> None:
        # Arrange
        data = {"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {"duration": "1.0"}}

        # Act / Assert
        with pytest.raises(ValueError, match="動画ストリーム"):
            parse_ffprobe(data)

    def test_missing_duration_raises(self) -> None:
        # Arrange: neither format.duration nor the video stream's duration is present
        data = {
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 100, "height": 100}],
            "format": {},
        }

        # Act / Assert
        with pytest.raises(ValueError, match="長さ"):
            parse_ffprobe(data)

    def test_no_audio_stream_yields_none_audio_codec(self) -> None:
        # Arrange
        data = {
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 100, "height": 100}],
            "format": {"duration": "1.0"},
        }

        # Act
        info = parse_ffprobe(data)

        # Assert
        assert info.audio_codec is None


class TestProbeVideoSubprocessSafety:
    def test_timeout_raises_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        def _raise_timeout(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)

        monkeypatch.setattr(subprocess, "run", _raise_timeout)

        # Act / Assert
        with pytest.raises(ValueError, match="タイムアウト"):
            probe_video(Path("video.mp4"))

    def test_passes_timeout_and_devnull_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        captured: dict[str, object] = {}

        def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.update(kwargs)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        # Act / Assert
        with pytest.raises(ValueError, match="動画ストリーム"):
            probe_video(Path("video.mp4"))
        assert captured["timeout"] == 30
        assert captured["stdin"] == subprocess.DEVNULL


class TestGenerateThumbnailSubprocessSafety:
    def test_timeout_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: thumbnail failures are the caller's (Publisher's) job to
        # treat as non-fatal, so this just needs to not hang forever.
        def _raise_timeout(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60)

        monkeypatch.setattr(subprocess, "run", _raise_timeout)

        # Act / Assert
        with pytest.raises(subprocess.TimeoutExpired):
            generate_thumbnail(Path("video.mp4"), Path("out.jpg"))

    def test_passes_timeout_and_devnull_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        captured: dict[str, object] = {}

        def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.update(kwargs)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        # Act
        generate_thumbnail(Path("video.mp4"), Path("out.jpg"))

        # Assert
        assert captured["timeout"] == 60
        assert captured["stdin"] == subprocess.DEVNULL


class TestCaptionLength:
    def test_x_uses_weighted_length(self) -> None:
        # Act / Assert
        assert caption_length("x", "a" * 10) == 10
        assert caption_length("x", "あ" * 10) == 20

    def test_tiktok_and_instagram_use_plain_length(self) -> None:
        # Act / Assert
        assert caption_length("tiktok", "あ" * 10) == 10
        assert caption_length("instagram", "a" * 10) == 10

    def test_x_family_emoji_zwj_sequence_weighs_2(self) -> None:
        # Arrange: a ZWJ (zero-width joiner) sequence of 4 base emoji is a
        # single displayed grapheme but 7 Unicode code points — X's own
        # counting rules (via twitter-text) weigh the whole sequence as 2,
        # not per-code-point. A hand-rolled per-code-point weigher (the old
        # implementation) would massively overcount this.
        family = "👨‍👩‍👧‍👦"

        # Act / Assert
        assert caption_length("x", family) == 2

    def test_x_decomposed_accented_character_is_normalized_to_nfc_first(self) -> None:
        # Arrange: "é" as NFD (base "e" + combining acute accent, 2 code
        # points) must weigh the same as its NFC form (1 code point, weight
        # 1) — the caller's input may arrive in either normalization form.
        decomposed = unicodedata.normalize("NFD", "é")
        assert len(decomposed) == 2  # sanity: really is 2 code points

        # Act / Assert
        assert caption_length("x", decomposed) == 1
        assert caption_length("x", unicodedata.normalize("NFC", "é")) == 1

    def test_x_url_with_trailing_punctuation_is_not_absorbed_into_the_url(self) -> None:
        # Arrange: "見て" (weight 2 each) + " " (1) + a 23-weight URL + a
        # trailing "." that is NOT part of the URL match (real X/twitter-text
        # URL extraction excludes trailing sentence punctuation) and so is
        # counted separately as its own weight-1 character.
        text = "見て https://example.com/a."

        # Act / Assert
        assert caption_length("x", text) == 29

    def test_tiktok_and_instagram_count_code_points_after_nfc_normalization(self) -> None:
        # Arrange: same contract as X's normalization, just without X's
        # weighting — a decomposed accented character must count as the 1
        # code point it becomes after NFC, not the 2 it was decomposed into.
        decomposed = unicodedata.normalize("NFD", "café")
        assert len(decomposed) == 5  # sanity: c-a-f-e-<combining accent>

        # Act / Assert
        assert caption_length("tiktok", decomposed) == 4
        assert caption_length("instagram", decomposed) == 4


class TestCaptionLengthFixtureContract:
    """Shared cross-language contract with the admin dashboard frontend's
    xWeightedLength.ts — both sides must match every case in this fixture."""

    def test_every_case_matches_caption_length(self) -> None:
        # Arrange
        cases = json.loads((FIXTURES / "caption_length_cases.json").read_text())
        assert len(cases) > 0

        # Act / Assert
        for case in cases:
            actual = caption_length(case["service"], case["text"])
            assert actual == case["expected_length"], case["description"]
