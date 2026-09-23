"""EventBridge Scheduler target Lambda: calls Publisher.sync() every 15
minutes (see infra/sam/template.yaml). No API Gateway involved — no auth,
no HTTP response shape.

Also does a best-effort Buffer channels -> Supabase refresh on every
invocation (see channels_sync.sync_channels) — this catches a channel
Buffer replaced (old row should become is_disconnected=True) even if no
one happens to run the CLI's `channels` command, which was previously the
*only* place this sync ran (root cause of a live incident: a replaced
channel was never marked disconnected until someone noticed and fixed it
by hand). A channel-refresh failure is logged and swallowed, never
allowed to block the more time-sensitive post-status sync below.

A BufferAuthError (invalid/rotated API key) is caught only long enough to
reset the SSM/Publisher cache (see reset_publisher) and is then always
re-raised, never swallowed: an unhandled exception makes the Lambda
invocation fail, which EventBridge records and CloudWatch alarms can act
on — silently succeeding would let a broken API key go unnoticed
indefinitely.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from sizuru_post.adapters.buffer import BufferAuthError, BufferClient
from sizuru_post.api._deadline import make_should_stop
from sizuru_post.channels_sync import sync_channels
from sizuru_post.config import Settings
from sizuru_post.factory import build_publisher_without_uploads, build_settings, reset_ssm_cache
from sizuru_post.publisher import Publisher

logger = logging.getLogger(__name__)

_settings: Settings | None = None
_publisher: Publisher | None = None


def get_settings() -> Settings:
    """Build Settings lazily, once per container.

    include_scheduler_key=False: this Lambda (SyncFunction) has no IAM
    grant for the SCHEDULER_API_KEY SSM parameter — only ApiFunction does,
    since only it serves /schedule/* routes (see infra/sam/template.yaml).
    Requesting it anyway would raise an unhandled AccessDenied here (see
    factory.build_settings's docstring) — this is exactly what took
    SyncFunction down in production after the scheduling API's first
    deploy.
    """
    global _settings
    if _settings is None:
        _settings = build_settings(include_scheduler_key=False)
    return _settings


def get_publisher() -> Publisher:
    """Build the Publisher lazily, once per container."""
    global _publisher
    if _publisher is None:
        _publisher = build_publisher_without_uploads(get_settings())
    return _publisher


def reset_publisher() -> None:
    """Drop the cached Settings/Publisher and SSM secret cache so the
    *next* invocation rebuilds from a fresh SSM read (the API key may
    have been rotated). Does NOT retry the current invocation."""
    global _settings, _publisher
    _settings = None
    _publisher = None
    reset_ssm_cache()


def _refresh_channels(publisher: Publisher, settings: Settings) -> None:
    """Best-effort — see this module's docstring. Never lets a Buffer/
    Supabase failure here propagate and abort the post-status sync."""
    try:
        # publisher._buffer is typed as Publisher's own minimal BufferPort
        # (create_post/get_post/edit_post/delete_post only), but
        # build_publisher_without_uploads() always wires a real
        # BufferClient there, which also has get_organizations/get_channels.
        buffer = cast(BufferClient, publisher._buffer)  # noqa: SLF001 - same package, intentional port access
        sync_channels(buffer, publisher._repo, settings.buffer_organization_id)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - best-effort, must not block publisher.sync() below
        logger.warning("チャンネル情報の同期に失敗しました", exc_info=True)


def handler(event: dict[str, Any], context: object = None) -> dict[str, int]:
    settings = get_settings()
    publisher = get_publisher()
    _refresh_channels(publisher, settings)
    try:
        # full=False: only posts whose due_at has passed (or is null) are
        # queried, since this runs frequently (see template.yaml's
        # schedule) — unlike the manual /sync API endpoint (full=True).
        counts = publisher.sync(full=False, should_stop=make_should_stop(context))
    except BufferAuthError:
        # The API key itself was rejected — it may have been rotated away
        # in SSM. Reset the cache so the *next* invocation re-reads it
        # fresh, then re-raise so this invocation still fails visibly
        # (CloudWatch/EventBridge records it) instead of being swallowed.
        logger.error("Buffer認証エラーが発生しました")
        reset_publisher()
        raise
    logger.info(json.dumps(counts, ensure_ascii=False))
    return counts
