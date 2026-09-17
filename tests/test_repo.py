"""Light tests for sizuru_post.adapters.repo.SupabaseRepo: table/method wiring only.

Deep behavior (join semantics, RLS, DB CHECK constraints) lives in Supabase
itself (see supabase/migrations/20260915000000_init_posting.sql); here we
only verify SupabaseRepo talks to the expected tables with the expected
payload shapes, using a mocked supabase client (not over-mocked).
"""

from unittest.mock import MagicMock, call
from uuid import uuid4

from sizuru_post.adapters.repo import SupabaseRepo
from sizuru_post.domain.models import PostStatus


def _query_mock(return_data: list[dict[str, object]]) -> MagicMock:
    """Build a chainable mock matching supabase-py's fluent query builder."""
    query = MagicMock()
    query.execute.return_value = MagicMock(data=return_data)
    for method in ("select", "insert", "upsert", "update", "eq", "in_", "single"):
        getattr(query, method).return_value = query
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


def test_upsert_post_pending_writes_timing_and_platform_options() -> None:
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
    row = repo.upsert_post_pending(
        submission_id=uuid4(),
        channel_id=uuid4(),
        caption="hi",
        mode="addToQueue",
        due_at=None,
        is_draft=False,
        platform_options={"tiktok_title": "T"},
    )

    # Assert
    client.table.assert_called_with("posts")
    (payload,), kwargs = table.upsert.call_args
    assert payload["mode"] == "addToQueue"
    assert payload["is_draft"] is False
    assert payload["platform_options"] == {"tiktok_title": "T"}
    assert payload["status"] == "pending"
    assert kwargs.get("on_conflict") == "submission_id,channel_id"
    assert row.mode == "addToQueue"
    assert row.platform_options == {"tiktok_title": "T"}


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
