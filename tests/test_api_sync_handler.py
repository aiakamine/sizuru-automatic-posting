"""Tests for sizuru_post.api.sync_handler: EventBridge Scheduler target."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

import pytest

import sizuru_post.api.sync_handler as sync_handler_module
from sizuru_post.adapters.buffer import BufferAuthError
from sizuru_post.adapters.repo import ChannelRow
from sizuru_post.domain.models import VideoInfo
from sizuru_post.publisher import Publisher
from tests.fakes import FakeBuffer, FakeStorage, InMemoryRepo

VALID_VIDEO = VideoInfo(duration_sec=10.0, width=1080, height=1920, video_codec="h264")


@pytest.fixture
def repo() -> InMemoryRepo:
    return InMemoryRepo(
        channels=[
            ChannelRow(
                id=uuid4(),
                buffer_channel_id="buf-x",
                service="x",  # type: ignore[arg-type]
                name="x",
                display_name="x",
                is_disconnected=False,
                is_queue_paused=False,
            )
        ]
    )


@pytest.fixture
def publisher(repo: InMemoryRepo) -> Publisher:
    return Publisher(
        buffer=FakeBuffer(),
        storage=FakeStorage(),
        repo=repo,
        probe=lambda path: VALID_VIDEO,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def patch_publisher(monkeypatch: pytest.MonkeyPatch, publisher: Publisher) -> None:
    monkeypatch.setattr(sync_handler_module, "get_publisher", lambda: publisher)


def test_handler_returns_sync_counts(publisher: Publisher) -> None:
    # Act
    result = sync_handler_module.handler({}, None)

    # Assert
    assert result["sent"] == 0
    assert result["failed"] == 0


def test_handler_invoked_locally_with_context_none_works(publisher: Publisher) -> None:
    # Arrange / Act: local `sam local invoke`/CLI-style calls pass no real
    # LambdaContext at all — must not blow up building should_stop().
    result = sync_handler_module.handler({}, None)

    # Assert
    assert result["sent"] == 0
    assert "truncated" not in result


def test_handler_logs_counts_as_valid_json(publisher: Publisher, caplog: pytest.LogCaptureFixture) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger=sync_handler_module.logger.name)

    # Act
    result = sync_handler_module.handler({}, None)

    # Assert: exactly the returned counts dict, round-trippable as JSON
    # (CloudWatch Logs Insights queries rely on this being valid JSON).
    logged = [json.loads(r.getMessage()) for r in caplog.records if r.name == sync_handler_module.logger.name]
    assert result in logged


def test_handler_calls_sync_with_full_false(publisher: Publisher) -> None:
    # Arrange: the EventBridge scheduler runs frequently — only posts whose
    # due_at has passed (or is null) should be queried, unlike the manual
    # /sync API endpoint (full=True).
    calls: list[dict[str, Any]] = []

    def _spy_sync(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"sent": 0, "failed": 0, "canceled": 0, "unchanged": 0, "stuck_pending": 0}

    publisher.sync = _spy_sync  # type: ignore[method-assign]

    # Act
    sync_handler_module.handler({}, None)

    # Assert
    assert calls[0]["full"] is False


def test_handler_raises_on_buffer_auth_error(monkeypatch: pytest.MonkeyPatch, publisher: Publisher) -> None:
    # Arrange: an invalid Buffer API key must alert (non-2xx equivalent for a
    # scheduled Lambda is an unhandled exception, which surfaces in
    # CloudWatch/EventBridge as a failed invocation).
    def _raise(**kwargs: Any) -> dict[str, Any]:
        raise BufferAuthError("bad key")

    monkeypatch.setattr(publisher, "sync", _raise)

    # Act / Assert
    with pytest.raises(BufferAuthError):
        sync_handler_module.handler({}, None)


def test_handler_resets_cached_publisher_on_buffer_auth_error(
    monkeypatch: pytest.MonkeyPatch, publisher: Publisher
) -> None:
    # Arrange: the secret may have been rotated away — the *next*
    # invocation must rebuild the Publisher (re-reading SSM) rather than
    # keep using a possibly-stale one for the container's lifetime.
    def _raise(**kwargs: Any) -> dict[str, Any]:
        raise BufferAuthError("bad key")

    monkeypatch.setattr(publisher, "sync", _raise)
    sync_handler_module._publisher = publisher
    reset_calls: list[bool] = []
    monkeypatch.setattr(sync_handler_module, "reset_ssm_cache", lambda: reset_calls.append(True))

    # Act / Assert
    with pytest.raises(BufferAuthError):
        sync_handler_module.handler({}, None)
    assert sync_handler_module._publisher is None
    assert reset_calls == [True]
