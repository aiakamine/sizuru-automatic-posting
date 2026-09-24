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

from postgrest.exceptions import APIError
from pydantic import BaseModel, Field
from supabase_auth.errors import AuthApiError

from sizuru_post.domain.models import PostStatus

# The Postgres SQLSTATE for a unique-constraint violation, surfaced by
# PostgREST as APIError.code — see create_submission()/UniqueViolationError.
_UNIQUE_VIOLATION_CODE = "23505"

# Supabase Admin Auth API error codes seen for a duplicate-email
# auth.admin.create_user() call — see SupabaseRepo.create_auth_user().
# "email_exists" is what Supabase's admin create-user endpoint actually
# returns as of this writing; "user_already_exists" is kept too since it's
# the code used by the (differently-triggered) public signup endpoint and
# the exact string isn't part of any documented contract.
_AUTH_EMAIL_EXISTS_CODES = {"email_exists", "user_already_exists"}


class UniqueViolationError(ValueError):
    """create_submission(): a concurrent request already inserted a
    submission with this s3_key (submissions.s3_key is UNIQUE) — the
    caller (Publisher.publish_uploaded) should re-fetch it via
    get_submission_by_s3_key() instead of treating this as a failure. A
    ValueError subclass so any existing broad `except ValueError` call
    site still behaves sanely if it doesn't specifically handle this."""


class AuthUserAlreadyExistsError(ValueError):
    """create_auth_user(): Supabase already has an auth user with this
    email — the caller (admin_accounts.create_admin_account) must not
    overwrite/reset it."""

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
COLUMN_S3_KEY = "s3_key"
COLUMN_DUE_AT = "due_at"

# list_posts_in_range() excludes these — canceled/failed posts aren't
# candidates for the scheduling skill's "what slots are taken" query.
_RANGE_EXCLUDED_STATUSES = [PostStatus.CANCELED.value, PostStatus.FAILED.value]

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


class PostInRange(BaseModel):
    """A row from `list_posts_in_range()`: just enough for the scheduling
    skill to show "what's already taken" — not a full PostRow."""

    post_id: UUID
    submission_id: UUID
    service: str
    due_at: datetime
    status: str
    title: str | None = None


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


class AdminUserRow(BaseModel):
    user_id: UUID
    email: str
    display_name: str | None = None
    created_at: datetime | None = None


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

    def get_submission_by_s3_key(self, s3_key: str) -> SubmissionRow | None: ...

    def list_posts_in_range(self, start: datetime, end: datetime) -> list[PostInRange]: ...

    def claim_pending_post(
        self,
        *,
        submission_id: UUID,
        channel_id: UUID,
        caption: str,
        mode: str,
        due_at: datetime | None,
        is_draft: bool,
        platform_options: dict[str, Any],
    ) -> tuple[PostRow, bool]: ...

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


def _row_to_post_in_range(row: dict[str, Any]) -> PostInRange:
    """Flatten a list_posts_in_range() row's embedded `channels`/`submissions`
    objects (PostgREST returns a many-to-one embed as a single object, not
    an array) into a PostInRange."""
    channel = row.get("channels") or {}
    submission = row.get("submissions") or {}
    return PostInRange(
        post_id=row[COLUMN_ID],
        submission_id=row[COLUMN_SUBMISSION_ID],
        service=channel.get("service", ""),
        due_at=row[COLUMN_DUE_AT],
        status=row[COLUMN_STATUS],
        title=submission.get("title"),
    )


class SupabaseRepo:
    def __init__(self, client: Any) -> None:
        self._client = client

    def upsert_channels(self, channels: list[dict[str, Any]]) -> None:
        """Upsert Buffer's current channel list, and mark every DB channel
        whose buffer_channel_id is NOT in it as disconnected (a channel
        missing entirely from a successful, non-empty fetch means Buffer
        no longer has it — e.g. it was replaced). A channel that
        reappears later is reconnected automatically by the upsert above,
        which writes its is_disconnected straight from Buffer's response.

        The `if not valid: return` guard is also what keeps this from
        ever mass-disconnecting every channel on an empty/failed fetch —
        this method simply isn't called with anything to diff against.
        """
        valid = [c for c in channels if c.get("service") in VALID_CHANNEL_SERVICES]
        if not valid:
            return
        self._client.table(TABLE_CHANNELS).upsert(valid, on_conflict=CHANNELS_UPSERT_CONFLICT).execute()
        buffer_channel_ids = [c[CHANNELS_UPSERT_CONFLICT] for c in valid]
        (
            self._client.table(TABLE_CHANNELS)
            .update({"is_disconnected": True})
            .not_.in_(CHANNELS_UPSERT_CONFLICT, buffer_channel_ids)
            .execute()
        )

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
        try:
            response = self._client.table(TABLE_SUBMISSIONS).insert(payload).execute()
        except APIError as exc:
            if exc.code == _UNIQUE_VIOLATION_CODE:
                # A concurrent publish_uploaded() call for the same
                # video's s3_key won the race — the caller re-fetches via
                # get_submission_by_s3_key() instead.
                raise UniqueViolationError(f"s3_key は既に存在します: {s3_key}") from exc
            raise
        return UUID(str(response.data[0][COLUMN_ID]))

    def get_submission(self, submission_id: UUID) -> SubmissionRow | None:
        response = (
            self._client.table(TABLE_SUBMISSIONS).select("*").eq(COLUMN_ID, str(submission_id)).execute()
        )
        rows = response.data or []
        return SubmissionRow.model_validate(rows[0]) if rows else None

    def get_submission_by_s3_key(self, s3_key: str) -> SubmissionRow | None:
        """The idempotency lookup for `Publisher.publish_uploaded`:
        `submissions.s3_key` is UNIQUE, so a resent scheduling request with
        the same key must reuse this row instead of inserting a duplicate."""
        response = (
            self._client.table(TABLE_SUBMISSIONS).select("*").eq(COLUMN_S3_KEY, s3_key).execute()
        )
        rows = response.data or []
        return SubmissionRow.model_validate(rows[0]) if rows else None

    def list_posts_in_range(self, start: datetime, end: datetime) -> list[PostInRange]:
        """Posts due in `[start, end)`, with just enough submission/channel
        context for the scheduling skill's "what's already taken" query —
        canceled/failed and due_at=NULL posts are never candidates."""
        response = (
            self._client.table(TABLE_POSTS)
            .select(f"{COLUMN_ID},{COLUMN_SUBMISSION_ID},{COLUMN_DUE_AT},{COLUMN_STATUS},"
                    "channels(service),submissions(title)")
            .gte(COLUMN_DUE_AT, start.isoformat())
            .lt(COLUMN_DUE_AT, end.isoformat())
            .not_.in_(COLUMN_STATUS, _RANGE_EXCLUDED_STATUSES)
            .order(COLUMN_DUE_AT)
            .order(COLUMN_ID)
            .execute()
        )
        return [_row_to_post_in_range(row) for row in response.data or []]

    def claim_pending_post(
        self,
        *,
        submission_id: UUID,
        channel_id: UUID,
        caption: str,
        mode: str,
        due_at: datetime | None,
        is_draft: bool,
        platform_options: dict[str, Any],
    ) -> tuple[PostRow, bool]:
        """Atomically create the (submission_id, channel_id) post row, or
        return the existing one if a concurrent caller already claimed it
        first — `created` is True only when THIS call performed the
        insert, so Publisher._publish_one knows it's the one that should
        actually call Buffer (see that method).

        `ignore_duplicates=True` maps to `ON CONFLICT ... DO NOTHING`,
        which — unlike the plain upsert this replaced — never lets two
        concurrent callers both "win" and both dispatch to Buffer. Postgres
        returns NO row at all for a conflicting insert (not the existing
        row), so a conflict is re-selected explicitly rather than assumed
        from an empty response.
        """
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
            self._client.table(TABLE_POSTS)
            .upsert(payload, on_conflict=POSTS_UPSERT_CONFLICT, ignore_duplicates=True)
            .execute()
        )
        if response.data:
            return PostRow.model_validate(response.data[0]), True
        existing = self._get_post_by_submission_and_channel(submission_id, channel_id)
        if existing is None:
            # The conflict itself guarantees a row exists; a missing one
            # here would mean this state was observed inconsistently
            # (e.g. read-replica lag) — surface that loudly rather than
            # silently treating it as "created".
            raise RuntimeError(
                f"claim_pending_post: 競合を検出しましたが該当行が見つかりません"
                f"（submission_id={submission_id}, channel_id={channel_id}）"
            )
        return existing, False

    def _get_post_by_submission_and_channel(self, submission_id: UUID, channel_id: UUID) -> PostRow | None:
        response = (
            self._client.table(TABLE_POSTS)
            .select("*")
            .eq(COLUMN_SUBMISSION_ID, str(submission_id))
            .eq("channel_id", str(channel_id))
            .execute()
        )
        rows = response.data or []
        return PostRow.model_validate(rows[0]) if rows else None

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

    # --- admin account management (sizuru-post admin ...) -----------------
    # See sizuru_post.admin_accounts for the id/email/password rules and the
    # create/reset/list orchestration that calls these.

    def create_auth_user(self, *, email: str, password: str) -> UUID:
        try:
            response = self._client.auth.admin.create_user(
                {"email": email, "password": password, "email_confirm": True}
            )
        except AuthApiError as exc:
            if exc.code in _AUTH_EMAIL_EXISTS_CODES:
                raise AuthUserAlreadyExistsError(f"既にアカウントが存在します: {email}") from exc
            raise
        return UUID(str(response.user.id))

    def delete_auth_user(self, user_id: UUID) -> None:
        self._client.auth.admin.delete_user(str(user_id))

    def insert_admin_user(self, *, user_id: UUID, email: str, display_name: str | None) -> None:
        payload = _serialize({"user_id": user_id, "email": email, "display_name": display_name})
        self._client.table(TABLE_ADMIN_USERS).insert(payload).execute()

    def find_admin_user_id_by_email(self, email: str) -> UUID | None:
        """Resolve an email to its `admin_users.user_id` — deliberately
        NOT a scan of the whole auth.users directory (auth.admin.list_users):
        `reset_admin_password` must only ever be able to reset an account
        that's actually in `admin_users`, never an arbitrary auth user."""
        response = self._client.table(TABLE_ADMIN_USERS).select("user_id").eq("email", email).execute()
        rows = response.data or []
        return UUID(str(rows[0]["user_id"])) if rows else None

    def update_auth_user_password(self, user_id: UUID, password: str) -> None:
        self._client.auth.admin.update_user_by_id(str(user_id), {"password": password})

    def list_admin_users(self) -> list[AdminUserRow]:
        response = self._client.table(TABLE_ADMIN_USERS).select("*").execute()
        return [AdminUserRow.model_validate(row) for row in response.data or []]
