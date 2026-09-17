"""EventBridge Scheduler target Lambda: calls Publisher.sync() every 15
minutes (see infra/sam/template.yaml). No API Gateway involved — no auth,
no HTTP response shape.

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
from typing import Any

from sizuru_post.adapters.buffer import BufferAuthError
from sizuru_post.api._deadline import make_should_stop
from sizuru_post.factory import build_publisher_without_uploads, build_settings, reset_ssm_cache
from sizuru_post.publisher import Publisher

logger = logging.getLogger(__name__)

_publisher: Publisher | None = None


def get_publisher() -> Publisher:
    """Build the Publisher lazily, once per container."""
    global _publisher
    if _publisher is None:
        _publisher = build_publisher_without_uploads(build_settings())
    return _publisher


def reset_publisher() -> None:
    """Drop the cached Publisher and SSM secret cache so the *next*
    invocation rebuilds from a fresh SSM read (the API key may have been
    rotated). Does NOT retry the current invocation."""
    global _publisher
    _publisher = None
    reset_ssm_cache()


def handler(event: dict[str, Any], context: object = None) -> dict[str, int]:
    publisher = get_publisher()
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
