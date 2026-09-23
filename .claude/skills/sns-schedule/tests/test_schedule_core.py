"""Tests for schedule_core: pure slot proposal, ffprobe parsing, and local
plan validation. No filesystem/subprocess/network involved."""

import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from schedule_core import (  # noqa: E402
    JST,
    LocalIssue,
    SlotSearchExhausted,
    approx_x_weighted_length,
    is_fully_settled,
    is_youtube_url,
    merge_gallery_data,
    needs_transcode,
    normalize_youtube_url,
    parse_ffprobe,
    parse_hhmm,
    plan_has_error,
    propose_slots,
    resolve_day_times,
    resolve_main_video_url,
    submit_has_problems,
    summarize_submit_results,
    validate_clip_locally,
    validate_main_video_url_locally,
)


class TestProposeSlots:
    def test_proposes_one_slot_per_day_at_given_time(self) -> None:
        # Arrange: no existing posts, single time per day
        now = datetime(2026, 9, 1, 0, 0, tzinfo=JST)
        start = date(2026, 9, 10)

        # Act
        slots = propose_slots([], start, [time(19, 0)], per_day=1, count=3, now=now)

        # Assert
        assert slots == [
            datetime(2026, 9, 10, 19, 0, tzinfo=JST),
            datetime(2026, 9, 11, 19, 0, tzinfo=JST),
            datetime(2026, 9, 12, 19, 0, tzinfo=JST),
        ]

    def test_uses_up_to_per_day_times_from_the_times_list(self) -> None:
        # Arrange: two times/day requested, per_day=2
        now = datetime(2026, 9, 1, 0, 0, tzinfo=JST)
        start = date(2026, 9, 10)

        # Act
        slots = propose_slots(
            [], start, [time(12, 0), time(19, 0)], per_day=2, count=4, now=now
        )

        # Assert: day 1 gets both times before moving to day 2
        assert slots == [
            datetime(2026, 9, 10, 12, 0, tzinfo=JST),
            datetime(2026, 9, 10, 19, 0, tzinfo=JST),
            datetime(2026, 9, 11, 12, 0, tzinfo=JST),
            datetime(2026, 9, 11, 19, 0, tzinfo=JST),
        ]

    def test_per_day_caps_the_times_list(self) -> None:
        # Arrange: 3 candidate times but per_day=1 -> only the first is used
        now = datetime(2026, 9, 1, 0, 0, tzinfo=JST)
        start = date(2026, 9, 10)

        # Act
        slots = propose_slots(
            [], start, [time(9, 0), time(12, 0), time(19, 0)], per_day=1, count=2, now=now
        )

        # Assert
        assert slots == [
            datetime(2026, 9, 10, 9, 0, tzinfo=JST),
            datetime(2026, 9, 11, 9, 0, tzinfo=JST),
        ]

    def test_skips_a_day_whose_slot_collides_with_an_existing_post(self) -> None:
        # Arrange: 2026-09-10 19:00 is already taken (within 30min window)
        now = datetime(2026, 9, 1, 0, 0, tzinfo=JST)
        start = date(2026, 9, 10)
        existing = [datetime(2026, 9, 10, 19, 20, tzinfo=JST)]

        # Act
        slots = propose_slots(existing, start, [time(19, 0)], per_day=1, count=1, now=now)

        # Assert: 9/10 skipped (20min from existing, within 30min window), 9/11 used
        assert slots == [datetime(2026, 9, 11, 19, 0, tzinfo=JST)]

    def test_slot_exactly_31_minutes_from_existing_is_not_a_collision(self) -> None:
        # Arrange
        now = datetime(2026, 9, 1, 0, 0, tzinfo=JST)
        start = date(2026, 9, 10)
        existing = [datetime(2026, 9, 10, 19, 31, tzinfo=JST)]

        # Act
        slots = propose_slots(existing, start, [time(19, 0)], per_day=1, count=1, now=now)

        # Assert: 31 minutes is outside the +-30min window, so 9/10 is usable
        assert slots == [datetime(2026, 9, 10, 19, 0, tzinfo=JST)]

    def test_newly_proposed_slots_also_avoid_colliding_with_each_other(self) -> None:
        # Arrange: two candidate times 20 minutes apart on the same day
        now = datetime(2026, 9, 1, 0, 0, tzinfo=JST)
        start = date(2026, 9, 10)

        # Act
        slots = propose_slots(
            [], start, [time(19, 0), time(19, 20)], per_day=2, count=2, now=now
        )

        # Assert: the second candidate collides with the first, so it's
        # skipped and the next day's first slot is used instead
        assert slots == [
            datetime(2026, 9, 10, 19, 0, tzinfo=JST),
            datetime(2026, 9, 11, 19, 0, tzinfo=JST),
        ]

    def test_skips_slots_less_than_15_minutes_from_now(self) -> None:
        # Arrange: "now" is 2026-09-10 18:50 -> the 19:00 slot is only 10min away
        now = datetime(2026, 9, 10, 18, 50, tzinfo=JST)
        start = date(2026, 9, 10)

        # Act
        slots = propose_slots([], start, [time(19, 0)], per_day=1, count=1, now=now)

        # Assert
        assert slots == [datetime(2026, 9, 11, 19, 0, tzinfo=JST)]

    def test_count_zero_returns_empty_list(self) -> None:
        # Arrange / Act
        now = datetime(2026, 9, 1, tzinfo=JST)
        slots = propose_slots([], date(2026, 9, 10), [time(19, 0)], per_day=1, count=0, now=now)

        # Assert
        assert slots == []

    def test_raises_when_every_day_is_fully_booked(self) -> None:
        # Arrange: existing posts occupy every single candidate slot for a year
        now = datetime(2026, 9, 1, tzinfo=JST)
        start = date(2026, 9, 10)
        existing = [
            datetime.combine(start, time(19, 0), tzinfo=JST) + timedelta(days=d) for d in range(0, 400)
        ]

        # Act / Assert
        with pytest.raises(SlotSearchExhausted):
            propose_slots(existing, start, [time(19, 0)], per_day=1, count=1, now=now)

    def test_max_days_bounds_the_search_to_the_days_we_actually_have_data_for(self) -> None:
        # Arrange: no existing posts, but we only fetched (know about) an
        # 8-day window -- searching past it would silently treat unknown
        # (possibly already-booked) days as free.
        now = datetime(2026, 9, 1, tzinfo=JST)
        start = date(2026, 9, 10)

        # Act / Assert: count=10 with only 8 days searchable is unsatisfiable
        with pytest.raises(SlotSearchExhausted):
            propose_slots([], start, [time(19, 0)], per_day=1, count=10, now=now, max_days=8)

    def test_max_days_default_is_still_the_full_year_search(self) -> None:
        # Arrange / Act: unchanged default behaviour for direct callers
        now = datetime(2026, 9, 1, tzinfo=JST)
        slots = propose_slots([], date(2026, 9, 10), [time(19, 0)], per_day=1, count=1, now=now)

        # Assert
        assert slots == [datetime(2026, 9, 10, 19, 0, tzinfo=JST)]

    def test_slot_exactly_30_minutes_from_existing_is_a_collision(self) -> None:
        # Arrange: the collision window is inclusive of exactly 30 minutes
        now = datetime(2026, 9, 1, 0, 0, tzinfo=JST)
        start = date(2026, 9, 10)
        existing = [datetime(2026, 9, 10, 19, 30, tzinfo=JST)]

        # Act
        slots = propose_slots(existing, start, [time(19, 0)], per_day=1, count=1, now=now)

        # Assert: 30 minutes is still within the +-30min window, so 9/10 is skipped
        assert slots == [datetime(2026, 9, 11, 19, 0, tzinfo=JST)]

    def test_collision_check_compares_across_timezones_via_absolute_time(self) -> None:
        # Arrange: an existing post reported in UTC (e.g. straight from the
        # API before any JST normalization) that is absolutely the same
        # instant as the JST candidate -- collision detection must not be
        # fooled by the differing tzinfo objects
        now = datetime(2026, 9, 1, 0, 0, tzinfo=JST)
        start = date(2026, 9, 10)
        existing = [datetime(2026, 9, 10, 10, 0, tzinfo=UTC)]  # == 2026-09-10 19:00 JST

        # Act
        slots = propose_slots(existing, start, [time(19, 0)], per_day=1, count=1, now=now)

        # Assert
        assert slots == [datetime(2026, 9, 11, 19, 0, tzinfo=JST)]

    def test_candidate_exactly_15_minutes_from_now_is_not_skipped(self) -> None:
        # Arrange: the lead-time check is a strict "<", so exactly 15
        # minutes out is allowed
        now = datetime(2026, 9, 10, 18, 45, 0, tzinfo=JST)
        start = date(2026, 9, 10)

        # Act
        slots = propose_slots([], start, [time(19, 0)], per_day=1, count=1, now=now)

        # Assert
        assert slots == [datetime(2026, 9, 10, 19, 0, tzinfo=JST)]

    def test_candidate_one_second_short_of_15_minutes_from_now_is_skipped(self) -> None:
        # Arrange: 14 minutes 59 seconds out -- just inside the exclusion zone
        now = datetime(2026, 9, 10, 18, 45, 1, tzinfo=JST)
        start = date(2026, 9, 10)

        # Act
        slots = propose_slots([], start, [time(19, 0)], per_day=1, count=1, now=now)

        # Assert
        assert slots == [datetime(2026, 9, 11, 19, 0, tzinfo=JST)]

    def test_times_spanning_midnight_are_taken_in_given_order_not_chronological_order(self) -> None:
        # Arrange: 23:30 then 00:30 -- both resolve to the SAME calendar day
        # (datetime.combine doesn't roll over), so this locks in that
        # propose_slots never reorders/wraps them to the next date
        now = datetime(2026, 9, 1, 0, 0, tzinfo=JST)
        start = date(2026, 9, 10)

        # Act
        slots = propose_slots(
            [], start, [time(23, 30), time(0, 30)], per_day=2, count=2, now=now
        )

        # Assert
        assert slots == [
            datetime(2026, 9, 10, 23, 30, tzinfo=JST),
            datetime(2026, 9, 10, 0, 30, tzinfo=JST),
        ]

    def test_no_dst_discontinuity_around_a_northern_hemisphere_spring_forward_date(self) -> None:
        # Arrange: JST is a fixed UTC+9 offset (Japan observes no DST) --
        # even across a date that is a DST "spring forward" transition
        # elsewhere (2026-03-08, US/Eastern), stepping must be a plain
        # 24h/1-calendar-day increment with no missing/duplicated hour
        now = datetime(2026, 3, 1, tzinfo=JST)
        start = date(2026, 3, 8)

        # Act
        slots = propose_slots([], start, [time(2, 30)], per_day=1, count=2, now=now)

        # Assert
        assert slots == [
            datetime(2026, 3, 8, 2, 30, tzinfo=JST),
            datetime(2026, 3, 9, 2, 30, tzinfo=JST),
        ]
        assert slots[1] - slots[0] == timedelta(days=1)


class TestResolveDayTimes:
    def test_uses_single_time_when_times_option_absent_and_per_day_is_1(self) -> None:
        # Arrange / Act
        result = resolve_day_times("19:00", None, per_day=1)

        # Assert
        assert result == [time(19, 0)]

    def test_per_day_greater_than_1_without_times_is_an_error(self) -> None:
        # Arrange: --per-day 2 but no --times -> ambiguous, must be explicit
        # Act / Assert
        with pytest.raises(ValueError, match="1日2本なら --times で時刻を2個指定してください"):
            resolve_day_times("19:00", None, per_day=2)

    def test_splits_comma_separated_times_option_matching_per_day(self) -> None:
        # Arrange / Act
        result = resolve_day_times("19:00", "09:00,12:30,21:00", per_day=3)

        # Assert
        assert result == [time(9, 0), time(12, 30), time(21, 0)]

    def test_times_option_with_fewer_entries_than_per_day_is_an_error(self) -> None:
        # Arrange: 1日3本のつもりが --times に2つしか無い
        # Act / Assert
        with pytest.raises(ValueError, match="1日3本なら --times で時刻を3個指定してください"):
            resolve_day_times("19:00", "09:00,12:30", per_day=3)

    def test_times_option_with_more_entries_than_per_day_is_an_error(self) -> None:
        # Arrange: --times に3つ指定したのに --per-day 2 (曖昧に間引かない)
        # Act / Assert
        with pytest.raises(ValueError, match="1日2本なら --times で時刻を2個指定してください"):
            resolve_day_times("19:00", "09:00,12:30,21:00", per_day=2)

    def test_time_opt_none_is_fine_when_times_option_is_given(self) -> None:
        # Arrange: argparse's --time/--times mutually-exclusive group means
        # --time is None whenever --times was used instead
        # Act
        result = resolve_day_times(None, "12:00,19:00", per_day=2)

        # Assert
        assert result == [time(12, 0), time(19, 0)]

    def test_both_time_and_times_absent_is_an_error(self) -> None:
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="--time または --times"):
            resolve_day_times(None, None, per_day=1)

    def test_parse_hhmm_rejects_missing_colon(self) -> None:
        # Arrange / Act / Assert
        with pytest.raises(ValueError):
            parse_hhmm("1900")


class TestParseFfprobe:
    def test_extracts_duration_width_height_and_codecs(self) -> None:
        # Arrange
        data = {
            "format": {"duration": "58.5"},
            "streams": [
                {"codec_type": "video", "width": 1080, "height": 1920, "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }

        # Act
        result = parse_ffprobe(data)

        # Assert
        assert result == {
            "duration_sec": 58.5,
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
            "audio_codec": "aac",
        }

    def test_audio_codec_is_none_when_no_audio_stream(self) -> None:
        # Arrange
        data = {
            "format": {"duration": "10.0"},
            "streams": [{"codec_type": "video", "width": 100, "height": 100, "codec_name": "h264"}],
        }

        # Act
        result = parse_ffprobe(data)

        # Assert
        assert result["audio_codec"] is None

    def test_raises_when_no_video_stream(self) -> None:
        # Arrange
        data = {"format": {"duration": "10.0"}, "streams": [{"codec_type": "audio"}]}

        # Act / Assert
        with pytest.raises(ValueError):
            parse_ffprobe(data)

    def test_falls_back_to_stream_duration_when_format_duration_missing(self) -> None:
        # Arrange: some containers only report duration on the video stream
        data = {
            "format": {},
            "streams": [{"codec_type": "video", "width": 1, "height": 1, "codec_name": "h264", "duration": "5.0"}],
        }

        # Act
        result = parse_ffprobe(data)

        # Assert
        assert result["duration_sec"] == 5.0


class TestNeedsTranscode:
    def test_under_limit_does_not_need_transcode(self) -> None:
        assert needs_transcode(19 * 1024 * 1024) is False

    def test_over_limit_needs_transcode(self) -> None:
        assert needs_transcode(19 * 1024 * 1024 + 1) is True


class TestApproxXWeightedLength:
    def test_ascii_counts_one_per_char(self) -> None:
        assert approx_x_weighted_length("a" * 280) == 280

    def test_fullwidth_counts_two_per_char(self) -> None:
        assert approx_x_weighted_length("あ" * 140) == 280

    def test_url_counts_as_23_regardless_of_length(self) -> None:
        # Arrange
        url = "https://example.com/very/long/path/that/would/otherwise/blow/the/budget"

        # Act
        length_short = approx_x_weighted_length(url)
        length_long = approx_x_weighted_length(url + " " + "x" * 45)

        # Assert
        assert length_short == 23
        assert length_long == 23 + 1 + 45

    def test_url_without_a_scheme_is_not_recognized_as_a_url(self) -> None:
        # Arrange: the pattern requires http(s):// -- a bare domain is just
        # counted as plain characters, not the flat 23-weight URL rule
        text = "example.com/path"

        # Act
        length = approx_x_weighted_length(text)

        # Assert
        assert length == len(text)

    def test_http_scheme_url_also_counts_as_23(self) -> None:
        assert approx_x_weighted_length("http://example.com/path") == 23

    def test_emoji_counts_as_two_like_cjk(self) -> None:
        # Arrange: emoji report east_asian_width "W" (wide), same bucket as CJK
        assert approx_x_weighted_length("🎉") == 2

    def test_halfwidth_katakana_counts_as_one(self) -> None:
        # Arrange: halfwidth forms report east_asian_width "H", not "W"/"F"
        assert approx_x_weighted_length("ｱｲｳ") == 3


class TestValidateClipLocally:
    def _now(self) -> datetime:
        return datetime(2026, 9, 1, tzinfo=JST)

    def _clip(self, targets: list[dict]) -> dict:
        return {"no": 1, "file": "/abs/01.mp4", "title": "t", "targets": targets}

    def test_no_issues_for_a_valid_clip(self) -> None:
        # Arrange
        clip = self._clip(
            [
                {"service": "tiktok", "caption": "hello", "due_at": "2026-09-25T19:00:00+09:00"},
                {"service": "x", "caption": "hi", "due_at": "2026-09-25T19:00:00+09:00"},
            ]
        )

        # Act
        issues = validate_clip_locally(clip, duration_sec=30.0, now=self._now())

        # Assert
        assert issues == []

    def test_duplicate_service_is_an_error(self) -> None:
        # Arrange
        clip = self._clip(
            [
                {"service": "x", "caption": "a", "due_at": "2026-09-25T19:00:00+09:00"},
                {"service": "x", "caption": "b", "due_at": "2026-09-25T19:00:00+09:00"},
            ]
        )

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert any("重複" in i.message for i in issues)
        assert plan_has_error(issues)

    def test_x_video_over_140_seconds_is_an_error(self) -> None:
        # Arrange
        clip = self._clip([{"service": "x", "caption": "a", "due_at": "2026-09-25T19:00:00+09:00"}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=141.0, now=self._now())

        # Assert
        assert len(issues) == 1
        assert issues[0].service == "x"
        assert issues[0].level == "error"

    def test_due_at_without_timezone_is_an_error(self) -> None:
        # Arrange
        clip = self._clip([{"service": "tiktok", "caption": "a", "due_at": "2026-09-25T19:00:00"}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert any("タイムゾーン" in i.message for i in issues)

    def test_due_at_in_the_past_is_an_error(self) -> None:
        # Arrange
        clip = self._clip([{"service": "tiktok", "caption": "a", "due_at": "2020-01-01T00:00:00+09:00"}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert any("過去" in i.message for i in issues)

    def test_missing_due_at_is_an_error(self) -> None:
        # Arrange
        clip = self._clip([{"service": "tiktok", "caption": "a", "due_at": None}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert len(issues) == 1
        assert "due_at" in issues[0].message

    def test_instagram_hashtag_over_limit_is_an_error(self) -> None:
        # Arrange
        hashtags = " ".join(f"#tag{i}" for i in range(31))
        clip = self._clip(
            [{"service": "instagram", "caption": hashtags, "due_at": "2026-09-25T19:00:00+09:00"}]
        )

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert any("ハッシュタグ" in i.message for i in issues)

    def test_generic_caption_over_2200_codepoints_is_an_error(self) -> None:
        # Arrange
        clip = self._clip(
            [{"service": "tiktok", "caption": "a" * 2201, "due_at": "2026-09-25T19:00:00+09:00"}]
        )

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert any(i.level == "error" for i in issues)

    def test_unknown_service_is_an_error(self) -> None:
        # Arrange
        clip = self._clip([{"service": "youtube", "caption": "a", "due_at": "2026-09-25T19:00:00+09:00"}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert len(issues) == 1
        assert "未知の媒体" in issues[0].message

    def test_x_video_at_exactly_140_seconds_is_not_an_error(self) -> None:
        # Arrange: the limit check is a strict ">", so exactly 140.0s is fine
        clip = self._clip([{"service": "x", "caption": "a", "due_at": "2026-09-25T19:00:00+09:00"}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=140.0, now=self._now())

        # Assert
        assert issues == []

    def test_x_video_at_140_01_seconds_is_an_error(self) -> None:
        # Arrange
        clip = self._clip([{"service": "x", "caption": "a", "due_at": "2026-09-25T19:00:00+09:00"}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=140.01, now=self._now())

        # Assert
        assert len(issues) == 1
        assert issues[0].service == "x"

    def test_due_at_one_second_in_the_past_is_an_error(self) -> None:
        # Arrange
        now = self._now()
        due_at = (now - timedelta(seconds=1)).isoformat()
        clip = self._clip([{"service": "tiktok", "caption": "a", "due_at": due_at}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=now)

        # Assert
        assert any("過去" in i.message for i in issues)

    def test_due_at_exactly_equal_to_now_is_an_error(self) -> None:
        # Arrange: the check is "<=", so an exact match still counts as past
        now = self._now()
        clip = self._clip([{"service": "tiktok", "caption": "a", "due_at": now.isoformat()}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=now)

        # Assert
        assert any("過去" in i.message for i in issues)

    def test_instagram_exactly_30_hashtags_is_not_an_error(self) -> None:
        # Arrange: the limit check is a strict ">", so exactly 30 is fine
        hashtags = " ".join(f"#tag{i}" for i in range(30))
        clip = self._clip(
            [{"service": "instagram", "caption": hashtags, "due_at": "2026-09-25T19:00:00+09:00"}]
        )

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert issues == []

    def test_instagram_fullwidth_hashtag_chars_are_counted_toward_the_limit(self) -> None:
        # Arrange: 29 ASCII "#" hashtags + 2 fullwidth "＃" hashtags = 31
        hashtags = " ".join(f"#tag{i}" for i in range(29)) + " ＃tagA ＃tagB"
        clip = self._clip(
            [{"service": "instagram", "caption": hashtags, "due_at": "2026-09-25T19:00:00+09:00"}]
        )

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert any("ハッシュタグ" in i.message for i in issues)

    def test_x_caption_at_exactly_280_weighted_length_is_not_an_error(self) -> None:
        # Arrange: the limit check is a strict ">", so exactly 280 is fine
        clip = self._clip([{"service": "x", "caption": "a" * 280, "due_at": "2026-09-25T19:00:00+09:00"}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert issues == []

    def test_x_caption_at_281_weighted_length_is_an_error(self) -> None:
        # Arrange
        clip = self._clip([{"service": "x", "caption": "a" * 281, "due_at": "2026-09-25T19:00:00+09:00"}])

        # Act
        issues = validate_clip_locally(clip, duration_sec=10.0, now=self._now())

        # Assert
        assert any("本文が長すぎます" in i.message for i in issues)


class TestNormalizeYoutubeUrl:
    """The canonical 本編 link form is exactly `https://www.youtube.com/watch?v=<ID>`
    (plus `t` if present). Every recognized YouTube 本編 URL shape is rewritten
    to it, not just stripped of tracking params."""

    def test_share_link_becomes_the_canonical_watch_form(self) -> None:
        # Arrange / Act: the exact example from the bug report
        result = normalize_youtube_url("https://youtu.be/MCmsezNx-Os?si=rwDOsGT0rpujKWYP")

        # Assert
        assert result == "https://www.youtube.com/watch?v=MCmsezNx-Os"

    def test_watch_form_is_unchanged(self) -> None:
        result = normalize_youtube_url("https://www.youtube.com/watch?v=abc123")
        assert result == "https://www.youtube.com/watch?v=abc123"

    def test_youtu_be_without_any_query_becomes_the_canonical_form(self) -> None:
        result = normalize_youtube_url("https://youtu.be/abc123")
        assert result == "https://www.youtube.com/watch?v=abc123"

    def test_m_dot_youtube_com_becomes_the_canonical_form(self) -> None:
        result = normalize_youtube_url("https://m.youtube.com/watch?v=abc123")
        assert result == "https://www.youtube.com/watch?v=abc123"

    def test_youtube_com_without_www_becomes_the_canonical_form(self) -> None:
        result = normalize_youtube_url("https://youtube.com/watch?v=abc123")
        assert result == "https://www.youtube.com/watch?v=abc123"

    def test_strips_feature_and_pp(self) -> None:
        result = normalize_youtube_url("https://www.youtube.com/watch?v=abc&feature=share&pp=xyz")
        assert result == "https://www.youtube.com/watch?v=abc"

    def test_strips_utm_params(self) -> None:
        result = normalize_youtube_url("https://youtu.be/abc?utm_source=x&utm_medium=y")
        assert result == "https://www.youtube.com/watch?v=abc"

    def test_keeps_t_param(self) -> None:
        result = normalize_youtube_url("https://www.youtube.com/watch?v=abc&t=30&si=xyz")
        assert result == "https://www.youtube.com/watch?v=abc&t=30"

    def test_drops_list_param(self) -> None:
        # Arrange: only v (and t, if present) survive -- the canonical form
        # is exactly watch?v=<ID>[&t=..], nothing else
        result = normalize_youtube_url("https://www.youtube.com/watch?v=abc&list=PLxyz&t=30")
        assert result == "https://www.youtube.com/watch?v=abc&t=30"

    def test_live_url_becomes_the_canonical_watch_form(self) -> None:
        result = normalize_youtube_url("https://www.youtube.com/live/abc123?si=xyz")
        assert result == "https://www.youtube.com/watch?v=abc123"

    def test_unrecognized_url_only_has_tracking_params_stripped(self) -> None:
        # Arrange: not a recognized 本編 shape (e.g. a shorts link) -- can't
        # extract a video ID to rewrite, so just strip known tracking params
        result = normalize_youtube_url("https://www.youtube.com/shorts/abc123?si=xyz")
        assert result == "https://www.youtube.com/shorts/abc123"

    def test_non_youtube_url_is_unchanged_apart_from_tracking_params(self) -> None:
        result = normalize_youtube_url("https://vimeo.com/12345?utm_source=x")
        assert result == "https://vimeo.com/12345"

    def test_is_idempotent(self) -> None:
        once = normalize_youtube_url("https://youtu.be/MCmsezNx-Os?si=rwDOsGT0rpujKWYP")
        twice = normalize_youtube_url(once)
        assert once == twice


class TestIsYoutubeUrl:
    def test_watch_url_is_youtube(self) -> None:
        assert is_youtube_url("https://www.youtube.com/watch?v=abc123") is True

    def test_short_url_is_youtube(self) -> None:
        assert is_youtube_url("https://youtu.be/abc123") is True

    def test_live_url_is_youtube(self) -> None:
        assert is_youtube_url("https://www.youtube.com/live/abc123") is True

    def test_non_youtube_url_is_not_youtube(self) -> None:
        assert is_youtube_url("https://vimeo.com/12345") is False

    def test_youtube_domain_without_a_recognized_path_is_not_youtube(self) -> None:
        # Arrange: e.g. a channel URL, not a specific video
        assert is_youtube_url("https://www.youtube.com/@somechannel") is False

    def test_shorts_url_is_not_a_recognized_main_video_url(self) -> None:
        # Arrange: a YouTube Shorts URL is not the 本編 (main video) --
        # posts must link to the full video, not a short
        assert is_youtube_url("https://www.youtube.com/shorts/abc123") is False


class TestResolveMainVideoUrl:
    def test_clip_override_wins_over_plan_default(self) -> None:
        plan = {"main_video_url": "https://youtu.be/plan"}
        clip = {"no": 1, "main_video_url": "https://youtu.be/clip-override"}
        assert resolve_main_video_url(plan, clip) == "https://www.youtube.com/watch?v=clip-override"

    def test_falls_back_to_plan_default_when_clip_has_no_override(self) -> None:
        plan = {"main_video_url": "https://youtu.be/plan"}
        clip = {"no": 1}
        assert resolve_main_video_url(plan, clip) == "https://www.youtube.com/watch?v=plan"

    def test_none_when_neither_is_set(self) -> None:
        assert resolve_main_video_url({}, {"no": 1}) is None

    def test_normalizes_tracking_params_out_of_plan_default(self) -> None:
        plan = {"main_video_url": "https://youtu.be/MCmsezNx-Os?si=rwDOsGT0rpujKWYP"}
        assert resolve_main_video_url(plan, {"no": 1}) == "https://www.youtube.com/watch?v=MCmsezNx-Os"

    def test_normalizes_tracking_params_out_of_clip_override(self) -> None:
        plan = {"main_video_url": "https://youtu.be/plan"}
        clip = {"no": 1, "main_video_url": "https://youtu.be/clip-override?si=xyz"}
        assert resolve_main_video_url(plan, clip) == "https://www.youtube.com/watch?v=clip-override"


class TestValidateMainVideoUrlLocally:
    def _clip(self, targets: list[dict], main_video_url: str | None = None) -> dict:
        clip = {"no": 1, "file": "/abs/01.mp4", "title": "t", "targets": targets}
        if main_video_url is not None:
            clip["main_video_url"] = main_video_url
        return clip

    def test_missing_main_video_url_is_an_error(self) -> None:
        clip = self._clip([])
        issues = validate_main_video_url_locally(clip, None)
        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].service == "main_video_url"

    def test_non_youtube_main_video_url_is_a_warning(self) -> None:
        clip = self._clip([])
        issues = validate_main_video_url_locally(clip, "https://vimeo.com/123")
        assert len(issues) == 1
        assert issues[0].level == "warning"

    def test_valid_youtube_url_with_no_targets_has_no_issues(self) -> None:
        clip = self._clip([])
        issues = validate_main_video_url_locally(clip, "https://youtu.be/abc123")
        assert issues == []

    def test_x_caption_containing_the_url_has_no_issue(self) -> None:
        url = "https://youtu.be/abc123"
        clip = self._clip([{"service": "x", "caption": f"本編はこちら {url}"}])
        issues = validate_main_video_url_locally(clip, url)
        assert issues == []

    def test_x_caption_missing_the_url_is_an_error(self) -> None:
        url = "https://youtu.be/abc123"
        clip = self._clip([{"service": "x", "caption": "本編はこちらから見てね"}])
        issues = validate_main_video_url_locally(clip, url)
        assert len(issues) == 1
        assert issues[0].service == "x"
        assert issues[0].level == "error"

    def test_x_url_check_is_skipped_when_main_video_url_itself_is_missing(self) -> None:
        # Arrange: already reported as its own error; don't also complain
        # that the (nonexistent) URL isn't in the caption
        clip = self._clip([{"service": "x", "caption": "本編はこちらから見てね"}])

        # Act
        issues = validate_main_video_url_locally(clip, None)

        # Assert: only the missing-main_video_url issue, nothing about X's caption
        assert len(issues) == 1
        assert issues[0].service == "main_video_url"

    def test_tiktok_caption_containing_the_url_has_no_issue(self) -> None:
        url = "https://youtu.be/abc123"
        clip = self._clip([{"service": "tiktok", "caption": f"本編はこちら {url} #営業"}])
        issues = validate_main_video_url_locally(clip, url)
        assert issues == []

    def test_tiktok_caption_missing_the_url_is_an_error(self) -> None:
        url = "https://youtu.be/abc123"
        clip = self._clip([{"service": "tiktok", "caption": "見てね #営業"}])
        issues = validate_main_video_url_locally(clip, url)
        assert len(issues) == 1
        assert issues[0].service == "tiktok"
        assert issues[0].level == "error"

    def test_instagram_caption_missing_the_url_is_an_error(self) -> None:
        url = "https://youtu.be/abc123"
        clip = self._clip([{"service": "instagram", "caption": "見てね #営業"}])
        issues = validate_main_video_url_locally(clip, url)
        assert len(issues) == 1
        assert issues[0].service == "instagram"
        assert issues[0].level == "error"

    def test_tiktok_and_instagram_url_check_is_skipped_when_main_video_url_itself_is_missing(self) -> None:
        # Arrange: already reported as its own error; don't also complain
        # about a (nonexistent) URL not being in the caption
        clip = self._clip(
            [
                {"service": "tiktok", "caption": "見てね"},
                {"service": "instagram", "caption": "見てね"},
            ]
        )

        # Act
        issues = validate_main_video_url_locally(clip, None)

        # Assert: only the missing-main_video_url issue
        assert len(issues) == 1
        assert issues[0].service == "main_video_url"

    def test_uses_the_resolved_per_clip_override_url_for_the_x_check(self) -> None:
        # Arrange: caller resolves clip override vs plan default before
        # calling this -- verify the check uses whatever URL it's given
        override_url = "https://youtu.be/clip-specific"
        clip = self._clip(
            [{"service": "x", "caption": f"see {override_url}"}], main_video_url=override_url
        )

        # Act
        issues = validate_main_video_url_locally(clip, override_url)

        # Assert
        assert issues == []


class TestLocalIssueHelpers:
    def test_plan_has_error_false_when_only_warnings(self) -> None:
        issues = [LocalIssue(1, "x", "warning", "w")]
        assert plan_has_error(issues) is False

    def test_plan_has_error_true_when_any_error(self) -> None:
        issues = [LocalIssue(1, "x", "warning", "w"), LocalIssue(1, "x", "error", "e")]
        assert plan_has_error(issues) is True


class TestIsFullySettled:
    def test_true_when_every_target_is_a_success_status(self) -> None:
        results = [{"status": "scheduled"}, {"status": "sent"}, {"status": "draft"}]
        assert is_fully_settled(results) is True

    def test_false_when_any_target_is_not_settled(self) -> None:
        results = [{"status": "scheduled"}, {"status": "failed"}]
        assert is_fully_settled(results) is False

    def test_false_when_results_empty(self) -> None:
        assert is_fully_settled([]) is False

    def test_true_when_a_target_was_canceled(self) -> None:
        # Arrange: canceled is terminal too -- a resend must never touch it
        # (canceled stays canceled; edit/re-add it in the admin panel instead)
        results = [{"status": "scheduled"}, {"status": "canceled"}]

        # Act / Assert
        assert is_fully_settled(results) is True

    def test_false_when_a_failed_target_is_mixed_with_canceled_ones(self) -> None:
        # Arrange: a clip must still be re-submitted (to resend just the
        # failed target) even if its other targets are canceled
        results = [{"status": "canceled"}, {"status": "failed"}]

        # Act / Assert
        assert is_fully_settled(results) is False


class TestSummarizeSubmitResults:
    def test_counts_settled_targets_as_scheduled(self) -> None:
        # Arrange
        clip_results = [
            {
                "clip_no": 1,
                "skipped": False,
                "result": {
                    "incomplete": False,
                    "results": [{"status": "scheduled"}, {"status": "sent"}, {"status": "draft"}],
                },
            }
        ]

        # Act
        summary = summarize_submit_results(clip_results)

        # Assert
        assert summary == {"scheduled": 3, "failed": 0, "unknown": 0, "skipped": 0, "not_attempted": 0}

    def test_dry_run_skip_counts_as_one_skipped(self) -> None:
        clip_results = [{"clip_no": 1, "skipped": True, "plan": {}}]
        summary = summarize_submit_results(clip_results)
        assert summary["skipped"] == 1

    def test_clip_level_api_error_counts_as_one_failed(self) -> None:
        clip_results = [{"clip_no": 1, "skipped": False, "error": "boom"}]
        summary = summarize_submit_results(clip_results)
        assert summary["failed"] == 1

    def test_unknown_and_not_attempted_target_statuses_are_counted(self) -> None:
        clip_results = [
            {
                "clip_no": 1,
                "skipped": False,
                "result": {
                    "incomplete": False,
                    "results": [{"status": "unknown"}, {"status": "not_attempted"}, {"status": "failed"}],
                },
            }
        ]
        summary = summarize_submit_results(clip_results)
        assert summary == {"scheduled": 0, "failed": 1, "unknown": 1, "skipped": 0, "not_attempted": 1}

    def test_unexpected_status_falls_back_to_failed(self) -> None:
        clip_results = [
            {"clip_no": 1, "skipped": False, "result": {"incomplete": False, "results": [{"status": "db_write_failed"}]}}
        ]
        summary = summarize_submit_results(clip_results)
        assert summary["failed"] == 1

    def test_canceled_target_is_not_counted_as_failed(self) -> None:
        # Arrange: a target canceled via the admin panel is an intentional,
        # already-handled outcome -- it must not surface as a "failure"
        clip_results = [
            {
                "clip_no": 1,
                "skipped": False,
                "result": {"incomplete": False, "results": [{"status": "scheduled"}, {"status": "canceled"}]},
            }
        ]

        # Act
        summary = summarize_submit_results(clip_results)

        # Assert
        assert summary == {"scheduled": 1, "failed": 0, "unknown": 0, "skipped": 0, "not_attempted": 0}


class TestSubmitHasProblems:
    def test_false_when_summary_is_all_zero_and_nothing_incomplete(self) -> None:
        summary = {"scheduled": 3, "failed": 0, "unknown": 0, "skipped": 0, "not_attempted": 0}
        clip_results = [{"clip_no": 1, "skipped": False, "result": {"incomplete": False, "results": []}}]
        assert submit_has_problems(clip_results, summary) is False

    def test_true_when_summary_has_any_failure(self) -> None:
        summary = {"scheduled": 0, "failed": 1, "unknown": 0, "skipped": 0, "not_attempted": 0}
        assert submit_has_problems([], summary) is True

    def test_true_when_a_clip_is_still_incomplete_after_retries(self) -> None:
        # Arrange: all-zero summary, but the clip's last response was still incomplete
        summary = {"scheduled": 0, "failed": 0, "unknown": 0, "skipped": 0, "not_attempted": 0}
        clip_results = [{"clip_no": 1, "skipped": False, "result": {"incomplete": True, "results": []}}]

        # Act / Assert
        assert submit_has_problems(clip_results, summary) is True


class TestMergeGalleryData:
    def _base(self) -> dict:
        return {
            "project": "p",
            "delivered_at": "2026-09-23",
            "delivery_dir": "/x",
            "clips": [
                {
                    "no": 1,
                    "file": "/abs/01.mp4",
                    "title": "t1",
                    "title_options": [],
                    "duration_sec": 10.0,
                    "size_bytes": 100,
                    "video_url": "https://cdn.example.com/01.mp4",
                    "schedule": None,
                }
            ],
        }

    def test_returns_base_unchanged_when_plan_is_none(self) -> None:
        base = self._base()
        assert merge_gallery_data(base, None, None) == base

    def test_video_url_is_kept_from_base_not_plan(self) -> None:
        base = self._base()
        plan = {
            "clips": [
                {
                    "no": 1,
                    "targets": [{"service": "tiktok", "caption": "c", "due_at": "2026-09-25T19:00:00+09:00"}],
                }
            ]
        }

        merged = merge_gallery_data(base, plan, None)

        assert merged["clips"][0]["video_url"] == "https://cdn.example.com/01.mp4"

    def test_targets_default_to_proposed_status_when_no_state(self) -> None:
        base = self._base()
        plan = {
            "clips": [
                {
                    "no": 1,
                    "targets": [
                        {"service": "tiktok", "caption": "hello", "due_at": "2026-09-25T19:00:00+09:00"},
                        {"service": "x", "caption": "hi", "due_at": "2026-09-25T19:00:00+09:00"},
                    ],
                }
            ]
        }

        merged = merge_gallery_data(base, plan, None)

        schedule = merged["clips"][0]["schedule"]
        assert schedule["due_at"] == "2026-09-25T19:00:00+09:00"
        tiktok = next(t for t in schedule["targets"] if t["service"] == "tiktok")
        assert tiktok["status"] == "proposed"
        assert tiktok["length"] == 5
        assert tiktok["limit"] == 2200
        x_target = next(t for t in schedule["targets"] if t["service"] == "x")
        assert x_target["limit"] == 280

    def test_status_comes_from_state_submission_result_when_present(self) -> None:
        base = self._base()
        plan = {
            "clips": [
                {
                    "no": 1,
                    "targets": [{"service": "tiktok", "caption": "hello", "due_at": "2026-09-25T19:00:00+09:00"}],
                }
            ]
        }
        state = {
            "clips": {
                "1": {
                    "submission_result": {
                        "results": [
                            {"service": "tiktok", "status": "scheduled", "external_link": None, "error": None}
                        ]
                    }
                }
            }
        }

        merged = merge_gallery_data(base, plan, state)

        tiktok = merged["clips"][0]["schedule"]["targets"][0]
        assert tiktok["status"] == "scheduled"

    def test_clip_with_no_matching_plan_entry_keeps_its_existing_schedule(self) -> None:
        base = self._base()
        base["clips"][0]["schedule"] = {"due_at": "x", "targets": []}
        plan = {"clips": []}  # no clip no=1 in the plan

        merged = merge_gallery_data(base, plan, None)

        assert merged["clips"][0]["schedule"] == {"due_at": "x", "targets": []}

    def test_clip_present_in_plan_but_missing_from_base_is_ignored_without_error(self) -> None:
        # Arrange: plan references a clip `no` that isn't in base.json (e.g.
        # base was regenerated without it) -- merge iterates base's clips,
        # so the extra plan entry must be silently dropped, not crash
        base = self._base()
        plan = {
            "clips": [
                {
                    "no": 1,
                    "targets": [{"service": "tiktok", "caption": "c", "due_at": "2026-09-25T19:00:00+09:00"}],
                },
                {
                    "no": 99,
                    "targets": [{"service": "tiktok", "caption": "ghost", "due_at": "2026-09-25T19:00:00+09:00"}],
                },
            ]
        }

        # Act
        merged = merge_gallery_data(base, plan, None)

        # Assert
        assert [c["no"] for c in merged["clips"]] == [1]

    def test_target_without_a_matching_state_result_defaults_to_proposed_even_with_a_sibling_result(
        self,
    ) -> None:
        # Arrange: state only has a result for "tiktok" -- the clip's other
        # target ("x") must independently fall back to "proposed", not
        # inherit tiktok's status
        base = self._base()
        plan = {
            "clips": [
                {
                    "no": 1,
                    "targets": [
                        {"service": "tiktok", "caption": "hello", "due_at": "2026-09-25T19:00:00+09:00"},
                        {"service": "x", "caption": "hi", "due_at": "2026-09-25T19:00:00+09:00"},
                    ],
                }
            ]
        }
        state = {
            "clips": {
                "1": {
                    "submission_result": {
                        "results": [
                            {"service": "tiktok", "status": "scheduled", "external_link": None, "error": None}
                        ]
                    }
                }
            }
        }

        # Act
        merged = merge_gallery_data(base, plan, state)

        # Assert
        targets = {t["service"]: t for t in merged["clips"][0]["schedule"]["targets"]}
        assert targets["tiktok"]["status"] == "scheduled"
        assert targets["x"]["status"] == "proposed"

    def test_plan_main_video_url_overrides_base_main_video_url(self) -> None:
        # Arrange: base.json already has one (set during the artifact step,
        # from hearing.md); plan.json's is authoritative once scheduling starts
        base = self._base()
        base["main_video_url"] = "https://youtu.be/from-artifact-step"
        plan = {"main_video_url": "https://youtu.be/from-plan", "clips": []}

        # Act
        merged = merge_gallery_data(base, plan, None)

        # Assert
        assert merged["main_video_url"] == "https://www.youtube.com/watch?v=from-plan"

    def test_base_main_video_url_is_kept_when_plan_has_none(self) -> None:
        # Arrange
        base = self._base()
        base["main_video_url"] = "https://youtu.be/from-artifact-step"
        plan = {"clips": []}  # no main_video_url key at all

        # Act
        merged = merge_gallery_data(base, plan, None)

        # Assert
        assert merged["main_video_url"] == "https://youtu.be/from-artifact-step"

    def test_plan_main_video_url_is_normalized(self) -> None:
        # Arrange: user pasted a share link with tracking params into plan.json
        base = self._base()
        plan = {"main_video_url": "https://youtu.be/MCmsezNx-Os?si=rwDOsGT0rpujKWYP", "clips": []}

        # Act
        merged = merge_gallery_data(base, plan, None)

        # Assert
        assert merged["main_video_url"] == "https://www.youtube.com/watch?v=MCmsezNx-Os"
