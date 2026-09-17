"""Supabase-backed audit trail: channels / submissions / posts.

Table/column names must match supabase/migrations/20260915000000_init_posting.sql
exactly (owned by another agent). They are centralized as module-level
constants below and in the Row models' field names, so a future schema
change only touches this file. Current shape: channels(id, service,
buffer_channel_id, name, display_name, is_disconnected, is_queue_paused,
created_at, updated_at), submissions(id, title, s3_key, video_url,
thumbnail_url, duration_sec, approved_at, created_at) — one approved
video+caption-set = one row, posts(id, submission_id, channel_id, caption,
mode, due_at, is_draft, platform_options jsonb, buffer_post_id, status,
external_link, sent_at, error_message, retry_count, created_at,
updated_at, unique(submission_id, channel_id)) — timing lives per post
since each target/channel can have its own schedule.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from sizuru_post.domain.models import PostStatus

# --- Schema surface: table & column names --------------------------------
# Every Supabase query below references these constants instead of inline
# string literals, so a rename only requires editing this block.

TABLE_CHANNELS = "channels"
TABLE_SUBMISSIONS = "submissions"
TABLE_POSTS = "posts"
TABLE_ADMIN_USERS = "admin_users"

COLUMN_ID = "id"
COLUMN_SUBMISSION_ID = "submission_id"
COLUMN_STATUS = "status"

CHANNELS_UPSERT_CONFLICT = "buffer_channel_id"
POSTS_UPSERT_CONFLICT = "submission_id,channel_id"

# The `channels.service` column is a Postgres enum (tiktok|instagram|x); a
# row with any other value would violate the DB constraint, so we filter
# before upserting rather than letting Supabase reject the whole batch.
VALID_CHANNEL_SERVICES = {"tiktok", "instagram", "x"}


class ChannelRow(BaseModel):
    id: UUID
    service: str
    buffer_channel_id: str
    name: str
    display_name: str | None = None
    is_disconnected: bool
    is_queue_paused: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SubmissionRow(BaseModel):
    id: UUID
    title: str | None = None
    s3_key: str
    video_url: str
    thumbnail_url: str | None = None
    duration_sec: float | None = None
    approved_at: datetime
    created_at: datetime | None = None


class PostRow(BaseModel):
    id: UUID
    submission_id: UUID
    channel_id: UUID
    caption: str
    mode: str
    due_at: datetime | None = None
    is_draft: bool = False
    platform_options: dict[str, Any] = Field(default_factory=dict)
    buffer_post_id: str | None = None
    status: PostStatus
    external_link: str | None = None
    sent_at: datetime | None = None
    error_message: str | None = None
    retry_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Repo(Protocol):
    """Port used by Publisher. SupabaseRepo implements it against Supabase;
    tests use an in-memory fake."""

    def upsert_channels(self, channels: list[dict[str, Any]]) -> None: ...

    def list_channels(self) -> list[ChannelRow]: ...

    def create_submission(
        self,
        *,
        title: str | None,
        s3_key: str,
        video_url: str,
        thumbnail_url: str | None,
        duration_sec: float | None,
        approved_at: datetime,
    ) -> UUID: ...

    def get_submission(self, submission_id: UUID) -> SubmissionRow | None: ...

    def upsert_post_pending(
        self,
        *,
        submission_id: UUID,
        channel_id: UUID,
        caption: str,
        mode: str,
        due_at: datetime | None,
        is_draft: bool,
        platform_options: dict[str, Any],
    ) -> PostRow: ...

    def update_post(self, post_id: UUID, **fields: Any) -> None: ...

    def claim_failed_post(self, post_id: UUID) -> PostRow | None: ...

    def get_post(self, post_id: UUID) -> PostRow | None: ...

    def list_posts_by_submission(self, submission_id: UUID) -> list[PostRow]: ...

    def list_syncable_posts(self) -> list[PostRow]: ...

    def list_pending_posts(self) -> list[PostRow]: ...

    def is_admin(self, user_id: str) -> bool: ...


def _serialize(fields: dict[str, Any]) -> dict[str, Any]:
    """Convert domain values (enums, UUIDs, datetimes) to JSON-safe primitives."""
    serialized: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, PostStatus):
            serialized[key] = value.value
        elif isinstance(value, datetime):
            serialized[key] = value.isoformat()
        elif isinstance(value, UUID):
            serialized[key] = str(value)
        else:
            serialized[key] = value
    return serialized


class SupabaseRepo:
    def __init__(self, client: Any) -> None:
        self._client = client

    def upsert_channels(self, channels: list[dict[str, Any]]) -> None:
        valid = [c for c in channels if c.get("service") in VALID_CHANNEL_SERVICES]
        if not valid:
            return
        self._client.table(TABLE_CHANNELS).upsert(valid, on_conflict=CHANNELS_UPSERT_CONFLICT).execute()

    def list_channels(self) -> list[ChannelRow]:
        response = self._client.table(TABLE_CHANNELS).select("*").execute()
        return [ChannelRow.model_validate(row) for row in response.data or []]

    def create_submission(
        self,
        *,
        title: str | None,
        s3_key: str,
        video_url: str,
        thumbnail_url: str | None,
        duration_sec: float | None,
        approved_at: datetime,
    ) -> UUID:
        payload = _serialize(
            {
                "title": title,
                "s3_key": s3_key,
                "video_url": video_url,
                "thumbnail_url": thumbnail_url,
                "duration_sec": duration_sec,
                "approved_at": approved_at,
            }
        )
        response = self._client.table(TABLE_SUBMISSIONS).insert(payload).execute()
        return UUID(str(response.data[0][COLUMN_ID]))

    def get_submission(self, submission_id: UUID) -> SubmissionRow | None:
        response = (
            self._client.table(TABLE_SUBMISSIONS).select("*").eq(COLUMN_ID, str(submission_id)).execute()
        )
        rows = response.data or []
        return SubmissionRow.model_validate(rows[0]) if rows else None

    def upsert_post_pending(
        self,
        *,
        submission_id: UUID,
        channel_id: UUID,
        caption: str,
        mode: str,
        due_at: datetime | None,
        is_draft: bool,
        platform_options: dict[str, Any],
    ) -> PostRow:
        payload = _serialize(
            {
                "submission_id": submission_id,
                "channel_id": channel_id,
                "caption": caption,
                "mode": mode,
                "due_at": due_at,
                "is_draft": is_draft,
                "platform_options": platform_options,
                "status": PostStatus.PENDING,
            }
        )
        response = (
            self._client.table(TABLE_POSTS).upsert(payload, on_conflict=POSTS_UPSERT_CONFLICT).execute()
        )
        return PostRow.model_validate(response.data[0])

    def update_post(self, post_id: UUID, **fields: Any) -> None:
        payload = _serialize(fields)
        self._client.table(TABLE_POSTS).update(payload).eq(COLUMN_ID, str(post_id)).execute()

    def claim_failed_post(self, post_id: UUID) -> PostRow | None:
        """Atomically transition failed->pending, guarded by WHERE status='failed'.

        Returns the updated row only if THIS call performed the
        transition (i.e. the row was actually 'failed'); returns None if
        another caller already claimed it (or it isn't failed), so
        concurrent retries of the same post can't both proceed.
        """
        response = (
            self._client.table(TABLE_POSTS)
            .update({COLUMN_STATUS: PostStatus.PENDING.value})
            .eq(COLUMN_ID, str(post_id))
            .eq(COLUMN_STATUS, PostStatus.FAILED.value)
            .execute()
        )
        rows = response.data or []
        return PostRow.model_validate(rows[0]) if rows else None

    def get_post(self, post_id: UUID) -> PostRow | None:
        response = self._client.table(TABLE_POSTS).select("*").eq(COLUMN_ID, str(post_id)).execute()
        rows = response.data or []
        return PostRow.model_validate(rows[0]) if rows else None

    def list_posts_by_submission(self, submission_id: UUID) -> list[PostRow]:
        response = (
            self._client.table(TABLE_POSTS).select("*").eq(COLUMN_SUBMISSION_ID, str(submission_id)).execute()
        )
        return [PostRow.model_validate(row) for row in response.data or []]

    def list_syncable_posts(self) -> list[PostRow]:
        """scheduled + draft — both can be repaired/reconciled from Buffer
        (see Publisher.sync); pending/failed/sent/canceled are not."""
        response = (
            self._client.table(TABLE_POSTS)
            .select("*")
            .in_(COLUMN_STATUS, [PostStatus.SCHEDULED.value, PostStatus.DRAFT.value])
            .execute()
        )
        return [PostRow.model_validate(row) for row in response.data or []]

    def list_pending_posts(self) -> list[PostRow]:
        return self._list_posts_by_status(PostStatus.PENDING)

    def _list_posts_by_status(self, status: PostStatus) -> list[PostRow]:
        response = self._client.table(TABLE_POSTS).select("*").eq(COLUMN_STATUS, status.value).execute()
        return [PostRow.model_validate(row) for row in response.data or []]

    def is_admin(self, user_id: str) -> bool:
        response = self._client.table(TABLE_ADMIN_USERS).select("user_id").eq("user_id", user_id).execute()
        return bool(response.data)
