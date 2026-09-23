"""In-memory test doubles for the Buffer/Storage/Repo ports used by Publisher."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sizuru_post.adapters.buffer import CreatePostResult
from sizuru_post.adapters.repo import (
    VALID_CHANNEL_SERVICES,
    ChannelRow,
    PostInRange,
    PostRow,
    SubmissionRow,
    UniqueViolationError,
)
from sizuru_post.adapters.storage import StoredObject
from sizuru_post.domain.models import PostStatus, PublishRequest, Target


class InMemoryRepo:
    """A fake Repo backed by dicts, for testing Publisher without a network."""

    def __init__(
        self, channels: list[ChannelRow] | None = None, admin_user_ids: set[str] | None = None
    ) -> None:
        self._channels: dict[UUID, ChannelRow] = {c.id: c for c in (channels or [])}
        self._submissions: dict[UUID, SubmissionRow] = {}
        self._posts: dict[UUID, PostRow] = {}
        self._admin_user_ids: set[str] = admin_user_ids or set()

    def upsert_channels(self, channels: list[dict[str, Any]]) -> None:
        valid = [c for c in channels if c.get("service") in VALID_CHANNEL_SERVICES]
        if not valid:
            return
        seen_buffer_channel_ids: set[str] = set()
        for row in valid:
            buffer_channel_id = row["buffer_channel_id"]
            seen_buffer_channel_ids.add(buffer_channel_id)
            existing = next(
                (c for c in self._channels.values() if c.buffer_channel_id == buffer_channel_id), None
            )
            payload = dict(row)
            payload.setdefault("id", existing.id if existing is not None else uuid4())
            channel = ChannelRow.model_validate(payload)
            self._channels[channel.id] = channel
        # Mirrors SupabaseRepo.upsert_channels: anything missing from this
        # batch is no longer in Buffer's list, so mark it disconnected.
        for channel_id, channel in list(self._channels.items()):
            if channel.buffer_channel_id not in seen_buffer_channel_ids:
                self._channels[channel_id] = channel.model_copy(update={"is_disconnected": True})

    def list_channels(self) -> list[ChannelRow]:
        return list(self._channels.values())

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
        # Mirrors the real DB's `submissions.s3_key` UNIQUE constraint
        # (and SupabaseRepo's matching 23505 handling): callers must check
        # get_submission_by_s3_key() first (see Publisher.publish_uploaded)
        # rather than relying on this to silently dedupe.
        if any(s.s3_key == s3_key for s in self._submissions.values()):
            raise UniqueViolationError(f"s3_key は一意である必要があります（重複）: {s3_key}")
        submission_id = uuid4()
        self._submissions[submission_id] = SubmissionRow(
            id=submission_id,
            title=title,
            s3_key=s3_key,
            video_url=video_url,
            thumbnail_url=thumbnail_url,
            duration_sec=duration_sec,
            approved_at=approved_at,
        )
        return submission_id

    def get_submission(self, submission_id: UUID) -> SubmissionRow | None:
        return self._submissions.get(submission_id)

    def get_submission_by_s3_key(self, s3_key: str) -> SubmissionRow | None:
        return next((s for s in self._submissions.values() if s.s3_key == s3_key), None)

    def list_posts_in_range(self, start: datetime, end: datetime) -> list[PostInRange]:
        excluded = {PostStatus.CANCELED, PostStatus.FAILED}
        rows = [
            p
            for p in self._posts.values()
            if p.due_at is not None and start <= p.due_at < end and p.status not in excluded
        ]
        rows.sort(key=lambda p: (p.due_at, p.id))  # type: ignore[union-attr, arg-type]
        return [
            PostInRange(
                post_id=p.id,
                submission_id=p.submission_id,
                service=self._channels[p.channel_id].service,
                due_at=p.due_at,  # type: ignore[arg-type]
                status=p.status.value,
                title=self._submissions[p.submission_id].title,
            )
            for p in rows
        ]

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
        existing = next(
            (
                p
                for p in self._posts.values()
                if p.submission_id == submission_id and p.channel_id == channel_id
            ),
            None,
        )
        if existing is not None:
            return existing, False
        post_id = uuid4()
        row = PostRow(
            id=post_id,
            submission_id=submission_id,
            channel_id=channel_id,
            caption=caption,
            mode=mode,
            due_at=due_at,
            is_draft=is_draft,
            platform_options=platform_options,
            status=PostStatus.PENDING,
        )
        self._posts[post_id] = row
        return row, True

    def update_post(self, post_id: UUID, **fields: Any) -> None:
        row = self._posts[post_id]
        self._posts[post_id] = row.model_copy(update=fields)

    def claim_failed_post(self, post_id: UUID) -> PostRow | None:
        row = self._posts.get(post_id)
        if row is None or row.status != PostStatus.FAILED:
            return None
        claimed = row.model_copy(update={"status": PostStatus.PENDING})
        self._posts[post_id] = claimed
        return claimed

    def get_post(self, post_id: UUID) -> PostRow | None:
        return self._posts.get(post_id)

    def list_posts_by_submission(self, submission_id: UUID) -> list[PostRow]:
        return [p for p in self._posts.values() if p.submission_id == submission_id]

    def list_syncable_posts(self) -> list[PostRow]:
        return [p for p in self._posts.values() if p.status in (PostStatus.SCHEDULED, PostStatus.DRAFT)]

    def list_pending_posts(self) -> list[PostRow]:
        return [p for p in self._posts.values() if p.status == PostStatus.PENDING]

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admin_user_ids


@dataclass
class FakeBuffer:
    """Fake Buffer port. Configure `results`/`exceptions` per service before use."""

    results: dict[str, CreatePostResult] = field(default_factory=dict)
    exceptions: dict[str, Exception] = field(default_factory=dict)
    posts: dict[str, dict[str, Any]] = field(default_factory=dict)
    get_post_exceptions: dict[str, Exception] = field(default_factory=dict)
    get_post_calls: list[str] = field(default_factory=list)
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    requests: list[PublishRequest] = field(default_factory=list)
    edit_results: dict[str, CreatePostResult] = field(default_factory=dict)
    delete_results: dict[str, CreatePostResult] = field(default_factory=dict)
    edit_exceptions: dict[str, Exception] = field(default_factory=dict)
    delete_exceptions: dict[str, Exception] = field(default_factory=dict)
    edit_calls: list[tuple[str, str | None, Any]] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)
    organizations: list[dict[str, Any]] = field(default_factory=list)
    channels_by_organization: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    get_organizations_exception: Exception | None = None
    get_channels_exception: Exception | None = None
    get_channels_calls: list[str] = field(default_factory=list)

    def create_post(
        self, request: PublishRequest, target: Target, channel_id: str, video_url: str
    ) -> CreatePostResult:
        self.calls.append((target.service, channel_id, video_url))
        self.requests.append(request)
        if target.service in self.exceptions:
            raise self.exceptions[target.service]
        return self.results.get(
            target.service,
            CreatePostResult(ok=True, post_id=f"post-{target.service}", status="scheduled"),
        )

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        self.get_post_calls.append(post_id)
        if post_id in self.get_post_exceptions:
            raise self.get_post_exceptions[post_id]
        return self.posts.get(post_id)

    def edit_post(
        self, post_id: str, *, text: str | None = None, due_at: datetime | None = None
    ) -> CreatePostResult:
        self.edit_calls.append((post_id, text, due_at))
        if post_id in self.edit_exceptions:
            raise self.edit_exceptions[post_id]
        return self.edit_results.get(post_id, CreatePostResult(ok=True, post_id=post_id, status="scheduled"))

    def delete_post(self, post_id: str) -> CreatePostResult:
        self.delete_calls.append(post_id)
        if post_id in self.delete_exceptions:
            raise self.delete_exceptions[post_id]
        return self.delete_results.get(post_id, CreatePostResult(ok=True, post_id=post_id, status="canceled"))

    def get_organizations(self) -> list[dict[str, Any]]:
        if self.get_organizations_exception is not None:
            raise self.get_organizations_exception
        return self.organizations

    def get_channels(self, organization_id: str) -> list[dict[str, Any]]:
        self.get_channels_calls.append(organization_id)
        if self.get_channels_exception is not None:
            raise self.get_channels_exception
        return self.channels_by_organization.get(organization_id, [])


@dataclass
class FakeStorage:
    """Fake Storage port. Set `video_exception`/`thumbnail_exception` to simulate failures."""

    video: StoredObject = field(
        default_factory=lambda: StoredObject(
            key="public/videos/x.mp4", public_url="https://cdn.example.com/videos/x.mp4"
        )
    )
    thumbnail: StoredObject = field(
        default_factory=lambda: StoredObject(
            key="public/thumbnails/x.jpg", public_url="https://cdn.example.com/thumbnails/x.jpg"
        )
    )
    video_exception: Exception | None = None
    thumbnail_exception: Exception | None = None
    video_calls: list[Path] = field(default_factory=list)
    thumbnail_calls: list[Path] = field(default_factory=list)

    def upload_video(self, path: Path) -> StoredObject:
        self.video_calls.append(path)
        if self.video_exception is not None:
            raise self.video_exception
        return self.video

    def upload_thumbnail(self, path: Path) -> StoredObject:
        self.thumbnail_calls.append(path)
        if self.thumbnail_exception is not None:
            raise self.thumbnail_exception
        return self.thumbnail
