"""Tests for sizuru_post.domain.models: PublishRequest / Timing / Target validation."""

import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import sizuru_post.domain.models as models_module
from sizuru_post.domain.models import (
    PostStatus,
    PublishRequest,
    Target,
    Timing,
    VideoInfo,
    resolve_timing,
    validate_due_at_is_future,
)


def _future(seconds: int = 3600) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


class TestTiming:
    def test_custom_scheduled_requires_due_at(self) -> None:
        # Arrange / Act / Assert
        with pytest.raises(ValidationError, match="due_at"):
            Timing(mode="customScheduled", due_at=None)

    def test_custom_scheduled_due_at_must_be_timezone_aware(self) -> None:
        # Arrange
        naive = datetime.now() + timedelta(hours=1)  # noqa: DTZ005

        # Act / Assert
        with pytest.raises(ValidationError, match="タイムゾーン"):
            Timing(mode="customScheduled", due_at=naive)

    def test_custom_scheduled_due_at_must_be_future(self) -> None:
        # Arrange
        past = datetime.now(UTC) - timedelta(hours=1)

        # Act / Assert
        with pytest.raises(ValidationError, match="未来"):
            Timing(mode="customScheduled", due_at=past)

    def test_custom_scheduled_accepts_valid_future_due_at(self) -> None:
        # Arrange
        due_at = _future()

        # Act
        timing = Timing(mode="customScheduled", due_at=due_at)

        # Assert
        assert timing.due_at == due_at

    def test_share_now_rejects_due_at(self) -> None:
        # Arrange / Act / Assert
        with pytest.raises(ValidationError, match="customScheduled"):
            Timing(mode="shareNow", due_at=_future())

    def test_add_to_queue_rejects_due_at(self) -> None:
        # Arrange / Act / Assert: same rule applies to every non-customScheduled mode
        with pytest.raises(ValidationError, match="customScheduled"):
            Timing(mode="addToQueue", due_at=_future())

    def test_share_now_without_due_at_is_valid(self) -> None:
        # Act
        timing = Timing(mode="shareNow")

        # Assert
        assert timing.due_at is None


class TestPublishRequest:
    def _target(self, service: str) -> Target:
        return Target(service=service, caption="hello")  # type: ignore[arg-type]

    def test_requires_at_least_one_target(self) -> None:
        # Act / Assert
        with pytest.raises(ValidationError):
            PublishRequest(
                video_path=Path("video.mp4"),
                timing=Timing(mode="shareNow"),
                targets=[],
            )

    def test_rejects_duplicate_service_targets(self) -> None:
        # Act / Assert
        with pytest.raises(ValidationError, match="重複"):
            PublishRequest(
                video_path=Path("video.mp4"),
                timing=Timing(mode="shareNow"),
                targets=[self._target("x"), self._target("x")],
            )

    def test_accepts_valid_request(self) -> None:
        # Act
        request = PublishRequest(
            video_path=Path("video.mp4"),
            timing=Timing(mode="addToQueue"),
            targets=[self._target("tiktok"), self._target("x")],
        )

        # Assert
        assert request.draft is False
        assert len(request.targets) == 2


class TestResolveTiming:
    def test_target_without_override_uses_request_timing(self) -> None:
        # Arrange
        request_timing = Timing(mode="addToQueue")
        request = PublishRequest(
            video_path=Path("video.mp4"),
            timing=request_timing,
            targets=[Target(service="x", caption="hi")],
        )

        # Act
        effective = resolve_timing(request, request.targets[0])

        # Assert
        assert effective is request_timing

    def test_target_with_override_uses_target_timing(self) -> None:
        # Arrange
        override = Timing(mode="customScheduled", due_at=_future())
        target = Target(service="x", caption="hi", timing=override)
        request = PublishRequest(
            video_path=Path("video.mp4"), timing=Timing(mode="addToQueue"), targets=[target]
        )

        # Act
        effective = resolve_timing(request, request.targets[0])

        # Assert
        assert effective is override


class TestVideoInfo:
    def test_holds_probed_fields(self) -> None:
        # Act
        info = VideoInfo(duration_sec=12.5, width=1080, height=1920, video_codec="h264", audio_codec="aac")

        # Assert
        assert info.duration_sec == 12.5
        assert info.audio_codec == "aac"


class TestValidateDueAtIsFuture:
    """Direct unit tests for the shared boundary rule (see its docstring):
    Timing/Publisher.edit()/api/handler.py/api/schedule.py all wrap this
    same function's message in their own exception type, so the rule
    itself only needs to be pinned down once here."""

    def test_naive_datetime_returns_timezone_message(self) -> None:
        # Arrange
        naive = datetime(2030, 1, 1)  # noqa: DTZ001

        # Act
        error = validate_due_at_is_future(naive)

        # Assert
        assert error == "due_at はタイムゾーン情報を含む必要があります"

    def test_future_datetime_returns_none(self) -> None:
        # Arrange / Act / Assert
        assert validate_due_at_is_future(_future()) is None

    def test_exactly_now_is_rejected_as_not_future(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: due_at == "now" must be rejected — the rule is `<=`, not
        # `<` (see the function's own `due_at <= datetime.now(UTC)` check).
        # The wall clock can't reliably land exactly on "now" from the
        # outside, so the module's `datetime` is patched to a fixed clock.
        fixed_now = datetime(2030, 6, 15, 12, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(models_module, "datetime", types.SimpleNamespace(now=lambda tz=None: fixed_now))

        # Act
        error = validate_due_at_is_future(fixed_now)

        # Assert
        assert error == "due_at は未来の日時を指定してください"

    def test_one_microsecond_after_now_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        fixed_now = datetime(2030, 6, 15, 12, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(models_module, "datetime", types.SimpleNamespace(now=lambda tz=None: fixed_now))
        due_at = fixed_now + timedelta(microseconds=1)

        # Act / Assert
        assert validate_due_at_is_future(due_at) is None


class TestPostStatus:
    def test_has_expected_members(self) -> None:
        # Assert
        assert {s.value for s in PostStatus} == {
            "pending",
            "draft",
            "scheduled",
            "sent",
            "failed",
            "canceled",
        }
