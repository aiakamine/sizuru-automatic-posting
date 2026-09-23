"""API Gateway HTTP API routes for the sns-schedule skill: upload a video
straight to S3, then create scheduled Buffer posts for it via the existing
Publisher.

Routes (must match infra/sam/template.yaml exactly):
  POST /schedule/uploads      -> presigned S3 POST for the video + thumbnail
  GET  /schedule/posts        -> what's already scheduled in a date range
  POST /schedule/submissions  -> create/resend a submission's posts

Auth: a single shared secret (`x-api-key` header), compared with
`hmac.compare_digest` against Settings.scheduler_api_key (see
config.Settings / factory.build_settings) — deliberately NOT the admin
dashboard's Supabase JWT (see api/handler.py's docstring): this is called
by a local CLI skill, not a signed-in browser user. No key configured on
the server -> 503 `scheduler_disabled`; missing/wrong key -> 401
`unauthorized`. Kept in its own module (rather than api/handler.py) so
that file stays focused on the admin dashboard's JWT-authenticated routes.

Settings/Publisher/Storage/Repo are all cached in api/_container, shared
with api/handler.py (both route families run in the same ApiFunction
Lambda) — see that module's docstring for why.
"""

from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from sizuru_post.adapters.repo import PostInRange
from sizuru_post.adapters.storage import PresignedUpload, StoredObject
from sizuru_post.api import _container as container
from sizuru_post.api._deadline import make_should_stop
from sizuru_post.api._http import ApiError, get_header, parse_body, response
from sizuru_post.config import Settings
from sizuru_post.domain.models import PublishRequest, Target, Timing, VideoInfo, validate_due_at_is_future
from sizuru_post.serialization import plan_has_error, plan_to_dict, report_to_dict

_API_KEY_HEADER = "x-api-key"
_UPLOAD_EXPIRES_IN_SEC = 900
_VIDEO_MAX_BYTES = 300 * 1024 * 1024  # 314,572,800
_THUMBNAIL_MAX_BYTES = 5 * 1024 * 1024  # 5,242,880 — a single JPEG frame, not a video
_MAX_RANGE_DAYS = 62
_MAX_TARGETS = 3


# --- auth --------------------------------------------------------------------


def _require_api_key(event: dict[str, Any], settings: Settings) -> None:
    configured = settings.scheduler_api_key
    # An empty/whitespace-only configured secret is a misconfiguration
    # (SCHEDULER_API_KEY="" in SSM), not a legitimate secret — treating it
    # as "not configured" avoids hmac.compare_digest(b"", b"") == True
    # silently authenticating an equally-empty provided key.
    if configured is None or not configured.get_secret_value().strip():
        raise ApiError(503, "scheduler_disabled", "スケジューリングAPIは無効です（APIキーが未設定です）")
    provided = get_header(event, _API_KEY_HEADER)
    if provided is None or not provided.strip():
        raise ApiError(401, "unauthorized", "APIキーが無効です")
    # Compare as UTF-8 bytes, not str: hmac.compare_digest(str, str) raises
    # TypeError on non-ASCII input (e.g. a wrong key containing non-ASCII
    # characters) — that must still be a clean 401, not an unhandled 500.
    # bytes-vs-bytes has no such restriction.
    if not hmac.compare_digest(provided.encode("utf-8"), configured.get_secret_value().encode("utf-8")):
        raise ApiError(401, "unauthorized", "APIキーが無効です")


# --- POST /schedule/uploads ---------------------------------------------------


def _presigned_to_dict(upload: PresignedUpload) -> dict[str, Any]:
    return {
        "key": upload.key,
        "public_url": upload.public_url,
        "upload": {"url": upload.upload_url, "fields": upload.fields},
    }


def _handle_uploads() -> dict[str, Any]:
    storage = container.get_storage()
    video = storage.presign_post(
        subdir="videos",
        extension=".mp4",
        content_type="video/mp4",
        max_bytes=_VIDEO_MAX_BYTES,
        expires_in=_UPLOAD_EXPIRES_IN_SEC,
    )
    thumbnail = storage.presign_post(
        subdir="thumbnails",
        extension=".jpg",
        content_type="image/jpeg",
        max_bytes=_THUMBNAIL_MAX_BYTES,
        expires_in=_UPLOAD_EXPIRES_IN_SEC,
    )
    return response(
        200,
        {
            "video": _presigned_to_dict(video),
            "thumbnail": _presigned_to_dict(thumbnail),
            "expires_in": _UPLOAD_EXPIRES_IN_SEC,
            "max_bytes": _VIDEO_MAX_BYTES,
            "thumbnail_max_bytes": _THUMBNAIL_MAX_BYTES,
        },
    )


# --- GET /schedule/posts -------------------------------------------------------


def _parse_range(params: dict[str, str]) -> tuple[datetime, datetime]:
    from_raw = params.get("from")
    to_raw = params.get("to")
    if not from_raw or not to_raw:
        raise ApiError(400, "validation", "from/to は必須です")
    try:
        start = datetime.fromisoformat(from_raw)
        end = datetime.fromisoformat(to_raw)
    except ValueError as exc:
        raise ApiError(400, "validation", "from/to の形式が不正です（ISO8601で指定してください）") from exc
    if start.tzinfo is None or end.tzinfo is None:
        raise ApiError(400, "validation", "from/to にはタイムゾーンを含めてください")
    if end <= start:
        raise ApiError(400, "validation", "to は from より後の日時を指定してください")
    if end - start > timedelta(days=_MAX_RANGE_DAYS):
        raise ApiError(400, "validation", f"期間は{_MAX_RANGE_DAYS}日以内で指定してください")
    return start, end


def _post_in_range_to_dict(row: PostInRange) -> dict[str, Any]:
    return {
        "post_id": str(row.post_id),
        "submission_id": str(row.submission_id),
        "service": row.service,
        "due_at": row.due_at.isoformat(),
        "status": row.status,
        "title": row.title,
    }


def _handle_list_posts(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    start, end = _parse_range(params)
    rows = container.get_repo().list_posts_in_range(start, end)
    return response(200, {"posts": [_post_in_range_to_dict(r) for r in rows]})


# --- POST /schedule/submissions ------------------------------------------------


class ScheduleTargetInput(BaseModel):
    """One target's caption/schedule within a `/schedule/submissions`
    request — distinct from domain.models.Target/Timing: due_at is inline
    and required (every target here is always customScheduled), rather
    than an optional nested Timing override."""

    service: Literal["tiktok", "instagram", "x"]
    caption: str
    due_at: datetime
    tiktok_title: str | None = None
    share_to_feed: bool = True

    @field_validator("caption")
    @classmethod
    def _validate_caption_not_blank(cls, caption: str) -> str:
        # validate_caption() (domain/validate.py) only enforces an UPPER
        # length bound per service — an empty/whitespace-only caption
        # would otherwise slip through it uncaught.
        if not caption.strip():
            raise ValueError("caption は空白のみにはできません")
        return caption

    @model_validator(mode="after")
    def _validate_due_at(self) -> Self:
        error = validate_due_at_is_future(self.due_at)
        if error is not None:
            raise ValueError(error)
        return self


class ScheduleSubmissionRequest(BaseModel):
    video_key: str
    thumbnail_key: str | None = None
    title: str | None = None
    video: VideoInfo
    targets: list[ScheduleTargetInput] = Field(min_length=1, max_length=_MAX_TARGETS)
    thumbnail_offset_ms: int | None = Field(default=None, ge=0)
    dry_run: bool = False

    @field_validator("video")
    @classmethod
    def _validate_video_dimensions(cls, video: VideoInfo) -> VideoInfo:
        # domain.models.VideoInfo itself has no positivity constraint (it's
        # also built from real ffprobe output, whose own defensive checks
        # live in domain/validate.py) — a client-reported duration_sec/
        # width/height of 0 (or negative) would otherwise sail through to
        # the DB's own CHECK constraint and 500, instead of a clean 400.
        if video.duration_sec <= 0:
            raise ValueError("video.duration_sec は正の数である必要があります")
        if video.width <= 0 or video.height <= 0:
            raise ValueError("video.width / video.height は正の整数である必要があります")
        return video

    @model_validator(mode="after")
    def _validate_unique_services(self) -> Self:
        services = [t.service for t in self.targets]
        if len(services) != len(set(services)):
            raise ValueError("targets の service は重複できません")
        return self


def _validate_key(key: str, *, prefix: str, subdir: str, extension: str, field_name: str) -> None:
    """video_key/thumbnail_key must match the exact layout S3Storage
    generates (see adapters.storage.S3Storage._new_key) — this is what
    keeps the scheduling API from being used to reference an arbitrary S3
    object outside the uploads it itself presigned.

    `[0-9]` (not `\\d`, which also matches non-ASCII Unicode digit
    categories like full-width digits) and `fullmatch` (not `match` + a
    trailing `$`, which matches just before a trailing "\\n" rather than
    only the true end of the string) — both matter here since this pattern
    is a security boundary, not just a UX nicety.
    """
    pattern = re.compile(rf"{re.escape(prefix)}{subdir}/[0-9]{{4}}/[0-9]{{2}}/[0-9a-f]{{32}}\.{extension}")
    if not pattern.fullmatch(key):
        raise ApiError(400, "validation", f"{field_name} の形式が不正です")


def _validation_error_message(exc: ValidationError) -> str:
    return "; ".join(str(error["msg"]) for error in exc.errors())


def _to_domain_target(item: ScheduleTargetInput) -> Target:
    return Target(
        service=item.service,
        caption=item.caption,
        tiktok_title=item.tiktok_title,
        share_to_feed=item.share_to_feed,
        timing=Timing(mode="customScheduled", due_at=item.due_at),
    )


def _handle_create_submission(event: dict[str, Any], context: object) -> dict[str, Any]:
    body = parse_body(event)
    try:
        payload = ScheduleSubmissionRequest.model_validate(body)
    except ValidationError as exc:
        raise ApiError(400, "validation", _validation_error_message(exc)) from exc

    settings = container.get_settings()
    prefix = settings.s3_public_prefix
    _validate_key(payload.video_key, prefix=prefix, subdir="videos", extension="mp4", field_name="video_key")
    if payload.thumbnail_key is not None:
        _validate_key(
            payload.thumbnail_key,
            prefix=prefix,
            subdir="thumbnails",
            extension="jpg",
            field_name="thumbnail_key",
        )

    # Existence checks run for both dry_run and real submissions — a dry
    # run is meant to catch exactly this kind of "not actually uploaded
    # yet" mistake before the client commits to scheduling anything.
    storage = container.get_storage()
    if not storage.exists(payload.video_key):
        raise ApiError(400, "upload_missing", "動画がまだアップロードされていません")
    thumbnail_url = (
        storage.public_url_for(payload.thumbnail_key)
        if payload.thumbnail_key is not None and storage.exists(payload.thumbnail_key)
        else None
    )

    targets = [_to_domain_target(t) for t in payload.targets]
    request = PublishRequest(
        video_path=None,
        title=payload.title,
        # Every target carries its own customScheduled timing (see
        # resolve_timing/_to_domain_target); this request-level value is
        # only pydantic's required-field filler, never actually read.
        timing=Timing(mode="customScheduled", due_at=payload.targets[0].due_at),
        targets=targets,
        thumbnail_offset_ms=payload.thumbnail_offset_ms,
    )
    publisher = container.get_publisher()

    if payload.dry_run:
        plan = publisher.plan_with_video(request, payload.video)
        return response(
            200, {"dry_run": True, "has_error": plan_has_error(plan), "plan": plan_to_dict(plan)}
        )

    stored = StoredObject(key=payload.video_key, public_url=storage.public_url_for(payload.video_key))
    should_stop = make_should_stop(context)
    report = publisher.publish_uploaded(
        request, payload.video, stored, thumbnail_url, approved=True, should_stop=should_stop
    )
    incomplete = any(result.status == "not_attempted" for result in report.results)
    return response(200, {"dry_run": False, "incomplete": incomplete, **report_to_dict(report)})


# --- entry point ---------------------------------------------------------------


def dispatch(event: dict[str, Any], context: object) -> dict[str, Any]:
    settings = container.get_settings()
    _require_api_key(event, settings)

    route_key = event.get("routeKey", "")
    if route_key == "POST /schedule/uploads":
        return _handle_uploads()
    if route_key == "GET /schedule/posts":
        return _handle_list_posts(event)
    if route_key == "POST /schedule/submissions":
        return _handle_create_submission(event, context)

    raise ApiError(404, "not_found", "不明なエンドポイントです")
