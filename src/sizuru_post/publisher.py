"""Orchestrates plan -> upload -> Buffer publish -> Supabase audit trail.

Buffer/Storage are typed as Protocols (ports) so this module is testable
with in-memory fakes; it never talks to httpx/boto3 directly.

Idempotency caveat: `publish()` always creates a new submission row for the
given video file, so re-running `publish()` on the same file publishes it
again. `retry()` is the supported way to resend only the posts that failed
for an existing submission.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sizuru_post.adapters.buffer import (
    BufferAmbiguousError,
    BufferAuthError,
    BufferProtocolError,
    CreatePostResult,
)
from sizuru_post.adapters.repo import ChannelRow, PostRow, Repo
from sizuru_post.adapters.storage import StoredObject
from sizuru_post.domain.models import PostStatus, PublishRequest, Target, Timing, VideoInfo, resolve_timing
from sizuru_post.domain.validate import (
    ValidationIssue,
    generate_thumbnail,
    probe_video,
    validate_caption,
    validate_video,
)

logger = logging.getLogger(__name__)


class ApprovalRequiredError(Exception):
    """Raised by publish()/retry() when called without human approval."""


class PostNotFoundError(ValueError):
    """edit()/cancel(): post_id (or its channel) doesn't correspond to any
    row. A ValueError subclass so existing `except ValueError`/
    `pytest.raises(ValueError)` call sites keep working unchanged; callers
    that need to distinguish it (e.g. the API handler, for a 404) can catch
    it specifically."""


class PostNotEditableError(ValueError):
    """edit()/cancel(): the post exists but isn't in an editable/cancelable
    state (wrong status, or within the near-due guard window)."""


class RequestValidationError(ValueError):
    """edit(): the caller-supplied caption/due_at is itself invalid (too
    long, naive, in the past, ...). A ValueError subclass for the same
    backward-compatibility reason as the other exceptions here; callers that
    need to distinguish an *expected* 400 from an unexpected error (e.g. the
    API handler) should catch this specifically rather than bare
    ValueError."""


class BufferRejectedError(ValueError):
    """edit()/cancel(): Buffer explicitly rejected the mutation (a
    MutationError response), e.g. the post was already sent."""


class BufferPort(Protocol):
    def create_post(
        self, request: PublishRequest, target: Target, channel_id: str, video_url: str
    ) -> CreatePostResult: ...

    def get_post(self, post_id: str) -> dict[str, Any] | None: ...

    def edit_post(
        self, post_id: str, *, text: str | None = None, due_at: datetime | None = None
    ) -> CreatePostResult: ...

    def delete_post(self, post_id: str) -> CreatePostResult: ...


class StoragePort(Protocol):
    def upload_video(self, path: Path) -> StoredObject: ...

    def upload_thumbnail(self, path: Path) -> StoredObject: ...


@dataclass
class PlannedTarget:
    target: Target
    channel: ChannelRow | None
    issues: list[ValidationIssue]

    @property
    def is_valid(self) -> bool:
        return self.channel is not None and not any(i.level == "error" for i in self.issues)


@dataclass
class PublishPlan:
    request: PublishRequest
    video: VideoInfo
    targets: list[PlannedTarget]

    @property
    def has_valid_target(self) -> bool:
        return any(t.is_valid for t in self.targets)


@dataclass
class TargetResult:
    service: str
    # scheduled | sent | draft | failed | canceled | skipped | unknown | db_write_failed
    status: str
    post_id: UUID | None = None
    """Our DB posts.id — what edit()/cancel() take. May exist even when
    buffer_post_id doesn't (e.g. pending/failed before Buffer ever
    created a post, or an ambiguous outcome)."""
    buffer_post_id: str | None = None
    """Buffer's own post id. None until Buffer has confirmed creating one."""
    due_at: str | None = None
    error: str | None = None
    issues: list[ValidationIssue] = field(default_factory=list)


@dataclass
class PublishReport:
    submission_id: UUID | None
    results: list[TargetResult]


_SETTLED_STATUSES = (PostStatus.SCHEDULED, PostStatus.SENT, PostStatus.DRAFT)
_EDITABLE_STATUSES = (PostStatus.SCHEDULED, PostStatus.DRAFT)
_NEAR_DUE_GUARD = timedelta(minutes=5)

# Buffer's real PostStatus enum: draft, error, needs_approval, scheduled,
# sending, sent. needs_approval/sending are still in flight -> scheduled.
_REMOTE_STATUS_TO_LOCAL: dict[str | None, PostStatus] = {
    "draft": PostStatus.DRAFT,
    "scheduled": PostStatus.SCHEDULED,
    "sending": PostStatus.SCHEDULED,
    "needs_approval": PostStatus.SCHEDULED,
    "sent": PostStatus.SENT,
    "error": PostStatus.FAILED,
}


def _error_message(message: str | None) -> str:
    """Fall back to a placeholder so we never write NULL for a failed post.

    The DB CHECK `posts_failed_has_error` requires error_message NOT NULL
    whenever status='failed'; a malformed upstream response (message=None)
    must not violate that constraint.
    """
    return message or "原因不明のエラー"


def _parse_buffer_datetime(value: str) -> datetime | None:
    """Parse a Buffer-supplied ISO8601 timestamp; returns None if unparsable
    rather than raising, since sync() must not abort a whole post over a
    single malformed date string."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _platform_options(request: PublishRequest, target: Target) -> dict[str, Any]:
    """Per-service extras (plus the shared thumbnail_offset_ms) to persist
    so a retry can resend identically.

    Kept separate from adapters.buffer.build_create_post_variables (the
    Buffer wire format) since this is a distinct contract: what we store
    in Supabase, not what we send to Buffer.
    """
    options: dict[str, Any] = {}
    if target.service == "instagram":
        options["share_to_feed"] = target.share_to_feed
    elif target.service == "tiktok" and target.tiktok_title:
        options["tiktok_title"] = target.tiktok_title

    if request.thumbnail_offset_ms is not None:
        options["thumbnail_offset_ms"] = request.thumbnail_offset_ms

    return options


class Publisher:
    def __init__(
        self,
        buffer: BufferPort,
        storage: StoragePort,
        repo: Repo,
        probe: Callable[[Path], VideoInfo] = probe_video,
        thumbnail: Callable[[Path, Path, int | None], None] = generate_thumbnail,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._buffer = buffer
        self._storage = storage
        self._repo = repo
        self._probe = probe
        self._thumbnail = thumbnail
        self._clock = clock

    # --- planning (no side effects) -----------------------------------------

    def plan(self, request: PublishRequest) -> PublishPlan:
        video = self._probe(request.video_path)
        channels = self._repo.list_channels()

        planned = [self._plan_target(target, video, channels) for target in request.targets]
        return PublishPlan(request=request, video=video, targets=planned)

    def _plan_target(self, target: Target, video: VideoInfo, channels: list[ChannelRow]) -> PlannedTarget:
        issues = validate_caption(target)
        issues.extend(validate_video(video, [target]))
        channel = self._resolve_channel(target, channels, issues)
        return PlannedTarget(target=target, channel=channel, issues=issues)

    def _resolve_channel(
        self, target: Target, channels: list[ChannelRow], issues: list[ValidationIssue]
    ) -> ChannelRow | None:
        if target.channel_id is not None:
            candidates = [
                c
                for c in channels
                if c.buffer_channel_id == target.channel_id and c.service == target.service
            ]
        else:
            candidates = [c for c in channels if c.service == target.service]

        if not candidates:
            message = f"{target.service}: 対応するチャンネルが見つかりません"
            issues.append(ValidationIssue(service=target.service, level="error", message=message))
            return None
        if len(candidates) > 1:
            message = f"{target.service}: 対応するチャンネルが複数あります。channel_id を指定してください"
            issues.append(ValidationIssue(service=target.service, level="error", message=message))
            return None

        channel = candidates[0]
        if channel.is_disconnected:
            message = f"{target.service}: チャンネルが連携解除されています"
            issues.append(ValidationIssue(service=target.service, level="error", message=message))
            return None
        if channel.is_queue_paused:
            message = f"{target.service}: チャンネルのキューが一時停止中です"
            issues.append(ValidationIssue(service=target.service, level="error", message=message))
            return None
        return channel

    # --- publish ---------------------------------------------------------------

    def publish(self, request: PublishRequest, approved: bool) -> PublishReport:
        if not approved:
            raise ApprovalRequiredError("公開には承認 (approved=True) が必要です")

        plan = self.plan(request)
        if not plan.has_valid_target:
            return PublishReport(submission_id=None, results=[self._skipped_result(t) for t in plan.targets])

        try:
            stored = self._storage.upload_video(request.video_path)
        except Exception as exc:  # noqa: BLE001 - upload failures must be reported, not raised
            return PublishReport(submission_id=None, results=self._upload_failure_results(plan, exc))

        thumbnail_url = self._make_thumbnail_url(request)

        submission_id = self._repo.create_submission(
            title=request.title,
            s3_key=stored.key,
            video_url=stored.public_url,
            thumbnail_url=thumbnail_url,
            duration_sec=plan.video.duration_sec,
            approved_at=datetime.now(UTC),
        )

        results = [
            self._publish_one(request, planned, submission_id, stored.public_url)
            if planned.is_valid
            else self._skipped_result(planned)
            for planned in plan.targets
        ]
        return PublishReport(submission_id=submission_id, results=results)

    def _make_thumbnail_url(self, request: PublishRequest) -> str | None:
        """Best-effort JPEG thumbnail; failures must never block posting."""
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                thumb_path = Path(tmp_dir) / "thumbnail.jpg"
                self._thumbnail(request.video_path, thumb_path, request.thumbnail_offset_ms)
                return self._storage.upload_thumbnail(thumb_path).public_url
        except Exception:  # noqa: BLE001 - thumbnail failure must not block posting
            logger.warning("サムネイル生成に失敗しました: %s", request.video_path, exc_info=True)
            return None

    def _upload_failure_results(self, plan: PublishPlan, exc: Exception) -> list[TargetResult]:
        # No post row exists yet — the upload happens before any post is created.
        error = f"動画のアップロードに失敗しました: {exc}"
        return [
            TargetResult(service=t.target.service, status="failed", error=error, issues=t.issues)
            if t.is_valid
            else self._skipped_result(t)
            for t in plan.targets
        ]

    def _skipped_result(self, planned: PlannedTarget) -> TargetResult:
        return TargetResult(service=planned.target.service, status="skipped", issues=planned.issues)

    def _publish_one(
        self, request: PublishRequest, planned: PlannedTarget, submission_id: UUID, video_url: str
    ) -> TargetResult:
        target = planned.target
        channel = planned.channel
        assert channel is not None  # noqa: S101 - guaranteed by is_valid check
        timing = resolve_timing(request, target)

        existing = next(
            (p for p in self._repo.list_posts_by_submission(submission_id) if p.channel_id == channel.id),
            None,
        )
        if existing is not None and existing.status in _SETTLED_STATUSES:
            return TargetResult(
                service=target.service,
                status=existing.status.value,
                post_id=existing.id,
                buffer_post_id=existing.buffer_post_id,
                issues=planned.issues,
            )

        row = self._repo.upsert_post_pending(
            submission_id=submission_id,
            channel_id=channel.id,
            caption=target.caption,
            mode=timing.mode,
            due_at=timing.due_at,
            is_draft=request.draft,
            platform_options=_platform_options(request, target),
        )

        try:
            result = self._buffer.create_post(request, target, channel.buffer_channel_id, video_url)
        except (BufferAmbiguousError, BufferProtocolError):
            # We don't know if Buffer actually created the post — a
            # malformed/undecodable "success" response is just as ambiguous
            # as a 5xx/transport error here, since it can only happen after
            # the mutation was already dispatched. Leave it `pending` (not
            # failed, which would invite an unsafe blind retry) and surface
            # "unknown" so a human checks Buffer.
            message = "送信結果が不明です。Bufferの画面で投稿の有無を確認してください"
            self._repo.update_post(row.id, error_message=message)
            return TargetResult(
                service=target.service, status="unknown", post_id=row.id, error=message, issues=planned.issues
            )
        except Exception as exc:  # noqa: BLE001 - one target's failure must not abort the others
            self._repo.update_post(row.id, status=PostStatus.FAILED, error_message=str(exc))
            return TargetResult(
                service=target.service, status="failed", post_id=row.id, error=str(exc), issues=planned.issues
            )

        if not result.ok:
            error = _error_message(result.error)
            self._repo.update_post(row.id, status=PostStatus.FAILED, error_message=error)
            return TargetResult(
                service=target.service, status="failed", post_id=row.id, error=error, issues=planned.issues
            )

        status = self._map_buffer_status(result.status, request.draft)
        update_fields = self._success_update_fields(status, result)
        return self._apply_success_update(
            row.id, update_fields, service=target.service, result=result, issues=planned.issues
        )

    @staticmethod
    def _success_update_fields(status: PostStatus, result: CreatePostResult) -> dict[str, Any]:
        """Fields to persist after a successful Buffer mutation, given the
        already-mapped local status. Buffer's immediate response can itself
        report status="error" (mapped to FAILED here) with an explanation —
        DB CHECK posts_failed_has_error requires that message be non-null."""
        update_fields: dict[str, Any] = {"status": status, "buffer_post_id": result.post_id}
        if result.due_at:
            update_fields["due_at"] = result.due_at
        if status == PostStatus.SENT:
            update_fields["sent_at"] = datetime.now(UTC)
        if status == PostStatus.FAILED:
            update_fields["error_message"] = _error_message(result.error)
        return update_fields

    def _apply_success_update(
        self,
        db_post_id: UUID,
        update_fields: dict[str, Any],
        *,
        service: str,
        result: CreatePostResult,
        issues: list[ValidationIssue],
    ) -> TargetResult:
        """Write the post-Buffer-success DB update. If Buffer already
        succeeded but our own write then fails, we must not lose either id
        or silently swallow the error — report it as its own status and log
        it, without aborting other targets/posts."""
        try:
            self._repo.update_post(db_post_id, **update_fields)
        except Exception as exc:  # noqa: BLE001 - Buffer already succeeded; don't lose the post_id
            logger.error(
                "Buffer成功後のDB更新に失敗しました（post_id=%s, buffer_post_id=%s）: %s",
                db_post_id,
                result.post_id,
                exc,
            )
            return TargetResult(
                service=service,
                status="db_write_failed",
                post_id=db_post_id,
                buffer_post_id=result.post_id,
                due_at=result.due_at,
                error=f"db_write_failed: {exc}",
                issues=issues,
            )
        status: PostStatus = update_fields["status"]
        return TargetResult(
            service=service,
            status=status.value,
            post_id=db_post_id,
            buffer_post_id=result.post_id,
            due_at=result.due_at,
            error=update_fields.get("error_message"),
            issues=issues,
        )

    @staticmethod
    def _map_buffer_status(buffer_status: str | None, draft: bool) -> PostStatus:
        """Map Buffer's real PostStatus enum (draft, error, needs_approval,
        scheduled, sending, sent) onto our local one. `draft` (our own
        request/post flag) takes priority, since Buffer's immediate response
        may not yet reflect it."""
        if draft:
            return PostStatus.DRAFT
        return _REMOTE_STATUS_TO_LOCAL.get(buffer_status, PostStatus.SCHEDULED)

    # --- retry -------------------------------------------------------------------

    def retry(self, submission_id: UUID, approved: bool) -> PublishReport:
        if not approved:
            raise ApprovalRequiredError("再送信には承認 (approved=True) が必要です")

        submission = self._repo.get_submission(submission_id)
        if submission is None:
            raise ValueError(f"submission が見つかりません: {submission_id}")

        channels_by_id = {c.id: c for c in self._repo.list_channels()}
        failed_posts = [
            p for p in self._repo.list_posts_by_submission(submission_id) if p.status == PostStatus.FAILED
        ]

        results = []
        for post in failed_posts:
            # Atomically claim failed->pending so a concurrent retry() call
            # (or this one running twice) can't both resend the same post.
            claimed = self._repo.claim_failed_post(post.id)
            if claimed is None:
                continue
            results.append(self._retry_one(claimed, submission.video_url, channels_by_id))
        return PublishReport(submission_id=submission_id, results=results)

    def _retry_one(
        self, post: PostRow, video_url: str, channels_by_id: dict[UUID, ChannelRow]
    ) -> TargetResult:
        channel = channels_by_id.get(post.channel_id)
        if channel is None:
            message = "チャンネルが見つかりません"
            self._repo.update_post(
                post.id, status=PostStatus.FAILED, retry_count=post.retry_count + 1, error_message=message
            )
            return TargetResult(
                service="unknown",
                status="failed",
                post_id=post.id,
                buffer_post_id=post.buffer_post_id,
                error=message,
            )

        if post.mode == "customScheduled" and post.due_at is not None and post.due_at <= datetime.now(UTC):
            message = "予定日時が過去のため再送信できません"
            self._repo.update_post(
                post.id, status=PostStatus.FAILED, retry_count=post.retry_count + 1, error_message=message
            )
            return TargetResult(
                service=channel.service,
                status="failed",
                post_id=post.id,
                buffer_post_id=post.buffer_post_id,
                error=message,
            )

        try:
            request, target = self._rebuild_retry_request(post, channel, video_url)
        except Exception as exc:  # noqa: BLE001 - a bad stored row must not abort the other posts
            message = f"再送信データの復元に失敗しました: {exc}"
            self._repo.update_post(
                post.id, status=PostStatus.FAILED, retry_count=post.retry_count + 1, error_message=message
            )
            return TargetResult(
                service=channel.service,
                status="failed",
                post_id=post.id,
                buffer_post_id=post.buffer_post_id,
                error=message,
            )

        try:
            result = self._buffer.create_post(request, target, channel.buffer_channel_id, video_url)
        except (BufferAmbiguousError, BufferProtocolError):
            # See _publish_one: a post-dispatch protocol error is just as
            # ambiguous as BufferAmbiguousError.
            message = "送信結果が不明です。Bufferの画面で投稿の有無を確認してください"
            self._repo.update_post(post.id, retry_count=post.retry_count + 1, error_message=message)
            return TargetResult(
                service=channel.service,
                status="unknown",
                post_id=post.id,
                buffer_post_id=post.buffer_post_id,
                error=message,
            )
        except Exception as exc:  # noqa: BLE001 - one target's failure must not abort the others
            self._repo.update_post(
                post.id, status=PostStatus.FAILED, retry_count=post.retry_count + 1, error_message=str(exc)
            )
            return TargetResult(
                service=channel.service,
                status="failed",
                post_id=post.id,
                buffer_post_id=post.buffer_post_id,
                error=str(exc),
            )

        if not result.ok:
            error = _error_message(result.error)
            self._repo.update_post(
                post.id, status=PostStatus.FAILED, retry_count=post.retry_count + 1, error_message=error
            )
            return TargetResult(
                service=channel.service,
                status="failed",
                post_id=post.id,
                buffer_post_id=post.buffer_post_id,
                error=error,
            )

        status = self._map_buffer_status(result.status, post.is_draft)
        update_fields: dict[str, Any] = {"retry_count": post.retry_count + 1, "error_message": None}
        update_fields.update(self._success_update_fields(status, result))
        return self._apply_success_update(
            post.id, update_fields, service=channel.service, result=result, issues=[]
        )

    @staticmethod
    def _rebuild_retry_request(
        post: PostRow, channel: ChannelRow, video_url: str
    ) -> tuple[PublishRequest, Target]:
        """Reconstruct the Target/PublishRequest needed to rebuild the Buffer
        payload from what was persisted at first-publish time.

        due_at is only set for customScheduled: Timing rejects a due_at on
        any other mode, and addToQueue/shareNow posts can have a due_at
        backfilled by sync() once Buffer assigns one (see Publisher.sync),
        so it must not be fed back in here.
        """
        # platform_options was persisted at first-publish time, so the retry
        # resends the exact same tiktok_title / share_to_feed / thumbnail
        # offset as before.
        target = Target(
            service=channel.service,
            caption=post.caption,
            tiktok_title=post.platform_options.get("tiktok_title"),
            share_to_feed=post.platform_options.get("share_to_feed", True),
        )
        timing = (
            Timing(mode=post.mode, due_at=post.due_at)
            if post.mode == "customScheduled"
            else Timing(mode=post.mode)
        )
        request = PublishRequest(
            video_path=Path(video_url),
            timing=timing,
            targets=[target],
            draft=post.is_draft,
            thumbnail_offset_ms=post.platform_options.get("thumbnail_offset_ms"),
        )
        return request, target

    # --- edit / cancel (also used by the future admin dashboard) -----------------

    def edit(
        self, post_id: UUID, *, caption: str | None = None, due_at: datetime | None = None
    ) -> TargetResult:
        post, channel = self._get_editable_post_and_channel(post_id)

        if caption is not None:
            errors = [
                i
                for i in validate_caption(Target(service=channel.service, caption=caption))
                if i.level == "error"
            ]
            if errors:
                raise RequestValidationError("; ".join(i.message for i in errors))

        if due_at is not None:
            if due_at.tzinfo is None:
                raise RequestValidationError("due_at はタイムゾーン情報を含む必要があります")
            if due_at <= datetime.now(UTC):
                raise RequestValidationError("due_at は未来の日時を指定してください")

        assert post.buffer_post_id is not None  # noqa: S101 - guaranteed by _EDITABLE_STATUSES + DB CHECK
        # BufferAmbiguousError/BufferProtocolError propagate as-is (not
        # wrapped): callers (the API handler) need to distinguish
        # "ambiguous" from other failures.
        result = self._buffer.edit_post(post.buffer_post_id, text=caption, due_at=due_at)
        if not result.ok:
            raise BufferRejectedError(_error_message(result.error))

        # Buffer's own response is the source of truth; only fall back to
        # what we requested when Buffer's response doesn't include a field.
        status = self._map_buffer_status(result.status, post.is_draft)
        resolved_caption = result.caption if result.caption is not None else caption
        resolved_due_at = _parse_buffer_datetime(result.due_at) if result.due_at else due_at

        update_fields: dict[str, Any] = {"status": status}
        if resolved_caption is not None:
            update_fields["caption"] = resolved_caption
        if resolved_due_at is not None:
            update_fields["due_at"] = resolved_due_at
        if status == PostStatus.SENT:
            update_fields["sent_at"] = datetime.now(UTC)
        if status == PostStatus.FAILED:
            update_fields["error_message"] = _error_message(result.error)

        return self._write_edit_result(
            post,
            update_fields,
            channel=channel,
            buffer_post_id=post.buffer_post_id,
            due_at_out=resolved_due_at.isoformat() if resolved_due_at else None,
            error=update_fields.get("error_message"),
        )

    def cancel(self, post_id: UUID) -> TargetResult:
        post, channel = self._get_editable_post_and_channel(post_id)
        assert post.buffer_post_id is not None  # noqa: S101 - guaranteed by _EDITABLE_STATUSES + DB CHECK

        # BufferAmbiguousError/BufferProtocolError propagate as-is (not
        # wrapped): callers (the API handler) need to distinguish
        # "ambiguous" from other failures.
        result = self._buffer.delete_post(post.buffer_post_id)
        if not result.ok:
            raise BufferRejectedError(_error_message(result.error))

        return self._write_edit_result(
            post,
            {"status": PostStatus.CANCELED},
            channel=channel,
            buffer_post_id=post.buffer_post_id,
            due_at_out=None,
            error=None,
        )

    def _write_edit_result(
        self,
        post: PostRow,
        update_fields: dict[str, Any],
        *,
        channel: ChannelRow,
        buffer_post_id: str,
        due_at_out: str | None,
        error: str | None,
    ) -> TargetResult:
        """Write edit()/cancel()'s post-Buffer-success DB update. If Buffer
        already succeeded but our own write then fails, report that
        explicitly as db_write_failed rather than losing the Buffer-side
        change or reporting a misleading generic failure (see
        Publisher._apply_success_update for the publish/retry equivalent)."""
        try:
            self._repo.update_post(post.id, **update_fields)
        except Exception as exc:  # noqa: BLE001 - Buffer already succeeded; don't lose the ids
            logger.error(
                "Buffer成功後のDB更新に失敗しました（post_id=%s, buffer_post_id=%s）: %s",
                post.id,
                buffer_post_id,
                exc,
            )
            return TargetResult(
                service=channel.service,
                status="db_write_failed",
                post_id=post.id,
                buffer_post_id=buffer_post_id,
                due_at=due_at_out,
                error=f"db_write_failed: {exc}",
            )
        status = update_fields["status"]
        return TargetResult(
            service=channel.service,
            status=status.value,
            post_id=post.id,
            buffer_post_id=buffer_post_id,
            due_at=due_at_out,
            error=error,
        )

    def _get_editable_post_and_channel(self, post_id: UUID) -> tuple[PostRow, ChannelRow]:
        post = self._repo.get_post(post_id)
        if post is None:
            raise PostNotFoundError(f"post が見つかりません: {post_id}")
        if post.status not in _EDITABLE_STATUSES:
            message = f"編集/キャンセルできるのは scheduled/draft の投稿のみです（現在: {post.status.value}）"
            raise PostNotEditableError(message)
        if post.due_at is not None and post.due_at <= self._clock() + _NEAR_DUE_GUARD:
            # Buffer may already be sending it — editing/canceling now risks
            # a race with an in-flight publish.
            raise PostNotEditableError(
                "投稿時刻の5分前を過ぎているため変更できません。Bufferの画面で確認してください"
            )
        channel = next((c for c in self._repo.list_channels() if c.id == post.channel_id), None)
        if channel is None:
            raise PostNotFoundError("チャンネルが見つかりません")
        return post, channel

    # --- sync ----------------------------------------------------------------------

    def sync(self, *, full: bool = False, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Reconcile scheduled/draft posts against Buffer.

        `full=False` (the EventBridge scheduler's usage) only queries posts
        whose due_at has passed (or is null) — cheap and frequent.
        `full=True` (the manual /sync API endpoint and CLI `sync` command,
        both human-triggered) reconciles every scheduled/draft post
        regardless of due_at, since a human may have rescheduled/canceled/
        edited directly in Buffer's UI.

        `should_stop` lets a Lambda caller pass a deadline check (see
        api/_deadline.make_should_stop): when it returns True, the loop
        stops before starting the next post and `truncated: True` is
        reported instead of leaving the invocation to be killed mid-post.
        """
        counts = {"sent": 0, "failed": 0, "canceled": 0, "unchanged": 0}
        errors: list[dict[str, Any]] = []
        now = self._clock()
        truncated = False
        for post in self._repo.list_syncable_posts():
            if should_stop is not None and should_stop():
                truncated = True
                break
            # Only query Buffer once a post's due_at has (or should have)
            # passed — querying earlier can't change anything and burns
            # Buffer's rate limit. Null due_at (addToQueue/shareNow before
            # Buffer assigns a slot) is always queried since we can't tell
            # whether it's "future".
            if not full and post.due_at is not None and post.due_at > now:
                counts["unchanged"] += 1
                continue
            try:
                bucket = self._sync_one(post)
                counts[bucket] += 1
            except BufferAuthError:
                # The API key itself is bad — every remaining post would fail
                # identically, so stop rather than spam the error list.
                raise
            except Exception as exc:  # noqa: BLE001 - one post's failure must not abort the sync
                errors.append({"post_id": str(post.id), "message": str(exc)})

        result: dict[str, Any] = dict(counts)
        result["stuck_pending"] = len(self._repo.list_pending_posts())
        if errors:
            result["errors"] = errors
        if truncated:
            result["truncated"] = True
        return result

    def _sync_one(self, post: PostRow) -> str:
        assert post.buffer_post_id is not None  # noqa: S101 - list_syncable_posts + DB CHECK guarantee this
        remote = self._buffer.get_post(post.buffer_post_id)
        if remote is None:
            self._repo.update_post(post.id, status=PostStatus.CANCELED)
            return "canceled"

        remote_status = remote.get("status")
        # Buffer is authoritative for due_at/caption on any candidate here
        # (scheduled or draft): repair both regardless of which branch below
        # applies, so a human editing directly in Buffer's UI is reflected.
        repair_fields = self._due_at_update_fields(post, remote.get("dueAt"))
        repair_fields.update(self._caption_update_fields(post, remote.get("text")))

        if remote_status == "sent":
            self._repo.update_post(
                post.id,
                status=PostStatus.SENT,
                external_link=remote.get("externalLink"),
                sent_at=remote.get("sentAt") or self._clock(),
                **repair_fields,
            )
            return "sent"

        if remote_status == "error":
            error = remote.get("error") or {}
            self._repo.update_post(
                post.id,
                status=PostStatus.FAILED,
                error_message=_error_message(error.get("message")),
                **repair_fields,
            )
            return "failed"

        if repair_fields:
            self._repo.update_post(post.id, **repair_fields)

        return "unchanged"

    @staticmethod
    def _due_at_update_fields(post: PostRow, remote_due_at: str | None) -> dict[str, Any]:
        """Buffer is authoritative for due_at; write it back whenever it
        differs (covers addToQueue/shareNow getting their first due_at, and
        a human rescheduling directly in Buffer's UI)."""
        if not remote_due_at:
            return {}
        parsed = _parse_buffer_datetime(remote_due_at)
        if parsed is None or parsed == post.due_at:
            return {}
        return {"due_at": parsed}

    @staticmethod
    def _caption_update_fields(post: PostRow, remote_text: str | None) -> dict[str, Any]:
        """Buffer is authoritative for caption too; write it back whenever
        it differs (a human may have edited the text directly in Buffer's
        UI, or our own edit() DB write may have previously failed)."""
        if not remote_text or remote_text == post.caption:
            return {}
        return {"caption": remote_text}
