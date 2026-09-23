"""Tests for sizuru_post.channels_sync: the Buffer-channels -> Supabase
sync shared by cli.py's `channels` command and api/sync_handler.py's
best-effort channel refresh.
"""

from __future__ import annotations

import pytest

from sizuru_post.channels_sync import NoBufferOrganizationError, sync_channels
from tests.fakes import FakeBuffer, InMemoryRepo


def test_uses_given_organization_id_without_calling_get_organizations() -> None:
    # Arrange
    buffer = FakeBuffer(channels_by_organization={"org-1": [{"id": "c1", "service": "tiktok", "name": "n1"}]})
    repo = InMemoryRepo()

    # Act
    raw = sync_channels(buffer, repo, "org-1")

    # Assert
    assert buffer.get_channels_calls == ["org-1"]
    assert raw == [{"id": "c1", "service": "tiktok", "name": "n1"}]


def test_resolves_organization_id_from_buffer_when_not_given() -> None:
    # Arrange
    buffer = FakeBuffer(
        organizations=[{"id": "org-1"}, {"id": "org-2"}],
        channels_by_organization={"org-1": [{"id": "c1", "service": "tiktok", "name": "n1"}]},
    )
    repo = InMemoryRepo()

    # Act
    raw = sync_channels(buffer, repo, None)

    # Assert: the first organization Buffer returns
    assert buffer.get_channels_calls == ["org-1"]
    assert raw == [{"id": "c1", "service": "tiktok", "name": "n1"}]


def test_raises_when_no_organization_id_given_and_buffer_has_none() -> None:
    # Arrange
    buffer = FakeBuffer(organizations=[])
    repo = InMemoryRepo()

    # Act / Assert
    with pytest.raises(NoBufferOrganizationError):
        sync_channels(buffer, repo, None)
    assert buffer.get_channels_calls == []


def test_upserts_channels_with_expected_field_mapping() -> None:
    # Arrange
    buffer = FakeBuffer(
        channels_by_organization={
            "org-1": [
                {
                    "id": "c1",
                    "service": "tiktok",
                    "name": "n1",
                    "displayName": "Display One",
                    "isDisconnected": False,
                    "isQueuePaused": True,
                }
            ]
        }
    )
    repo = InMemoryRepo()

    # Act
    sync_channels(buffer, repo, "org-1")

    # Assert
    channel = repo.list_channels()[0]
    assert channel.buffer_channel_id == "c1"
    assert channel.service == "tiktok"
    assert channel.display_name == "Display One"
    assert channel.is_disconnected is False
    assert channel.is_queue_paused is True


def test_missing_channel_from_a_new_sync_is_marked_disconnected() -> None:
    # Arrange: the exact root-cause scenario — a channel that no longer
    # appears in Buffer's list at all (replaced by a new one) must be
    # flagged disconnected, not left as-is.
    buffer = FakeBuffer(channels_by_organization={"org-1": [{"id": "c1", "service": "x", "name": "n1"}]})
    repo = InMemoryRepo()
    sync_channels(buffer, repo, "org-1")
    stale_channel = next(c for c in repo.list_channels() if c.buffer_channel_id == "c1")
    assert stale_channel.is_disconnected is False

    buffer.channels_by_organization["org-1"] = [{"id": "c2", "service": "x", "name": "n2"}]

    # Act
    sync_channels(buffer, repo, "org-1")

    # Assert
    channels_by_id = {c.buffer_channel_id: c for c in repo.list_channels()}
    assert channels_by_id["c1"].is_disconnected is True
    assert channels_by_id["c2"].is_disconnected is False


def test_reappearing_channel_is_reconnected() -> None:
    # Arrange: c1 goes missing (disconnected), then reappears.
    buffer = FakeBuffer(channels_by_organization={"org-1": [{"id": "c1", "service": "x", "name": "n1"}]})
    repo = InMemoryRepo()
    sync_channels(buffer, repo, "org-1")
    buffer.channels_by_organization["org-1"] = [{"id": "c2", "service": "x", "name": "n2"}]
    sync_channels(buffer, repo, "org-1")
    assert next(c for c in repo.list_channels() if c.buffer_channel_id == "c1").is_disconnected is True

    buffer.channels_by_organization["org-1"] = [
        {"id": "c1", "service": "x", "name": "n1"},
        {"id": "c2", "service": "x", "name": "n2"},
    ]

    # Act
    sync_channels(buffer, repo, "org-1")

    # Assert
    assert next(c for c in repo.list_channels() if c.buffer_channel_id == "c1").is_disconnected is False
