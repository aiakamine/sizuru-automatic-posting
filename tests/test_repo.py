"""Light tests for sizuru_post.adapters.repo.SupabaseRepo: table/method wiring only.

Deep behavior (join semantics, RLS, DB CHECK constraints) lives in Supabase
itself (see supabase/migrations/20260915000000_init_posting.sql); here we
only verify SupabaseRepo talks to the expected tables with the expected
payload shapes, using a mocked supabase client (not over-mocked).
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, call
from uuid import uuid4

import pytest
from postgrest.exceptions import APIError

from sizuru_post.adapters.repo import SupabaseRepo, UniqueViolationError
from sizuru_post.domain.models import PostStatus


def _query_mock(return_data: list[dict[str, object]]) -> MagicMock:
    """Build a chainable mock matching supabase-py's fluent query builder."""
    query = MagicMock()
    query.execute.return_value = MagicMock(data=return_data)
    for method in ("select", "insert", "upsert", "update", "eq", "in_", "single", "gte", "lt", "order"):
        getattr(query, method).return_value = query
    # `.not_` is a modifier property (not a call) in postgrest-py's fluent
    # builder — `.not_.in_(...)` negates the *next* filter on the same query.
    query.not_ = query
    return query


def test_upsert_channels_writes_to_channels_table() -> None:
    # Arrange
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    repo.upsert_channels([{"buffer_channel_id": "c1", "service": "tiktok"}])

    # Assert
    client.table.assert_called_with("channels")
    table.upsert.assert_called_once()
    (payload,), kwargs = table.upsert.call_args
    assert payload == [{"buffer_channel_id": "c1", "service": "tiktok"}]
    assert kwargs.get("on_conflict") == "buffer_channel_id"


def test_upsert_channels_ignores_unknown_services() -> None:
    # Arrange: Buffer may return channels for services we don't support (e.g. linkedin)
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    repo.upsert_channels(
        [
            {"buffer_channel_id": "c1", "service": "tiktok"},
            {"buffer_channel_id": "c2", "service": "linkedin"},
        ]
    )

    # Assert
    (payload,), _ = table.upsert.call_args
    assert payload == [{"buffer_channel_id": "c1", "service": "tiktok"}]


def test_upsert_channels_skips_call_when_nothing_valid() -> None:
    # Arrange
    client = MagicMock()
    repo = SupabaseRepo(client)

    # Act
    repo.upsert_channels([{"buffer_channel_id": "c2", "service": "linkedin"}])

    # Assert
    client.table.assert_not_called()


def test_upsert_channels_marks_channels_missing_from_the_list_as_disconnected() -> None:
    # Arrange: when a Buffer channel is replaced, the old row's
    # buffer_channel_id no longer appears in this sync's list at all — it
    # must be marked disconnected, not silently left is_disconnected=False
    # forever (root cause of a live incident: the CLI `channels` command
    # upserted the new channel but never flagged the old one as gone).
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    repo.upsert_channels(
        [
            {"buffer_channel_id": "c1", "service": "tiktok"},
            {"buffer_channel_id": "c2", "service": "x"},
        ]
    )

    # Assert: the upsert, then a separate update disconnecting everything
    # NOT in this batch.
    assert client.table.call_args_list == [call("channels"), call("channels")]
    table.update.assert_called_once_with({"is_disconnected": True})
    table.not_.in_.assert_called_once_with("buffer_channel_id", ["c1", "c2"])


def test_upsert_channels_does_not_disconnect_anything_when_nothing_valid() -> None:
    # Arrange: an empty/failed Buffer fetch (or a batch with only unknown
    # services) must never mass-disconnect every channel in the DB.
    client = MagicMock()
    repo = SupabaseRepo(client)

    # Act
    repo.upsert_channels([])

    # Assert
    client.table.assert_not_called()


def test_list_channels_returns_channel_rows() -> None:
    # Arrange
    channel_id = str(uuid4())
    client = MagicMock()
    table = _query_mock(
        [
            {
                "id": channel_id,
                "buffer_channel_id": "c1",
                "service": "tiktok",
                "name": "n",
                "display_name": None,
                "is_disconnected": False,
                "is_queue_paused": False,
            }
        ]
    )
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    channels = repo.list_channels()

    # Assert
    assert len(channels) == 1
    assert channels[0].buffer_channel_id == "c1"
    assert channels[0].service == "tiktok"
    assert channels[0].display_name is None


def test_create_submission_inserts_thumbnail_and_duration_and_returns_id() -> None:
    # Arrange
    submission_id = str(uuid4())
    client = MagicMock()
    table = _query_mock([{"id": submission_id}])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    result_id = repo.create_submission(
        title="t",
        s3_key="public/videos/2026/01/x.mp4",
        video_url="https://bucket.s3.region.amazonaws.com/public/videos/2026/01/x.mp4",
        thumbnail_url="https://bucket.s3.region.amazonaws.com/public/thumbnails/2026/01/x.jpg",
        duration_sec=12.3,
        approved_at=None,  # type: ignore[arg-type]
    )

    # Assert
    client.table.assert_called_with("submissions")
    (payload,), _ = table.insert.call_args
    assert (
        payload["thumbnail_url"] == "https://bucket.s3.region.amazonaws.com/public/thumbnails/2026/01/x.jpg"
    )
    assert payload["duration_sec"] == 12.3
    assert "mode" not in payload
    assert "due_at" not in payload
    assert "is_draft" not in payload
    assert str(result_id) == submission_id


def test_create_submission_reraises_unique_violation_as_dedicated_error_type() -> None:
    # Arrange: two concurrent requests for the same video (same s3_key) —
    # Publisher.publish_uploaded must be able to catch this specifically
    # and re-fetch the winning row, rather than the request failing with a
    # raw/opaque database error.
    client = MagicMock()
    table = _query_mock([])
    table.execute.side_effect = APIError(
        {"code": "23505", "message": 'duplicate key value violates unique constraint "submissions_s3_key_key"'}
    )
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act / Assert
    with pytest.raises(UniqueViolationError):
        repo.create_submission(
            title="t",
            s3_key="public/videos/2026/01/x.mp4",
            video_url="https://bucket.s3.region.amazonaws.com/public/videos/2026/01/x.mp4",
            thumbnail_url=None,
            duration_sec=12.3,
            approved_at=None,  # type: ignore[arg-type]
        )


def test_create_submission_reraises_other_database_errors_unchanged() -> None:
    # Arrange: only a unique-violation (23505) is special-cased — anything
    # else must propagate as-is, not be silently swallowed.
    client = MagicMock()
    table = _query_mock([])
    table.execute.side_effect = APIError({"code": "42501", "message": "permission denied for table submissions"})
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act / Assert
    with pytest.raises(APIError):
        repo.create_submission(
            title="t",
            s3_key="public/videos/2026/01/x.mp4",
            video_url="https://bucket.s3.region.amazonaws.com/public/videos/2026/01/x.mp4",
            thumbnail_url=None,
            duration_sec=12.3,
            approved_at=None,  # type: ignore[arg-type]
        )


def test_claim_pending_post_inserts_and_reports_created_true_when_no_conflict() -> None:
    # Arrange
    post_id = str(uuid4())
    client = MagicMock()
    table = _query_mock(
        [
            {
                "id": post_id,
                "submission_id": str(uuid4()),
                "channel_id": str(uuid4()),
                "caption": "hi",
                "mode": "addToQueue",
                "due_at": None,
                "is_draft": False,
                "platform_options": {"tiktok_title": "T"},
                "buffer_post_id": None,
                "status": "pending",
                "retry_count": 0,
            }
        ]
    )
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    row, created = repo.claim_pending_post(
        submission_id=uuid4(),
        channel_id=uuid4(),
        caption="hi",
        mode="addToQueue",
        due_at=None,
        is_draft=False,
        platform_options={"tiktok_title": "T"},
    )

    # Assert: ON CONFLICT DO NOTHING (ignore_duplicates), not the old
    # blind "upsert always wins" — concurrent claims must not both
    # succeed and both think they should call Buffer.
    client.table.assert_called_with("posts")
    (payload,), kwargs = table.upsert.call_args
    assert payload["mode"] == "addToQueue"
    assert payload["is_draft"] is False
    assert payload["platform_options"] == {"tiktok_title": "T"}
    assert payload["status"] == "pending"
    assert kwargs.get("on_conflict") == "submission_id,channel_id"
    assert kwargs.get("ignore_duplicates") is True
    assert created is True
    assert row.mode == "addToQueue"
    assert row.platform_options == {"tiktok_title": "T"}


def test_claim_pending_post_reselects_existing_row_and_reports_created_false_on_conflict() -> None:
    # Arrange: ON CONFLICT DO NOTHING + RETURNING gives back NO rows at all
    # for a conflicting insert (not the pre-existing row) — a concurrent
    # caller already claimed this (submission_id, channel_id), so the
    # winning row must be re-selected rather than treating an empty
    # response as if nothing exists.
    submission_id = uuid4()
    channel_id = uuid4()
    existing_post_id = str(uuid4())
    client = MagicMock()
    upsert_table = _query_mock([])  # empty: conflict, nothing inserted
    select_table = _query_mock(
        [
            {
                "id": existing_post_id,
                "submission_id": str(submission_id),
                "channel_id": str(channel_id),
                "caption": "hi",
                "mode": "addToQueue",
                "due_at": None,
                "is_draft": False,
                "platform_options": {},
                "buffer_post_id": "buf-1",
                "status": "scheduled",
                "retry_count": 0,
            }
        ]
    )
    client.table.side_effect = [upsert_table, select_table]
    repo = SupabaseRepo(client)

    # Act
    row, created = repo.claim_pending_post(
        submission_id=submission_id,
        channel_id=channel_id,
        caption="hi",
        mode="addToQueue",
        due_at=None,
        is_draft=False,
        platform_options={},
    )

    # Assert
    assert created is False
    assert str(row.id) == existing_post_id
    assert row.status == PostStatus.SCHEDULED
    select_table.eq.assert_any_call("submission_id", str(submission_id))
    select_table.eq.assert_any_call("channel_id", str(channel_id))


def test_claim_pending_post_conflict_with_no_reselectable_row_raises_runtime_error() -> None:
    # Arrange: ON CONFLICT DO NOTHING returned no row (a conflict happened),
    # but the follow-up re-select also finds nothing — this should be
    # impossible (the conflict itself guarantees a row exists) and must
    # surface loudly (e.g. read-replica lag) rather than silently being
    # reported as "created".
    client = MagicMock()
    upsert_table = _query_mock([])  # empty: conflict, nothing inserted
    select_table = _query_mock([])  # empty: re-select also finds nothing
    client.table.side_effect = [upsert_table, select_table]
    repo = SupabaseRepo(client)

    # Act / Assert
    with pytest.raises(RuntimeError, match="競合を検出しましたが該当行が見つかりません"):
        repo.claim_pending_post(
            submission_id=uuid4(),
            channel_id=uuid4(),
            caption="hi",
            mode="addToQueue",
            due_at=None,
            is_draft=False,
            platform_options={},
        )


def test_update_post_sends_only_given_fields() -> None:
    # Arrange
    post_id = uuid4()
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    repo.update_post(post_id, status=PostStatus.FAILED, error_message="boom")

    # Assert
    client.table.assert_called_with("posts")
    (payload,), _ = table.update.call_args
    assert payload == {"status": "failed", "error_message": "boom"}
    table.eq.assert_called_with("id", str(post_id))


def test_get_post_returns_row_by_id() -> None:
    # Arrange
    post_id = uuid4()
    client = MagicMock()
    table = _query_mock(
        [
            {
                "id": str(post_id),
                "submission_id": str(uuid4()),
                "channel_id": str(uuid4()),
                "caption": "hi",
                "mode": "shareNow",
                "due_at": None,
                "is_draft": False,
                "platform_options": {},
                "buffer_post_id": "buf-1",
                "status": "scheduled",
                "retry_count": 0,
            }
        ]
    )
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    row = repo.get_post(post_id)

    # Assert
    client.table.assert_called_with("posts")
    table.eq.assert_called_with("id", str(post_id))
    assert row is not None
    assert row.buffer_post_id == "buf-1"


def test_get_post_returns_none_when_missing() -> None:
    # Arrange
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    row = repo.get_post(uuid4())

    # Assert
    assert row is None


def test_list_syncable_posts_filters_by_scheduled_and_draft_status() -> None:
    # Arrange: sync() must repair drafts too (see Publisher.sync), not just
    # scheduled posts.
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    repo.list_syncable_posts()

    # Assert
    table.in_.assert_called_with("status", ["scheduled", "draft"])


def test_list_pending_posts_filters_by_pending_status() -> None:
    # Arrange
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    repo.list_pending_posts()

    # Assert
    table.eq.assert_called_with("status", "pending")


def test_list_posts_by_submission_filters_by_submission_id() -> None:
    # Arrange
    submission_id = uuid4()
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    repo.list_posts_by_submission(submission_id)

    # Assert
    table.eq.assert_called_with("submission_id", str(submission_id))


def test_claim_failed_post_updates_conditionally_and_returns_row() -> None:
    # Arrange: atomic failed->pending claim, guarded by a WHERE status='failed'
    post_id = uuid4()
    client = MagicMock()
    table = _query_mock(
        [
            {
                "id": str(post_id),
                "submission_id": str(uuid4()),
                "channel_id": str(uuid4()),
                "caption": "hi",
                "mode": "addToQueue",
                "due_at": None,
                "is_draft": False,
                "platform_options": {},
                "buffer_post_id": "buf-1",
                "status": "pending",
                "retry_count": 1,
            }
        ]
    )
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    row = repo.claim_failed_post(post_id)

    # Assert
    client.table.assert_called_with("posts")
    (payload,), _ = table.update.call_args
    assert payload == {"status": "pending"}
    assert table.eq.call_args_list == [
        call("id", str(post_id)),
        call("status", "failed"),
    ]
    assert row is not None
    assert row.status == PostStatus.PENDING


def test_claim_failed_post_returns_none_when_not_failed() -> None:
    # Arrange: no row matched status='failed' -> Supabase returns empty data
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    row = repo.claim_failed_post(uuid4())

    # Assert
    assert row is None


def test_is_admin_returns_true_when_row_exists() -> None:
    # Arrange
    client = MagicMock()
    table = _query_mock([{"user_id": "some-uuid"}])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    result = repo.is_admin("some-uuid")

    # Assert
    client.table.assert_called_with("admin_users")
    table.eq.assert_called_with("user_id", "some-uuid")
    assert result is True


def test_is_admin_returns_false_when_no_row() -> None:
    # Arrange
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    result = repo.is_admin("some-uuid")

    # Assert
    assert result is False


def test_get_submission_by_s3_key_returns_row_when_found() -> None:
    # Arrange
    submission_id = str(uuid4())
    client = MagicMock()
    table = _query_mock(
        [
            {
                "id": submission_id,
                "title": "t",
                "s3_key": "public/videos/2026/01/x.mp4",
                "video_url": "https://bucket.s3.region.amazonaws.com/public/videos/2026/01/x.mp4",
                "thumbnail_url": None,
                "duration_sec": 12.3,
                "approved_at": "2026-01-01T00:00:00+00:00",
            }
        ]
    )
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    row = repo.get_submission_by_s3_key("public/videos/2026/01/x.mp4")

    # Assert
    client.table.assert_called_with("submissions")
    table.eq.assert_called_with("s3_key", "public/videos/2026/01/x.mp4")
    assert row is not None
    assert str(row.id) == submission_id


def test_get_submission_by_s3_key_returns_none_when_missing() -> None:
    # Arrange
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    row = repo.get_submission_by_s3_key("public/videos/2026/01/missing.mp4")

    # Assert
    assert row is None


def test_list_posts_in_range_queries_the_expected_window_and_excludes_canceled_and_failed() -> None:
    # Arrange
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 8, tzinfo=UTC)

    # Act
    repo.list_posts_in_range(start, end)

    # Assert
    client.table.assert_called_with("posts")
    table.gte.assert_called_with("due_at", start.isoformat())
    table.lt.assert_called_with("due_at", end.isoformat())
    table.in_.assert_called_with("status", ["canceled", "failed"])


def test_list_posts_in_range_orders_by_due_at_then_id() -> None:
    # Arrange: `.order(due_at).order(id)` is a stable sort (due_at primary,
    # id as the tiebreaker for same-instant posts) — the scheduling skill's
    # calendar view depends on a deterministic order, not just "some" order.
    client = MagicMock()
    table = _query_mock([])
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    repo.list_posts_in_range(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 8, tzinfo=UTC))

    # Assert
    assert table.order.call_args_list == [call("due_at"), call("id")]


def test_list_posts_in_range_flattens_embedded_channel_and_submission_title() -> None:
    # Arrange
    post_id = str(uuid4())
    submission_id = str(uuid4())
    client = MagicMock()
    table = _query_mock(
        [
            {
                "id": post_id,
                "submission_id": submission_id,
                "due_at": "2026-01-02T09:00:00+00:00",
                "status": "scheduled",
                "channels": {"service": "tiktok"},
                "submissions": {"title": "My clip"},
            }
        ]
    )
    client.table.return_value = table
    repo = SupabaseRepo(client)

    # Act
    rows = repo.list_posts_in_range(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 8, tzinfo=UTC))

    # Assert
    assert len(rows) == 1
    assert str(rows[0].post_id) == post_id
    assert str(rows[0].submission_id) == submission_id
    assert rows[0].service == "tiktok"
    assert rows[0].status == "scheduled"
    assert rows[0].title == "My clip"
