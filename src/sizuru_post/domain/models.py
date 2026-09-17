"""Core domain models: the contract between the CLI, publisher and adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


class Timing(BaseModel):
    """When a post should go out."""

    mode: Literal["shareNow", "addToQueue", "customScheduled"]
    due_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_due_at(self) -> Self:
        if self.mode == "customScheduled":
            if self.due_at is None:
                raise ValueError("customScheduled には due_at が必須です")
            if self.due_at.tzinfo is None:
                raise ValueError("due_at はタイムゾーン情報を含む必要があります")
            if self.due_at <= datetime.now(UTC):
                raise ValueError("due_at は未来の日時を指定してください")
        elif self.due_at is not None:
            raise ValueError("due_at は customScheduled の場合のみ指定できます")
        return self


class Target(BaseModel):
    """A single service to post to, with its own caption/options."""

    service: Literal["tiktok", "instagram", "x"]
    caption: str
    channel_id: str | None = None
    tiktok_title: str | None = None
    share_to_feed: bool = True
    timing: Timing | None = None
    """Per-target override of the request-level timing (see resolve_timing)."""


class PublishRequest(BaseModel):
    """A full publish request: one video, one or more targets."""

    video_path: Path
    title: str | None = None
    timing: Timing
    targets: list[Target] = Field(min_length=1)
    draft: bool = False
    thumbnail_offset_ms: int | None = None

    @model_validator(mode="after")
    def _validate_unique_targets(self) -> Self:
        services = [target.service for target in self.targets]
        if len(services) != len(set(services)):
            raise ValueError("targets の service は重複できません")
        return self


class VideoInfo(BaseModel):
    """Metadata about a local video file, as reported by ffprobe."""

    duration_sec: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None = None


class PostStatus(StrEnum):
    """Lifecycle status of a single (submission, channel) post.

    pending -> {draft, scheduled} -> sent, or -> failed / canceled at any
    non-terminal point. A row still `pending` at sync time means the
    process crashed mid-publish (see Publisher.sync's "stuck pending"
    reporting).
    """

    PENDING = "pending"
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    SENT = "sent"
    FAILED = "failed"
    CANCELED = "canceled"


def resolve_timing(request: PublishRequest, target: Target) -> Timing:
    """The timing that actually applies to a target: its own override, or
    the request-level default. Timing now lives per-post in the DB, so
    every call site that needs "the" timing for a target must go through
    this instead of reading request.timing directly."""
    return target.timing or request.timing
