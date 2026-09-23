"""Pure logic for the sns-schedule skill: no filesystem/subprocess/network I/O.

Kept separate from schedule.py so the scheduling and validation rules can be
unit tested directly, without mocking ffmpeg/curl/urllib. schedule.py (the
CLI) is the only module allowed to do I/O; everything here is a pure function
or a plain dataclass.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal

JST = timezone(timedelta(hours=9))

Level = Literal["error", "warning"]

# --- slot proposal --------------------------------------------------------

_MIN_LEAD_TIME = timedelta(minutes=15)
_COLLISION_WINDOW = timedelta(minutes=30)
_MAX_DAYS_TO_SEARCH = 366


class SlotSearchExhausted(ValueError):
    """Raised when propose_slots can't find enough non-colliding slots
    within _MAX_DAYS_TO_SEARCH days."""


def propose_slots(
    existing: list[datetime],
    start_date: date,
    times: list[time],
    per_day: int,
    count: int,
    now: datetime,
    max_days: int = _MAX_DAYS_TO_SEARCH,
) -> list[datetime]:
    """Propose `count` JST due_at datetimes starting from `start_date`.

    Each day contributes up to `per_day` slots, taken in order from `times`
    (already resolved by the caller to either the user's --times list or a
    single-element list built from --time). A candidate is skipped if it's
    less than 15 minutes from `now`, or within 30 minutes of any existing or
    already-proposed slot.

    `max_days` bounds how many days ahead we search. The caller MUST pass
    the number of days it actually fetched `existing` posts for (e.g. the
    API's 62-day window) -- searching further than that would silently treat
    unknown/unfetched days as free, risking a double-booking. It defaults to
    the full year search for direct/pure callers and tests.
    """
    day_times = times[:per_day]
    if count <= 0 or not day_times:
        return []

    proposed: list[datetime] = []
    taken = list(existing)
    day = start_date
    days_tried = 0
    while len(proposed) < count and days_tried < max_days:
        for t in day_times:
            if len(proposed) >= count:
                break
            candidate = datetime.combine(day, t, tzinfo=JST)
            if candidate < now + _MIN_LEAD_TIME:
                continue
            if _collides(candidate, taken):
                continue
            proposed.append(candidate)
            taken.append(candidate)
        day += timedelta(days=1)
        days_tried += 1

    if len(proposed) < count:
        raise SlotSearchExhausted(
            f"空き枠が見つかりませんでした（提案できたのは{len(proposed)}/{count}件）"
        )
    return proposed


def _collides(candidate: datetime, taken: list[datetime]) -> bool:
    return any(abs(candidate - t) <= _COLLISION_WINDOW for t in taken)


def parse_hhmm(text: str) -> time:
    hour_str, _, minute_str = text.strip().partition(":")
    if not minute_str:
        raise ValueError(f"時刻はHH:MM形式で指定してください: {text!r}")
    return time(hour=int(hour_str), minute=int(minute_str))


def resolve_day_times(time_opt: str | None, times_opt: str | None, per_day: int) -> list[time]:
    """Build the ordered list of times-per-day used by propose_slots from
    the CLI's --time / --times / --per-day options. `time_opt` is None
    whenever the caller used --times instead (they're a mutually exclusive
    argparse group at the CLI layer).

    Deliberately strict: silently capping/padding a mismatch between
    --per-day and the number of times given is a UX trap (e.g. asking for
    2 posts/day but --times only lists 1 time would silently schedule only
    1/day). --times, when given, must list exactly `per_day` times; with no
    --times, --per-day must be 1 (there's only a single --time to use).
    """
    times = [parse_hhmm(part) for part in times_opt.split(",") if part.strip()] if times_opt else None
    if times is None:
        if time_opt is None:
            raise ValueError("--time または --times のどちらかを指定してください")
        if per_day != 1:
            raise ValueError(f"1日{per_day}本なら --times で時刻を{per_day}個指定してください")
        return [parse_hhmm(time_opt)]
    if len(times) != per_day:
        raise ValueError(f"1日{per_day}本なら --times で時刻を{per_day}個指定してください")
    return times


# --- ffprobe parsing --------------------------------------------------------


def parse_ffprobe(data: dict[str, Any]) -> dict[str, Any]:
    """Parse `ffprobe -print_format json -show_format -show_streams` output
    into the probe() command's shape (minus size_bytes, which the caller
    fills in from the filesystem since ffprobe's format.size is unreliable
    for some containers)."""
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError("動画ストリームが見つかりません")
    duration = fmt.get("duration", video.get("duration"))
    return {
        "duration_sec": float(duration) if duration is not None else 0.0,
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "video_codec": str(video.get("codec_name", "")),
        "audio_codec": str(audio.get("codec_name")) if audio else None,
    }


# --- preview / transcode decision ------------------------------------------

PREVIEW_SIZE_LIMIT_BYTES = 19 * 1024 * 1024  # 19 MiB
PREVIEW_CRF_FIRST = 28
PREVIEW_CRF_RETRY = 32


def needs_transcode(size_bytes: int) -> bool:
    return size_bytes > PREVIEW_SIZE_LIMIT_BYTES


# --- local (client-side) plan validation ------------------------------------
#
# These four limits are a client-side approximation for fast, offline
# feedback only. `src/sizuru_post/domain/validate.py` (the API's actual
# dry_run/real validation, invoked via `submit`) is the authoritative source
# -- if these ever drift from it, that file wins; update these to match.

_URL_PATTERN = re.compile(r"https?://\S+")
_HASHTAG_PATTERN = re.compile(r"[#＃]\w+")
_X_APPROX_LIMIT = 280
_GENERIC_LIMIT = 2200
_INSTAGRAM_HASHTAG_LIMIT = 30
_X_MAX_DURATION_SEC = 140.0

_VALID_SERVICES = {"tiktok", "instagram", "x"}


@dataclass(frozen=True)
class LocalIssue:
    clip_no: int
    service: str
    level: Level
    message: str


def issues_to_dicts(issues: list[LocalIssue]) -> list[dict[str, Any]]:
    return [
        {"clip_no": i.clip_no, "service": i.service, "level": i.level, "message": i.message} for i in issues
    ]


def approx_x_weighted_length(text: str) -> int:
    """Rough local approximation of X's weighted-length rule: a URL counts
    as 23 regardless of its own length, other characters count 2 if
    CJK/full-width else 1. This is intentionally approximate -- schedule.py
    has no dependency on sizuru_post._vendor.twitter_text (stdlib only);
    the authoritative count happens server-side inside `submit`'s dry_run.
    """
    normalized = unicodedata.normalize("NFC", text)
    urls = _URL_PATTERN.findall(normalized)
    remainder = _URL_PATTERN.sub("", normalized)
    weight = len(urls) * 23
    for ch in remainder:
        weight += 2 if _is_wide(ch) else 1
    return weight


def _is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def validate_clip_locally(clip: dict[str, Any], duration_sec: float, now: datetime) -> list[LocalIssue]:
    """Local-only validation for one plan.json clip: caption length limits,
    Instagram hashtag count, X video duration, due_at sanity, and duplicate
    service targets. Does NOT call the API (that's `submit`'s dry_run)."""
    no = clip["no"]
    issues: list[LocalIssue] = []
    seen_services: set[str] = set()
    for target in clip.get("targets", []):
        service = target.get("service")
        if service not in _VALID_SERVICES:
            issues.append(LocalIssue(no, str(service), "error", f"未知の媒体指定です: {service!r}"))
            continue
        if service in seen_services:
            message = f"{service}: 同じクリップに重複した媒体指定があります"
            issues.append(LocalIssue(no, service, "error", message))
        seen_services.add(service)

        issues.extend(_validate_caption_locally(no, service, target.get("caption", "")))
        issues.extend(_validate_due_at_locally(no, service, target.get("due_at"), now))
        if service == "x" and duration_sec > _X_MAX_DURATION_SEC:
            issues.append(
                LocalIssue(
                    no,
                    service,
                    "error",
                    f"X: 動画が長すぎます（{duration_sec:.1f}秒/{_X_MAX_DURATION_SEC:.0f}秒）",
                )
            )
    return issues


def _validate_caption_locally(no: int, service: str, caption: str) -> list[LocalIssue]:
    if service == "x":
        length = approx_x_weighted_length(caption)
        if length > _X_APPROX_LIMIT:
            message = f"X: 本文が長すぎます（概算{length}/{_X_APPROX_LIMIT}）"
            return [LocalIssue(no, service, "error", message)]
        return []

    issues: list[LocalIssue] = []
    length = len(unicodedata.normalize("NFC", caption))
    if length > _GENERIC_LIMIT:
        issues.append(
            LocalIssue(no, service, "error", f"{service}: 本文が長すぎます（{length}/{_GENERIC_LIMIT}文字）")
        )
    if service == "instagram":
        hashtag_count = len(_HASHTAG_PATTERN.findall(caption))
        if hashtag_count > _INSTAGRAM_HASHTAG_LIMIT:
            issues.append(
                LocalIssue(
                    no,
                    service,
                    "error",
                    f"instagram: ハッシュタグが多すぎます（{hashtag_count}/{_INSTAGRAM_HASHTAG_LIMIT}）",
                )
            )
    return issues


def _validate_due_at_locally(no: int, service: str, due_at_str: Any, now: datetime) -> list[LocalIssue]:
    if not due_at_str:
        return [LocalIssue(no, service, "error", f"{service}: due_atがありません")]
    try:
        due_at = datetime.fromisoformat(due_at_str)
    except (TypeError, ValueError):
        message = f"{service}: due_atがISO8601形式ではありません（{due_at_str}）"
        return [LocalIssue(no, service, "error", message)]
    if due_at.tzinfo is None:
        message = f"{service}: due_atにタイムゾーンがありません（{due_at_str}）"
        return [LocalIssue(no, service, "error", message)]
    if due_at <= now:
        message = f"{service}: due_atが過去の日時です（{due_at_str}）"
        return [LocalIssue(no, service, "error", message)]
    return []


def plan_has_error(issues: list[LocalIssue]) -> bool:
    return any(issue.level == "error" for issue in issues)


# --- 本編(main video) YouTube link ------------------------------------------
#
# Posts must drive viewers to the YouTube 本編: every service's caption
# includes the URL as plain text. TikTok/Instagram captions can't have
# clickable links, but the user has accepted that (no profile-link setup).

_YOUTUBE_URL_PATTERN = re.compile(r"^https?://(www\.)?(youtube\.com/(watch|live/)|youtu\.be/)", re.IGNORECASE)

# Query params to drop when a URL's video ID can't be extracted (so there's
# nothing to rewrite to the canonical form -- see normalize_youtube_url):
# share-link tracking (si), embed/referrer tracking (feature, pp), and any
# utm_* analytics param. This is a blocklist, not an allowlist, so it
# doesn't need updating for every legitimate param.
_YOUTUBE_TRACKING_PARAM_NAMES = {"si", "feature", "pp"}

_YOUTU_BE_ID_RE = re.compile(r"^https?://(?:www\.)?youtu\.be/([^/?#]+)", re.IGNORECASE)
_YOUTUBE_WATCH_RE = re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/watch", re.IGNORECASE)
_YOUTUBE_LIVE_ID_RE = re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/live/([^/?#]+)", re.IGNORECASE)

CANONICAL_YOUTUBE_HOST = "www.youtube.com"
CANONICAL_YOUTUBE_PATH = "/watch"


def _is_youtube_tracking_param(name: str) -> bool:
    return name in _YOUTUBE_TRACKING_PARAM_NAMES or name.startswith("utm_")


def _extract_youtube_video_id(url: str, query: dict[str, str]) -> str | None:
    """Pull the video ID out of any recognized 本編 URL shape (youtu.be/<id>,
    youtube.com/watch?v=<id> with or without www./m., youtube.com/live/<id>).
    None if `url` doesn't match one of these (e.g. a shorts link, a channel
    URL, or a non-YouTube URL)."""
    match = _YOUTU_BE_ID_RE.match(url)
    if match:
        return match.group(1)
    if _YOUTUBE_WATCH_RE.match(url):
        return query.get("v")
    match = _YOUTUBE_LIVE_ID_RE.match(url)
    if match:
        return match.group(1)
    return None


def normalize_youtube_url(url: str) -> str:
    """Rewrite any recognized YouTube 本編 URL shape (youtu.be/<id>,
    youtube.com/watch with or without www./m., youtube.com/live/<id>, with
    or without tracking params) to the one canonical form the user wants
    every link to be: `https://www.youtube.com/watch?v=<id>` (`t` is kept
    if present; every other query param, including `list`, is dropped).
    URLs that don't match a recognized 本編 shape (shorts, channel URLs,
    non-YouTube URLs) are left as-is apart from stripping known tracking
    params -- there's no ID to rewrite to the canonical form, and
    `is_youtube_url()` is what flags them as not being a valid 本編 link.
    Idempotent: normalizing an already-canonical URL returns it unchanged."""
    parts = urllib.parse.urlsplit(url)
    query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = dict(query_pairs)

    video_id = _extract_youtube_video_id(url, query)
    if video_id:
        canonical_query = {"v": video_id}
        if "t" in query:
            canonical_query["t"] = query["t"]
        new_query = urllib.parse.urlencode(canonical_query)
        return urllib.parse.urlunsplit(
            ("https", CANONICAL_YOUTUBE_HOST, CANONICAL_YOUTUBE_PATH, new_query, "")
        )

    kept_pairs = [pair for pair in query_pairs if not _is_youtube_tracking_param(pair[0])]
    new_query = urllib.parse.urlencode(kept_pairs)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def is_youtube_url(url: str) -> bool:
    """True for a YouTube watch/short-link/live URL (youtube.com/watch,
    youtu.be/, youtube.com/live/) -- not e.g. a bare channel URL."""
    return bool(_YOUTUBE_URL_PATTERN.match(url.strip()))


def resolve_main_video_url(plan: dict[str, Any], clip: dict[str, Any]) -> str | None:
    """A clip's effective 本編 URL: its own `main_video_url` override if
    set, else the plan's top-level default. Normalized to the canonical
    `watch?v=<id>` form so every reader (validate/submit/gallery, the
    gate_submit hook) compares/displays the exact same string."""
    raw = clip.get("main_video_url") or plan.get("main_video_url")
    return normalize_youtube_url(raw) if raw else None


def validate_main_video_url_locally(clip: dict[str, Any], main_video_url: str | None) -> list[LocalIssue]:
    """Local-only validation for the resolved 本編(main video) URL: missing
    or non-YouTube `main_video_url`, and captions (on all 3 services --
    TikTok/Instagram links aren't clickable but the URL is still posted as
    plain text) that don't literally contain it. `main_video_url` must
    already be resolved (see `resolve_main_video_url`) by the caller."""
    no = clip["no"]
    issues: list[LocalIssue] = []
    if not main_video_url:
        message = "main_video_urlがありません（本編のYouTube URLを指定してください）"
        issues.append(LocalIssue(no, "main_video_url", "error", message))
    elif not is_youtube_url(main_video_url):
        message = f"main_video_urlがYouTubeのURL形式ではありません（{main_video_url}）"
        issues.append(LocalIssue(no, "main_video_url", "warning", message))

    for target in clip.get("targets", []):
        service = target.get("service")
        caption = target.get("caption", "")
        if service in _VALID_SERVICES and main_video_url and main_video_url not in caption:
            message = f"{service}: キャプションに本編URLが含まれていません"
            issues.append(LocalIssue(no, service, "error", message))
    return issues


# --- .env parsing (pure: takes text, not a path) ----------------------------

_DOTENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_dotenv_text(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines (skipping blanks/#comments), stripping a single
    layer of matching quotes from the value. Deliberately minimal -- this is
    only used to read SIZURU_API_URL/SIZURU_API_KEY out of the repo's .env."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _DOTENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


# --- submit result bookkeeping ----------------------------------------------

# Server-confirmed successes. A resend targets exactly the complement of
# this (plus "canceled" -- see _TERMINAL_STATUSES): re-sending an already
# scheduled/sent/draft target with a new due_at/caption is NOT how an edit
# works (that's the admin panel's job); only a "failed" row gets re-claimed
# and resent by the server on the next identical-video_key submission.
_SUCCESS_STATUSES = {"scheduled", "sent", "draft"}

# Statuses a resend must never touch: success statuses plus "canceled" (a
# user cancellation via the admin panel is final -- resubmitting must not
# resurrect it). Anything else (failed/unknown/not_attempted/...) still
# needs another submit() to make progress.
_TERMINAL_STATUSES = _SUCCESS_STATUSES | {"canceled"}


def is_fully_settled(target_results: list[dict[str, Any]]) -> bool:
    """True if a clip's previous submission_result already has every target
    in a terminal state (scheduled/sent/draft/canceled) -- i.e.
    re-submitting would be pointless (success) or harmful (would resurrect
    a cancellation). False whenever ANY target is `failed` (or any other
    non-terminal status): the server re-claims and resends only `failed`
    rows on a resend, so those clips must still go through `submit_clip`.
    Empty (nothing submitted yet) is not considered settled."""
    if not target_results:
        return False
    return all(r.get("status") in _TERMINAL_STATUSES for r in target_results)


def summarize_submit_results(clip_results: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate `submit`'s per-clip results into target-level counts for
    the CLI's summary and exit-code decision. A clip skipped by dry-run
    (has_error) or one that hit a clip-level ApiError both count once, at
    clip granularity, under "skipped"/"failed" respectively (they never got
    far enough to produce per-target statuses). A `canceled` target is an
    intentional, already-handled outcome (via the admin panel) -- it's not
    counted anywhere (not a "failure" to surface)."""
    summary = {"scheduled": 0, "failed": 0, "unknown": 0, "skipped": 0, "not_attempted": 0}
    for clip_result in clip_results:
        if clip_result.get("error") is not None:
            summary["failed"] += 1
            continue
        if clip_result.get("skipped"):
            summary["skipped"] += 1
            continue
        result = clip_result.get("result") or {}
        for target in result.get("results", []):
            status = target.get("status")
            if status in _SUCCESS_STATUSES:
                summary["scheduled"] += 1
            elif status == "canceled":
                continue
            elif status in summary:
                summary[status] += 1
            else:
                summary["failed"] += 1  # db_write_failed and any other unexpected status
    return summary


def submit_has_problems(clip_results: list[dict[str, Any]], summary: dict[str, int]) -> bool:
    """True if `submit` should exit nonzero: any clip skipped, any target
    failed/unknown/not_attempted, or any clip is still `incomplete` after
    its retries (the summary alone can't see "incomplete" -- it's a
    per-response flag, not a target status)."""
    if summary["failed"] or summary["unknown"] or summary["skipped"] or summary["not_attempted"]:
        return True
    return any((clip_result.get("result") or {}).get("incomplete") for clip_result in clip_results)


# --- gallery data merging ----------------------------------------------------


def merge_gallery_data(
    base: dict[str, Any], plan: dict[str, Any] | None, state: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge plan.json's targets/due_at and plan.json.state.json's
    submission results into base.json's clips (matched by clip `no`),
    building each clip's `schedule` block. `video_url` always comes from
    `base` (it's set by the artifact-upload step, independent of plan/state).
    `main_video_url` (the 本編 the posts drive traffic to) comes from `plan`
    when it has one -- authoritative once scheduling starts -- else `base`'s
    existing value (set during the artifact step, from hearing.md) is kept.
    With no `plan`, `base` is returned unchanged (the original single-file
    `gallery --data` usage)."""
    if plan is None:
        return base

    plan_clips_by_no = {clip["no"]: clip for clip in plan.get("clips", [])}
    state_clips = (state or {}).get("clips", {})

    merged_clips = []
    for clip in base.get("clips", []):
        plan_clip = plan_clips_by_no.get(clip["no"])
        if plan_clip is None:
            merged_clips.append(clip)
            continue
        clip_state = state_clips.get(str(clip["no"]))
        merged_clips.append({**clip, "schedule": _build_clip_schedule(plan_clip, clip_state)})

    merged = {**base, "clips": merged_clips}
    if plan.get("main_video_url"):
        merged["main_video_url"] = normalize_youtube_url(plan["main_video_url"])
    return merged


def _build_clip_schedule(
    plan_clip: dict[str, Any], clip_state: dict[str, Any] | None
) -> dict[str, Any] | None:
    targets = plan_clip.get("targets", [])
    if not targets:
        return None
    results_by_service = _target_results_by_service(clip_state)
    return {
        "due_at": targets[0].get("due_at"),
        "targets": [_merge_target(t, results_by_service.get(t["service"])) for t in targets],
    }


def _target_results_by_service(clip_state: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not clip_state:
        return {}
    submission_result = clip_state.get("submission_result") or {}
    return {r["service"]: r for r in submission_result.get("results", [])}


def _merge_target(target: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    service = target["service"]
    caption = target.get("caption", "")
    length, limit = _caption_length_and_limit(service, caption)
    return {
        "service": service,
        "caption": caption,
        "length": length,
        "limit": limit,
        "status": result.get("status", "unknown") if result is not None else "proposed",
        "external_link": result.get("external_link") if result is not None else None,
        "error": result.get("error") if result is not None else None,
    }


def _caption_length_and_limit(service: str, caption: str) -> tuple[int, int]:
    if service == "x":
        return approx_x_weighted_length(caption), _X_APPROX_LIMIT
    return len(unicodedata.normalize("NFC", caption)), _GENERIC_LIMIT
