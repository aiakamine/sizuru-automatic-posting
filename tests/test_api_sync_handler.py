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
from sizuru_post.api.sync_handler import get_settings as _real_get_settings
from sizuru_post.config import Settings
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
def buffer() -> FakeBuffer:
    return FakeBuffer()


@pytest.fixture
def publisher(buffer: FakeBuffer, repo: InMemoryRepo) -> Publisher:
    return Publisher(
        buffer=buffer,
        storage=FakeStorage(),
        repo=repo,
        probe=lambda path: VALID_VIDEO,  # type: ignore[arg-type]
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        buffer_api_key="k",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="k",
    )


@pytest.fixture(autouse=True)
def patch_publisher(monkeypatch: pytest.MonkeyPatch, publisher: Publisher, settings: Settings) -> None:
    monkeypatch.setattr(sync_handler_module, "get_publisher", lambda: publisher)
    monkeypatch.setattr(sync_handler_module, "get_settings", lambda: settings)


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
    # Only the INFO-level record — a best-effort channel refresh failure
    # (this fixture's Buffer has no organizations configured) logs its own
    # non-JSON WARNING, which must not be mistaken for the counts log.
    info_records = [r for r in caplog.records if r.name == sync_handler_module.logger.name and r.levelno == logging.INFO]
    logged = [json.loads(r.getMessage()) for r in info_records]
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


def test_handler_refreshes_channels_before_syncing_posts(
    publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
) -> None:
    # Arrange: Buffer no longer reports the existing "buf-x" channel at
    # all (replaced) — the same root cause as the live incident this
    # module's channel refresh exists to prevent from recurring.
    buffer.organizations = [{"id": "org-1"}]
    buffer.channels_by_organization = {"org-1": [{"id": "buf-new-x", "service": "x", "name": "new"}]}

    # Act
    sync_handler_module.handler({}, None)

    # Assert
    channels_by_id = {c.buffer_channel_id: c for c in repo.list_channels()}
    assert channels_by_id["buf-x"].is_disconnected is True
    assert channels_by_id["buf-new-x"].is_disconnected is False


def test_channel_refresh_failure_does_not_block_post_sync(
    monkeypatch: pytest.MonkeyPatch, publisher: Publisher, buffer: FakeBuffer
) -> None:
    # Arrange: no BUFFER_ORGANIZATION_ID and Buffer reports no
    # organizations -> sync_channels() raises — the post-status sync
    # below is far more time-sensitive and must still run.
    buffer.organizations = []
    calls: list[dict[str, Any]] = []

    def _spy_sync(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"sent": 0, "failed": 0, "canceled": 0, "unchanged": 0, "stuck_pending": 0}

    monkeypatch.setattr(publisher, "sync", _spy_sync)

    # Act
    result = sync_handler_module.handler({}, None)

    # Assert
    assert len(calls) == 1
    assert result["sent"] == 0


def test_channel_refresh_failure_is_logged_as_a_warning(
    monkeypatch: pytest.MonkeyPatch, publisher: Publisher, buffer: FakeBuffer, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    buffer.organizations = []
    caplog.set_level(logging.WARNING, logger=sync_handler_module.logger.name)

    # Act
    sync_handler_module.handler({}, None)

    # Assert
    assert any(r.levelno == logging.WARNING for r in caplog.records if r.name == sync_handler_module.logger.name)


def test_get_settings_never_requests_the_scheduler_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: SyncFunction has no IAM grant for SCHEDULER_API_KEY (only
    # ApiFunction serves /schedule/* routes) — this is the exact
    # regression that took SyncFunction down in production (an unhandled
    # AccessDenied on ssm:GetParameter for that parameter). Bypass the
    # module-level `patch_publisher` fixture's own get_settings stub to
    # exercise the real implementation.
    monkeypatch.setattr(sync_handler_module, "get_settings", _real_get_settings)
    calls: list[dict[str, Any]] = []

    def _spy_build_settings(**kwargs: Any) -> Settings:
        calls.append(kwargs)
        return Settings(
            _env_file=None,
            buffer_api_key="k",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="k",
        )

    monkeypatch.setattr(sync_handler_module, "build_settings", _spy_build_settings)
    sync_handler_module._settings = None

    # Act
    sync_handler_module.get_settings()

    # Assert
    assert calls == [{"include_scheduler_key": False}]
