"""Shared Buffer-channels -> Supabase sync logic.

Used by both the CLI's `channels` command (cli.py) and the periodic
SyncFunction Lambda's best-effort channel refresh (api/sync_handler.py) —
kept in one place so both stay consistent with each other and with
Repo.upsert_channels's "mark anything missing as disconnected" behavior
(the root cause of a live incident this module exists to prevent
recurring: a channel replaced in Buffer was never flagged disconnected in
Supabase because nothing besides `channels` ever ran the sync).
"""

from __future__ import annotations

from typing import Any, Protocol


class _BufferPort(Protocol):
    def get_organizations(self) -> list[dict[str, Any]]: ...

    def get_channels(self, organization_id: str) -> list[dict[str, Any]]: ...


class _RepoPort(Protocol):
    def upsert_channels(self, channels: list[dict[str, Any]]) -> None: ...


class NoBufferOrganizationError(RuntimeError):
    """No BUFFER_ORGANIZATION_ID configured and Buffer reports no
    organizations for this API key. Callers decide how to surface this
    (the CLI: a clear stderr message + exit 1; the SyncFunction: caught
    and logged, since a channel-refresh failure must not block the more
    time-sensitive post-status sync)."""


def sync_channels(
    buffer: _BufferPort, repo: _RepoPort, organization_id: str | None
) -> list[dict[str, Any]]:
    """Fetch Buffer's current channel list for `organization_id`
    (resolved from Buffer if not given) and upsert it into Supabase —
    including marking any DB channel missing from the list as
    disconnected (see Repo.upsert_channels). Returns the raw Buffer
    channel dicts (the CLI prints them; the Lambda ignores them)."""
    if organization_id is None:
        organizations = buffer.get_organizations()
        if not organizations:
            raise NoBufferOrganizationError(
                "Buffer組織が見つかりません。BUFFER_ORGANIZATION_ID を設定してください。"
            )
        organization_id = organizations[0]["id"]

    raw_channels = buffer.get_channels(organization_id)
    repo.upsert_channels(
        [
            {
                "buffer_channel_id": c["id"],
                "service": c.get("service"),
                "name": c.get("name"),
                "display_name": c.get("displayName") or c.get("name"),
                "is_disconnected": bool(c.get("isDisconnected", False)),
                "is_queue_paused": bool(c.get("isQueuePaused", False)),
            }
            for c in raw_channels
        ]
    )
    return raw_channels
