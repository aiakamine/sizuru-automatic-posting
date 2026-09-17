"""Shared Lambda-context deadline check, used by Publisher.sync()'s
`should_stop` callback so a long-running sync stops cleanly instead of
being killed mid-post when a Lambda invocation is about to time out.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Leave enough headroom to finish the in-flight repo write and return a
# clean response instead of being killed mid-request.
_STOP_MARGIN_MS = 15_000


def make_should_stop(context: Any) -> Callable[[], bool] | None:
    """Build a should_stop() callable from a Lambda context's remaining
    execution time, or None if no usable context was given.

    `context` is only a real `LambdaContext` (with
    `get_remaining_time_in_millis()`) during an actual Lambda invocation;
    local/test callers often pass `context=None`, so this must fail safe
    (never stop) rather than raise when it's unavailable.
    """
    get_remaining = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(get_remaining):
        return None

    def _should_stop() -> bool:
        return bool(get_remaining() < _STOP_MARGIN_MS)

    return _should_stop
