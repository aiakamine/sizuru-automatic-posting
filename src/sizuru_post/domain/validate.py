"""Pure validation rules for captions and video files, plus ffprobe parsing.

All side effects (shelling out to ffprobe) are isolated to `probe_video`;
everything else here is a pure function so it can be unit tested without
touching the filesystem or a subprocess.
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sizuru_post._vendor.twitter_text import parse_tweet
from sizuru_post.domain.models import Target, VideoInfo

Level = Literal["error", "warning"]


@dataclass(frozen=True)
class ValidationIssue:
    service: str
    level: Level
    message: str


# --- X (Twitter-style) weighted length ----------------------------------

_X_LIMIT = 280


def caption_length(service: str, text: str) -> int:
    """The length used against a service's caption limit: X's official
    counting rules for "x" (weighted length, emoji ZWJ sequences, URL
    entity detection — see
    https://docs.x.com/fundamentals/counting-characters), or a plain
    code-point count for tiktok/instagram. Both branches normalize to NFC
    first so a caller-supplied decomposed character (e.g. "e" + combining
    accent) counts the same as its precomposed form.

    Exposed publicly (not just via validate_caption) because the admin
    dashboard's frontend must reproduce the exact same result via the npm
    `twitter-text` package — tests/fixtures/caption_length_cases.json is
    the shared contract verified independently from both sides.
    """
    normalized = unicodedata.normalize("NFC", text)
    if service == "x":
        return int(parse_tweet(normalized).weightedLength)
    return len(normalized)


# --- TikTok / Instagram caption rules ------------------------------------

_CAPTION_LIMIT = 2200
_INSTAGRAM_HASHTAG_LIMIT = 30
_HASHTAG_PATTERN = re.compile(r"[#＃]\w+")


def validate_caption(target: Target) -> list[ValidationIssue]:
    """Validate a single target's caption against its service's rules."""
    if target.service == "x":
        return _validate_x_caption(target)
    return _validate_generic_caption(target)


def _validate_x_caption(target: Target) -> list[ValidationIssue]:
    length = caption_length("x", target.caption)
    if length <= _X_LIMIT:
        return []
    return [
        ValidationIssue(
            service=target.service,
            level="error",
            message=f"X: 本文が長すぎます（{length}/{_X_LIMIT}）",
        )
    ]


def _validate_generic_caption(target: Target) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    length = caption_length(target.service, target.caption)
    if length > _CAPTION_LIMIT:
        issues.append(
            ValidationIssue(
                service=target.service,
                level="error",
                message=f"{target.service}: 本文が長すぎます（{length}/{_CAPTION_LIMIT}文字）",
            )
        )

    if target.service == "instagram":
        hashtag_count = len(_HASHTAG_PATTERN.findall(target.caption))
        if hashtag_count > _INSTAGRAM_HASHTAG_LIMIT:
            message = f"instagram: ハッシュタグが多すぎます（{hashtag_count}/{_INSTAGRAM_HASHTAG_LIMIT}）"
            issues.append(ValidationIssue(service=target.service, level="error", message=message))
    return issues


# --- Video rules -----------------------------------------------------------

_X_MAX_DURATION_SEC = 140.0
_LONG_VIDEO_WARNING_SEC = 180.0
_TARGET_ASPECT_RATIO = 9 / 16
_ASPECT_RATIO_TOLERANCE = 0.01


def validate_video(video: VideoInfo, targets: list[Target]) -> list[ValidationIssue]:
    """Validate a probed video against the rules of each target's service."""
    services = {target.service for target in targets}
    issues: list[ValidationIssue] = []

    if "x" in services and video.duration_sec > _X_MAX_DURATION_SEC:
        issues.append(
            ValidationIssue(
                service="x",
                level="error",
                message=f"X: 動画が長すぎます（{video.duration_sec:.1f}秒/{_X_MAX_DURATION_SEC:.0f}秒）",
            )
        )

    for service in services & {"tiktok", "instagram"}:
        if video.duration_sec > _LONG_VIDEO_WARNING_SEC:
            limit = f"{_LONG_VIDEO_WARNING_SEC:.0f}"
            duration = f"{video.duration_sec:.1f}"
            message = f"{service}: 動画が{limit}秒を超えています（{duration}秒）"
            issues.append(ValidationIssue(service=service, level="warning", message=message))

    if video.width > 0 and video.height > 0:
        ratio = video.width / video.height
        deviation = abs(ratio - _TARGET_ASPECT_RATIO) / _TARGET_ASPECT_RATIO
        if deviation > _ASPECT_RATIO_TOLERANCE:
            for service in services:
                issues.append(
                    ValidationIssue(
                        service=service,
                        level="warning",
                        message=f"{service}: 縦横比が9:16ではありません（{video.width}x{video.height}）",
                    )
                )

    if video.video_codec.lower() != "h264":
        for service in services:
            issues.append(
                ValidationIssue(
                    service=service,
                    level="warning",
                    message=f"{service}: 動画コーデックがh264ではありません（{video.video_codec}）",
                )
            )

    return issues


# --- ffprobe integration -----------------------------------------------------


def parse_ffprobe(data: dict[str, Any]) -> VideoInfo:
    """Parse `ffprobe -show_streams -show_format` JSON output into VideoInfo."""
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise ValueError("動画ストリームが見つかりません")
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration_str = data.get("format", {}).get("duration") or video_stream.get("duration")
    if duration_str is None:
        raise ValueError("動画の長さを取得できません")

    return VideoInfo(
        duration_sec=float(duration_str),
        width=int(video_stream["width"]),
        height=int(video_stream["height"]),
        video_codec=str(video_stream.get("codec_name", "")),
        audio_codec=str(audio_stream["codec_name"]) if audio_stream else None,
    )


_FFPROBE_TIMEOUT_SEC = 30
_FFMPEG_TIMEOUT_SEC = 60


def probe_video(path: Path) -> VideoInfo:
    """Run ffprobe on a local file and parse the result. Not unit tested beyond parsing."""
    try:
        result = subprocess.run(  # noqa: S603
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=_FFPROBE_TIMEOUT_SEC,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"ffprobeがタイムアウトしました（{_FFPROBE_TIMEOUT_SEC}秒）") from exc
    return parse_ffprobe(json.loads(result.stdout))


_DEFAULT_THUMBNAIL_OFFSET_MS = 1000
_THUMBNAIL_WIDTH_PX = 360


def generate_thumbnail(video_path: Path, out_path: Path, offset_ms: int | None = None) -> None:
    """Extract one frame as a JPEG, scaled to a fixed width, via ffmpeg.

    Thin subprocess wrapper (same pattern as probe_video): not unit tested
    beyond the timeout/stdin safety wiring; callers (Publisher) are
    responsible for treating failures (including a timeout) as non-fatal,
    per the "thumbnail failure must not block posting" rule.
    """
    offset_seconds = (offset_ms if offset_ms is not None else _DEFAULT_THUMBNAIL_OFFSET_MS) / 1000
    subprocess.run(  # noqa: S603
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{offset_seconds:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={_THUMBNAIL_WIDTH_PX}:-1",
            str(out_path),
        ],
        capture_output=True,
        check=True,
        timeout=_FFMPEG_TIMEOUT_SEC,
        stdin=subprocess.DEVNULL,
    )
