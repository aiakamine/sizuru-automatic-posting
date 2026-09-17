"""Tests for sizuru_post.api._deadline: Lambda-context-based should_stop()."""

from __future__ import annotations

from sizuru_post.api._deadline import make_should_stop


class _Context:
    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


def test_returns_none_when_context_is_none() -> None:
    # Arrange / Act / Assert: local invocations/tests often pass context=None.
    assert make_should_stop(None) is None


def test_returns_none_when_context_lacks_the_method() -> None:
    # Arrange / Act / Assert
    assert make_should_stop(object()) is None


def test_should_stop_true_when_remaining_time_below_margin() -> None:
    # Arrange
    should_stop = make_should_stop(_Context(10_000))
    assert should_stop is not None

    # Act / Assert
    assert should_stop() is True


def test_should_stop_false_when_remaining_time_above_margin() -> None:
    # Arrange
    should_stop = make_should_stop(_Context(20_000))
    assert should_stop is not None

    # Act / Assert
    assert should_stop() is False
