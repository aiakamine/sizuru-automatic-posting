"""Tests for sizuru_post.publisher.Publisher, using in-memory fakes (no network)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from sizuru_post.adapters.buffer import (
    BufferAmbiguousError,
    BufferAuthError,
    BufferProtocolError,
    CreatePostResult,
)
from sizuru_post.adapters.repo import ChannelRow
from sizuru_post.domain.models import PostStatus, PublishRequest, Target, Timing, VideoInfo
from sizuru_post.publisher import ApprovalRequiredError, Publisher, RequestValidationError
from tests.fakes import FakeBuffer, FakeStorage, InMemoryRepo

VALID_VIDEO = VideoInfo(duration_sec=30.0, width=1080, height=1920, video_codec="h264", audio_codec="aac")


def _channel(service: str, **overrides: object) -> ChannelRow:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "buffer_channel_id": f"buf-{service}",
        "service": service,
        "name": service,
        "display_name": service,
        "is_disconnected": False,
        "is_queue_paused": False,
    }
    defaults.update(overrides)
    return ChannelRow(**defaults)  # type: ignore[arg-type]


def _request(targets: list[Target], **overrides: object) -> PublishRequest:
    defaults: dict[str, object] = {
        "video_path": Path("clip.mp4"),
        "timing": Timing(mode="addToQueue"),
        "targets": targets,
    }
    defaults.update(overrides)
    return PublishRequest(**defaults)  # type: ignore[arg-type]


def _future(seconds: int = 3600) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


@pytest.fixture
def channels() -> list[ChannelRow]:
    return [_channel("tiktok"), _channel("instagram"), _channel("x")]


@pytest.fixture
def repo(channels: list[ChannelRow]) -> InMemoryRepo:
    return InMemoryRepo(channels=channels)


@pytest.fixture
def buffer() -> FakeBuffer:
    return FakeBuffer()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


def _fake_thumbnail(video_path: Path, out_path: Path, offset_ms: int | None) -> None:
    out_path.write_bytes(b"fake-jpeg")


FIXED_NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def publisher(buffer: FakeBuffer, storage: FakeStorage, repo: InMemoryRepo) -> Publisher:
    return Publisher(
        buffer=buffer,
        storage=storage,
        repo=repo,
        probe=lambda path: VALID_VIDEO,  # type: ignore[arg-type]
        thumbnail=_fake_thumbnail,
        clock=lambda: FIXED_NOW,
    )


def _targets(*services: str) -> list[Target]:
    return [Target(service=s, caption="hello") for s in services]  # type: ignore[arg-type]


class TestPublishHappyPath:
    def test_all_three_targets_succeed(self, publisher: Publisher) -> None:
        # Arrange
        request = _request(_targets("tiktok", "instagram", "x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        assert report.submission_id is not None
        assert {r.service: r.status for r in report.results} == {
            "tiktok": "scheduled",
            "instagram": "scheduled",
            "x": "scheduled",
        }

    def test_draft_request_marks_posts_as_draft(self, publisher: Publisher) -> None:
        # Arrange
        request = _request(_targets("x"), draft=True)

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        assert report.results[0].status == "draft"

    def test_buffer_sent_status_maps_to_sent_and_sets_sent_at(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        buffer.results["x"] = CreatePostResult(ok=True, post_id="p1", status="sent")
        request = _request(_targets("x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        assert report.results[0].status == "sent"
        submission_id = report.submission_id
        assert submission_id is not None
        row = publisher._repo.list_posts_by_submission(submission_id)[0]
        assert row.sent_at is not None

    @pytest.mark.parametrize(
        ("remote_status", "expected_local_status"),
        [
            ("scheduled", PostStatus.SCHEDULED),
            ("sending", PostStatus.SCHEDULED),
            ("needs_approval", PostStatus.SCHEDULED),
            ("sent", PostStatus.SENT),
            ("draft", PostStatus.DRAFT),
            ("error", PostStatus.FAILED),
        ],
    )
    def test_immediate_buffer_status_maps_to_local_status(
        self,
        publisher: Publisher,
        buffer: FakeBuffer,
        remote_status: str,
        expected_local_status: PostStatus,
    ) -> None:
        # Arrange: Buffer's real PostStatus enum is
        # draft|error|needs_approval|scheduled|sending|sent
        buffer.results["x"] = CreatePostResult(ok=True, post_id="p1", status=remote_status)
        request = _request(_targets("x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.status == expected_local_status
        assert report.results[0].status == expected_local_status.value
        if expected_local_status == PostStatus.FAILED:
            # DB CHECK posts_failed_has_error requires error_message NOT NULL
            assert row.error_message is not None
            assert report.results[0].error is not None

    def test_approval_stamps_approved_at_on_submission(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange
        request = _request(_targets("x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        submission = repo.get_submission(report.submission_id)  # type: ignore[arg-type]
        assert submission is not None
        assert submission.approved_at is not None

    def test_platform_options_persisted_for_retry_fidelity(self, publisher: Publisher) -> None:
        # Arrange
        target = Target(service="tiktok", caption="hi", tiktok_title="My Title")
        request = _request([target])

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.platform_options == {"tiktok_title": "My Title"}

    def test_thumbnail_generated_and_url_stored_on_submission(
        self, publisher: Publisher, repo: InMemoryRepo, storage: FakeStorage
    ) -> None:
        # Arrange
        request = _request(_targets("x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        assert len(storage.thumbnail_calls) == 1
        submission = repo.get_submission(report.submission_id)  # type: ignore[arg-type]
        assert submission is not None
        assert submission.thumbnail_url == storage.thumbnail.public_url
        assert submission.duration_sec == VALID_VIDEO.duration_sec

    def test_thumbnail_failure_does_not_block_posting(
        self, publisher: Publisher, repo: InMemoryRepo, storage: FakeStorage
    ) -> None:
        # Arrange
        storage.thumbnail_exception = RuntimeError("ffmpeg missing")
        request = _request(_targets("x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert: posting still succeeds, thumbnail_url is just null
        assert report.results[0].status == "scheduled"
        submission = repo.get_submission(report.submission_id)  # type: ignore[arg-type]
        assert submission is not None
        assert submission.thumbnail_url is None

    def test_per_target_timing_override_is_persisted_on_that_posts_row(self, publisher: Publisher) -> None:
        # Arrange: request defaults to addToQueue, but the tiktok target opts
        # into a specific schedule
        due_at = _future()
        tiktok_target = Target(
            service="tiktok", caption="hi", timing=Timing(mode="customScheduled", due_at=due_at)
        )
        x_target = Target(service="x", caption="hi")
        request = _request([tiktok_target, x_target])

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        rows = {p.channel_id: p for p in publisher._repo.list_posts_by_submission(report.submission_id)}  # type: ignore[arg-type]
        planned_channels = {t.target.service: t for t in publisher.plan(request).targets}
        tiktok_row = rows[planned_channels["tiktok"].channel.id]  # type: ignore[union-attr]
        x_row = rows[planned_channels["x"].channel.id]  # type: ignore[union-attr]
        assert tiktok_row.mode == "customScheduled"
        assert tiktok_row.due_at == due_at
        assert x_row.mode == "addToQueue"
        assert x_row.due_at is None

    def test_thumbnail_offset_ms_persisted_in_platform_options(self, publisher: Publisher) -> None:
        # Arrange
        request = _request(_targets("x"), thumbnail_offset_ms=2500)

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.platform_options.get("thumbnail_offset_ms") == 2500


class TestPublishPartialFailure:
    def test_mutation_error_without_message_still_persists_non_null_error_message(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: DB CHECK posts_failed_has_error requires error_message NOT NULL
        # whenever status='failed', so a malformed Buffer response (error=None)
        # must not violate that constraint.
        buffer.results["x"] = CreatePostResult(ok=False, error=None)
        request = _request(_targets("x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        assert report.results[0].status == "failed"
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.error_message is not None

    def test_one_service_buffer_failure_does_not_block_others(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        buffer.results["x"] = CreatePostResult(ok=False, error="rate limited")
        request = _request(_targets("tiktok", "instagram", "x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        results = {r.service: r for r in report.results}
        assert results["x"].status == "failed"
        assert results["x"].error == "rate limited"
        assert results["x"].post_id is not None  # DB row exists even though Buffer rejected it
        assert results["x"].buffer_post_id is None
        assert results["tiktok"].status == "scheduled"
        assert results["instagram"].status == "scheduled"

    def test_buffer_exception_marks_target_failed_without_stopping_others(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        buffer.exceptions["x"] = RuntimeError("boom")
        request = _request(_targets("tiktok", "x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        results = {r.service: r for r in report.results}
        assert results["x"].status == "failed"
        assert "boom" in (results["x"].error or "")
        assert results["tiktok"].status == "scheduled"

    def test_ambiguous_create_post_leaves_pending_and_reports_unknown(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: a 5xx/transport/timeout during createPost means we don't know
        # if Buffer actually created the post — must not be marked failed (which
        # would invite an unsafe blind retry) or scheduled (which would be a lie).
        buffer.exceptions["x"] = BufferAmbiguousError("boom")
        request = _request(_targets("tiktok", "x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        results = {r.service: r for r in report.results}
        assert results["x"].status == "unknown"
        assert results["tiktok"].status == "scheduled"
        x_channel_id = next(c.id for c in publisher._repo.list_channels() if c.service == "x")
        row = next(
            p
            for p in publisher._repo.list_posts_by_submission(report.submission_id)  # type: ignore[arg-type]
            if p.channel_id == x_channel_id
        )
        assert row.status == PostStatus.PENDING
        assert row.buffer_post_id is None
        assert row.error_message is not None
        # the DB post id must still be reported so it can be looked up later
        # (there's no Buffer id yet to identify it by)
        assert results["x"].post_id == row.id
        assert results["x"].buffer_post_id is None

    def test_protocol_error_after_create_leaves_pending_and_reports_unknown(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: a malformed/undecodable "success" response after the
        # mutation was dispatched is just as ambiguous as a 5xx/transport
        # error — Buffer may have created the post despite the bad response.
        buffer.exceptions["x"] = BufferProtocolError("boom")
        request = _request(_targets("tiktok", "x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        results = {r.service: r for r in report.results}
        assert results["x"].status == "unknown"
        assert results["tiktok"].status == "scheduled"
        x_channel_id = next(c.id for c in publisher._repo.list_channels() if c.service == "x")
        row = next(
            p
            for p in publisher._repo.list_posts_by_submission(report.submission_id)  # type: ignore[arg-type]
            if p.channel_id == x_channel_id
        )
        assert row.status == PostStatus.PENDING
        assert row.buffer_post_id is None

    def test_invalid_caption_skips_only_that_target(self, publisher: Publisher) -> None:
        # Arrange: X caption too long (weighted > 280), others fine
        targets = [
            Target(service="x", caption="あ" * 141),
            Target(service="tiktok", caption="ok"),
        ]
        request = _request(targets)

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        results = {r.service: r for r in report.results}
        assert results["x"].status == "skipped"
        assert any(i.level == "error" for i in results["x"].issues)
        assert results["tiktok"].status == "scheduled"

    def test_all_targets_invalid_uploads_nothing(self, publisher: Publisher, storage: FakeStorage) -> None:
        # Arrange
        targets = [Target(service="x", caption="あ" * 141)]
        request = _request(targets)

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        assert report.submission_id is None
        assert report.results[0].status == "skipped"
        assert storage.video_calls == []


class TestPublishIdempotency:
    def test_rerun_skips_already_scheduled_target(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange
        request = _request(_targets("x"))
        first_report = publisher.publish(request, approved=True)
        submission_id = first_report.submission_id
        assert submission_id is not None

        plan = publisher.plan(request)
        planned = plan.targets[0]

        # Act: re-invoke the per-target publish path for the same submission/channel
        second_result = publisher._publish_one(
            request, planned, submission_id, "https://cdn.example.com/x.mp4"
        )

        # Assert: no second call reached Buffer
        assert len(buffer.calls) == 1
        assert second_result.status == "scheduled"


class TestPublishApprovalAndUploadFailure:
    def test_not_approved_raises_and_performs_no_side_effects(
        self, publisher: Publisher, buffer: FakeBuffer, storage: FakeStorage, repo: InMemoryRepo
    ) -> None:
        # Arrange
        request = _request(_targets("x"))

        # Act / Assert
        with pytest.raises(ApprovalRequiredError):
            publisher.publish(request, approved=False)
        assert buffer.calls == []
        assert storage.video_calls == []
        assert repo.list_syncable_posts() == []

    def test_storage_failure_prevents_buffer_calls_and_reports_failure(
        self, publisher: Publisher, buffer: FakeBuffer, storage: FakeStorage
    ) -> None:
        # Arrange
        storage.video_exception = RuntimeError("s3 down")
        request = _request(_targets("tiktok", "x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert
        assert report.submission_id is None
        assert buffer.calls == []
        assert all(r.status == "failed" for r in report.results)


class _RepoWithUpdateFailureAfterSuccess:
    """Wraps a Repo and fails only the update_post call that follows a
    successful Buffer create_post for one specific Buffer post id."""

    def __init__(self, inner: InMemoryRepo, fail_for_buffer_post_id: str) -> None:
        self._inner = inner
        self._fail_for = fail_for_buffer_post_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def update_post(self, post_id: object, **fields: object) -> None:
        if fields.get("buffer_post_id") == self._fail_for:
            raise RuntimeError("db down")
        self._inner.update_post(post_id, **fields)  # type: ignore[arg-type]


class _RepoWithUpdateFailureForPostId:
    """Wraps a Repo and fails every update_post call for one specific
    (already-existing) db post id — used by edit()/cancel() db_write_failed
    tests to simulate Buffer succeeding but our own DB write then failing."""

    def __init__(self, inner: InMemoryRepo, fail_for_post_id: object) -> None:
        self._inner = inner
        self._fail_for = fail_for_post_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def update_post(self, post_id: object, **fields: object) -> None:
        if post_id == self._fail_for:
            raise RuntimeError("db down")
        self._inner.update_post(post_id, **fields)  # type: ignore[arg-type]


class TestPublishDbWriteFailure:
    def test_db_failure_after_buffer_success_reports_db_write_failed_with_post_id(
        self, buffer: FakeBuffer, storage: FakeStorage, repo: InMemoryRepo
    ) -> None:
        # Arrange: Buffer already created the post; our own DB write then fails.
        # We must not lose the buffer_post_id, and other targets must proceed.
        wrapped_repo = _RepoWithUpdateFailureAfterSuccess(repo, fail_for_buffer_post_id="post-x")
        publisher = Publisher(
            buffer=buffer,
            storage=storage,
            repo=wrapped_repo,
            probe=lambda path: VALID_VIDEO,  # type: ignore[arg-type]
        )
        request = _request(_targets("tiktok", "x"))

        # Act
        report = publisher.publish(request, approved=True)

        # Assert: buffer_post_id must survive even though our own DB write
        # failed, and post_id (our DB row's id) is still available since the
        # row was already created before the Buffer call.
        results = {r.service: r for r in report.results}
        assert results["x"].status == "db_write_failed"
        assert results["x"].buffer_post_id == "post-x"
        assert results["x"].post_id is not None
        assert results["tiktok"].status == "scheduled"


class TestPlan:
    def test_missing_channel_for_service_is_error(self, buffer: FakeBuffer, storage: FakeStorage) -> None:
        # Arrange: repo has no channels at all
        repo = InMemoryRepo(channels=[])
        publisher = Publisher(buffer=buffer, storage=storage, repo=repo, probe=lambda path: VALID_VIDEO)
        request = _request(_targets("x"))

        # Act
        plan = publisher.plan(request)

        # Assert
        assert plan.targets[0].channel is None
        assert any(i.level == "error" for i in plan.targets[0].issues)

    def test_disconnected_channel_is_error(self, buffer: FakeBuffer, storage: FakeStorage) -> None:
        # Arrange
        repo = InMemoryRepo(channels=[_channel("x", is_disconnected=True)])
        publisher = Publisher(buffer=buffer, storage=storage, repo=repo, probe=lambda path: VALID_VIDEO)
        request = _request(_targets("x"))

        # Act
        plan = publisher.plan(request)

        # Assert
        assert plan.targets[0].channel is None
        assert any("連携解除" in i.message for i in plan.targets[0].issues)

    def test_queue_paused_channel_is_error(self, buffer: FakeBuffer, storage: FakeStorage) -> None:
        # Arrange
        repo = InMemoryRepo(channels=[_channel("x", is_queue_paused=True)])
        publisher = Publisher(buffer=buffer, storage=storage, repo=repo, probe=lambda path: VALID_VIDEO)
        request = _request(_targets("x"))

        # Act
        plan = publisher.plan(request)

        # Assert
        assert plan.targets[0].channel is None
        assert any("一時停止" in i.message for i in plan.targets[0].issues)

    def test_multiple_channels_for_service_without_channel_id_is_error(
        self, buffer: FakeBuffer, storage: FakeStorage
    ) -> None:
        # Arrange: two channels for the same service, target doesn't specify channel_id
        repo = InMemoryRepo(channels=[_channel("x"), _channel("x")])
        publisher = Publisher(buffer=buffer, storage=storage, repo=repo, probe=lambda path: VALID_VIDEO)
        request = _request(_targets("x"))

        # Act
        plan = publisher.plan(request)

        # Assert
        assert plan.targets[0].channel is None
        assert any("複数" in i.message for i in plan.targets[0].issues)

    def test_explicit_channel_id_for_a_different_service_is_error(
        self, buffer: FakeBuffer, storage: FakeStorage
    ) -> None:
        # Arrange: channel_id belongs to a tiktok channel, but the target says "x"
        tiktok_channel = _channel("tiktok")
        repo = InMemoryRepo(channels=[tiktok_channel, _channel("x")])
        publisher = Publisher(buffer=buffer, storage=storage, repo=repo, probe=lambda path: VALID_VIDEO)
        target = Target(service="x", caption="hi", channel_id=tiktok_channel.buffer_channel_id)
        request = _request([target])

        # Act
        plan = publisher.plan(request)

        # Assert
        assert plan.targets[0].channel is None
        assert any(i.level == "error" for i in plan.targets[0].issues)


class TestSync:
    def test_sync_skips_buffer_call_for_future_due_at(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange: a customScheduled post whose due_at is well in the future
        # relative to the injected clock — Buffer shouldn't be queried at all.
        future_due_at = FIXED_NOW + timedelta(hours=1)
        target = Target(
            service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=future_due_at)
        )
        request = _request([target])
        report = publisher.publish(request, approved=True)
        buffer.calls.clear()

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["unchanged"] == 1
        assert buffer.get_post_calls == []
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.status == PostStatus.SCHEDULED  # untouched

    def test_sync_queries_buffer_for_past_due_at(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange: due_at is now-or-past relative to the injected clock
        past_due_at = FIXED_NOW - timedelta(minutes=1)
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=past_due_at))
        request = _request([target])
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "sent"}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["sent"] == 1

    def test_sync_queries_buffer_for_null_due_at(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange: addToQueue posts have no due_at until Buffer assigns one,
        # so they must always be queried (can't tell if they're "future").
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "sent"}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["sent"] == 1

    @pytest.mark.parametrize("remote_status", ["scheduled", "sending", "needs_approval", "draft"])
    def test_sync_leaves_in_flight_statuses_unchanged(
        self, publisher: Publisher, buffer: FakeBuffer, remote_status: str
    ) -> None:
        # Arrange: none of these are terminal from Buffer's perspective yet
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": remote_status}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts == {"sent": 0, "failed": 0, "canceled": 0, "unchanged": 1, "stuck_pending": 0}
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.status == PostStatus.SCHEDULED

    def test_sync_maps_sent_and_failed_and_leaves_others_unchanged(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        request = _request(_targets("tiktok", "instagram"))
        report = publisher.publish(request, approved=True)
        tiktok_post_id = next(r.buffer_post_id for r in report.results if r.service == "tiktok")
        instagram_post_id = next(r.buffer_post_id for r in report.results if r.service == "instagram")
        assert tiktok_post_id is not None
        assert instagram_post_id is not None

        buffer.posts[tiktok_post_id] = {"status": "sent", "externalLink": "https://tiktok.com/1"}
        buffer.posts[instagram_post_id] = {"status": "error", "error": {"message": "policy violation"}}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts == {"sent": 1, "failed": 1, "canceled": 0, "unchanged": 0, "stuck_pending": 0}
        submission_id = report.submission_id
        assert submission_id is not None
        rows = {p.buffer_post_id: p for p in publisher._repo.list_posts_by_submission(submission_id)}
        assert rows[tiktok_post_id].status == PostStatus.SENT
        assert rows[tiktok_post_id].external_link == "https://tiktok.com/1"

    def test_sync_maps_missing_buffer_post_to_canceled(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: Buffer post was deleted -> get_post returns {} (post: null)
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts.pop(post_id, None)  # ensure get_post returns {}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["canceled"] == 1
        submission_id = report.submission_id
        assert submission_id is not None
        row = publisher._repo.list_posts_by_submission(submission_id)[0]
        assert row.status == PostStatus.CANCELED

    def test_sync_writes_back_buffer_assigned_due_at_for_queued_post(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: addToQueue post has no due_at until Buffer decides one
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "needs_approval", "dueAt": "2030-08-01T09:00:00+00:00"}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["unchanged"] == 1
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.due_at is not None

    def test_sync_overwrites_due_at_when_buffer_reports_a_different_value(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: a customScheduled post already has a due_at set locally, but
        # Buffer is authoritative — if it reports a different due date (e.g. a
        # human rescheduled it directly in Buffer's UI), we must reflect that.
        due_at = _future()
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=due_at))
        request = _request([target])
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "scheduled", "dueAt": "2099-01-01T00:00:00+00:00"}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["unchanged"] == 1
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.due_at is not None
        assert row.due_at.year == 2099

    def test_sync_leaves_due_at_untouched_when_buffer_reports_same_value(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        due_at = _future()
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=due_at))
        request = _request([target])
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "scheduled", "dueAt": due_at.isoformat()}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["unchanged"] == 1
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.due_at == due_at

    def test_sync_failed_without_message_persists_non_null_error_message(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: DB CHECK posts_failed_has_error requires error_message NOT NULL,
        # including when Buffer reports a failure with no error message.
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "error", "error": {}}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["failed"] == 1
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.error_message is not None

    def test_sync_reports_stuck_pending_without_calling_buffer(
        self, publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
    ) -> None:
        # Arrange: simulate a crash that left a post in `pending`
        channel = repo.list_channels()[0]
        submission_id = repo.create_submission(
            title=None,
            s3_key="k",
            video_url="https://cdn/x.mp4",
            thumbnail_url=None,
            duration_sec=None,
            approved_at=datetime.now(UTC),
        )
        repo.upsert_post_pending(
            submission_id=submission_id,
            channel_id=channel.id,
            caption="hi",
            mode="addToQueue",
            due_at=None,
            is_draft=False,
            platform_options={},
        )

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["stuck_pending"] == 1
        assert buffer.calls == []

    def test_sync_records_per_post_error_and_continues(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: one post's Buffer lookup blows up, the other succeeds
        request = _request(_targets("tiktok", "x"))
        report = publisher.publish(request, approved=True)
        tiktok_post_id = next(r.buffer_post_id for r in report.results if r.service == "tiktok")
        x_post_id = next(r.buffer_post_id for r in report.results if r.service == "x")
        assert tiktok_post_id is not None
        assert x_post_id is not None
        buffer.get_post_exceptions[tiktok_post_id] = RuntimeError("boom")
        buffer.posts[x_post_id] = {"status": "sent"}

        # Act
        result = publisher.sync()

        # Assert
        assert result["sent"] == 1
        tiktok_row = next(
            p
            for p in publisher._repo.list_posts_by_submission(report.submission_id)  # type: ignore[arg-type]
            if p.buffer_post_id == tiktok_post_id
        )
        assert result["errors"] == [{"post_id": str(tiktok_row.id), "message": "boom"}]
        # the failing post itself is left untouched (still scheduled), not silently canceled/failed
        assert tiktok_row.status == PostStatus.SCHEDULED

    def test_sync_auth_error_aborts_the_whole_sync(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange: an invalid API key should stop immediately, not be swallowed
        # per-post — every other post's sync would fail the same way anyway.
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.get_post_exceptions[post_id] = BufferAuthError("bad key")

        # Act / Assert
        with pytest.raises(BufferAuthError):
            publisher.sync()

    def test_sync_repairs_caption_when_buffer_reports_different_text(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: a human edited the post directly in Buffer's UI — sync()
        # must repair our local caption to match, not just due_at.
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "scheduled", "text": "edited directly in buffer"}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["unchanged"] == 1
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.caption == "edited directly in buffer"

    def test_sync_leaves_caption_untouched_when_buffer_reports_same_text(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "scheduled", "text": "hello"}

        # Act
        publisher.sync()

        # Assert
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.caption == "hello"

    def test_sync_includes_draft_posts_in_candidate_set(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: sync() must repair/reconcile drafts too, not just
        # scheduled posts (see Publisher.sync / Repo.list_syncable_posts).
        request = _request(_targets("x"), draft=True)
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "sent"}

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["sent"] == 1
        assert buffer.get_post_calls == [post_id]

    def test_sync_full_true_queries_buffer_regardless_of_due_at(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: the manual /sync endpoint (dashboard button, CLI) must
        # reconcile every scheduled/draft post even if its due_at hasn't
        # arrived yet — e.g. a human canceled it directly in Buffer.
        future_due_at = FIXED_NOW + timedelta(hours=1)
        target = Target(
            service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=future_due_at)
        )
        request = _request([target])
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "sent"}

        # Act
        counts = publisher.sync(full=True)

        # Assert
        assert counts["sent"] == 1
        assert buffer.get_post_calls == [post_id]

    def test_sync_stops_when_should_stop_signals_and_reports_truncated(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: a Lambda invocation running low on remaining execution
        # time must stop cleanly instead of getting killed mid-post.
        request = _request(_targets("tiktok", "x"))
        report = publisher.publish(request, approved=True)
        for result in report.results:
            assert result.buffer_post_id is not None
            buffer.posts[result.buffer_post_id] = {"status": "sent"}

        # Act: signal "stop" before processing even the first candidate
        counts = publisher.sync(should_stop=lambda: True)

        # Assert
        assert counts["truncated"] is True
        assert buffer.get_post_calls == []
        assert counts["sent"] == 0

    def test_sync_should_stop_trips_after_first_post_leaves_remaining_untouched(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: should_stop only starts returning True once the first
        # candidate has already been processed — the second must be left
        # completely untouched (not queried, status unchanged), not merely
        # counted as "unchanged".
        request = _request(_targets("tiktok", "x"))
        report = publisher.publish(request, approved=True)
        for result in report.results:
            assert result.buffer_post_id is not None
            buffer.posts[result.buffer_post_id] = {"status": "sent"}

        calls = {"n": 0}

        def _should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # let exactly one post through

        # Act
        counts = publisher.sync(should_stop=_should_stop)

        # Assert
        assert counts["truncated"] is True
        assert counts["sent"] == 1
        assert len(buffer.get_post_calls) == 1
        rows = publisher._repo.list_posts_by_submission(report.submission_id)  # type: ignore[arg-type]
        untouched_rows = [r for r in rows if r.buffer_post_id not in buffer.get_post_calls]
        assert len(untouched_rows) == 1
        assert untouched_rows[0].status == PostStatus.SCHEDULED

    def test_sync_maps_missing_buffer_post_to_canceled_for_draft(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: sync() must detect a draft deleted directly in Buffer's
        # UI too, not just scheduled posts.
        request = _request(_targets("x"), draft=True)
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts.pop(post_id, None)

        # Act
        counts = publisher.sync()

        # Assert
        assert counts["canceled"] == 1
        row = publisher._repo.list_posts_by_submission(report.submission_id)[0]  # type: ignore[arg-type]
        assert row.status == PostStatus.CANCELED

    def test_sync_without_should_stop_never_truncates(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = report.results[0].buffer_post_id
        assert post_id is not None
        buffer.posts[post_id] = {"status": "sent"}

        # Act
        counts = publisher.sync()

        # Assert
        assert "truncated" not in counts


class TestRetry:
    def test_retry_only_resends_failed_posts_with_identical_platform_options(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        buffer.results["tiktok"] = CreatePostResult(ok=False, error="temporary error")
        target = Target(service="tiktok", caption="hi", tiktok_title="My Title")
        request = _request([target, Target(service="x", caption="hi")])
        first_report = publisher.publish(request, approved=True)
        submission_id = first_report.submission_id
        assert submission_id is not None
        assert len(buffer.calls) == 2

        # Fix the failure for the retry
        buffer.results["tiktok"] = CreatePostResult(ok=True, post_id="post-tiktok-2", status="scheduled")
        buffer.calls.clear()

        # Act
        retry_report = publisher.retry(submission_id, approved=True)

        # Assert: only "tiktok" was retried, x untouched, platform_options resent identically
        assert len(retry_report.results) == 1
        assert retry_report.results[0].service == "tiktok"
        assert retry_report.results[0].status == "scheduled"
        assert retry_report.results[0].buffer_post_id == "post-tiktok-2"
        assert retry_report.results[0].post_id is not None
        assert len(buffer.calls) == 1
        row = next(
            p
            for p in publisher._repo.list_posts_by_submission(submission_id)
            if p.buffer_post_id == "post-tiktok-2"
        )
        assert row.platform_options == {"tiktok_title": "My Title"}
        assert row.retry_count == 1

    def test_retry_without_approval_raises(self, publisher: Publisher) -> None:
        # Act / Assert
        with pytest.raises(ApprovalRequiredError):
            publisher.retry(uuid4(), approved=False)

    def test_retry_customscheduled_with_past_due_at_fails_fast_without_calling_buffer(
        self, publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
    ) -> None:
        # Arrange: a customScheduled post that previously failed, whose due_at has
        # since elapsed -- retrying it must not resend a now-invalid schedule.
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=_future()))
        request = _request([target])
        report = publisher.publish(request, approved=True)
        submission_id = report.submission_id
        assert submission_id is not None
        post = next(p for p in repo.list_posts_by_submission(submission_id))
        past = datetime.now(UTC) - timedelta(hours=1)
        repo.update_post(post.id, status=PostStatus.FAILED, due_at=past, error_message="prior failure")
        buffer.calls.clear()

        # Act
        retry_report = publisher.retry(submission_id, approved=True)

        # Assert
        assert buffer.calls == []
        assert retry_report.results[0].status == "failed"
        assert "予定日時" in (retry_report.results[0].error or "")
        updated = repo.get_post(post.id)
        assert updated is not None
        assert updated.status == PostStatus.FAILED
        assert updated.retry_count == 1

    def test_retry_mutation_error_without_message_still_persists_non_null_error_message(
        self, publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
    ) -> None:
        # Arrange: DB CHECK posts_failed_has_error requires error_message NOT NULL
        # whenever status='failed', including on the retry path.
        buffer.results["x"] = CreatePostResult(ok=False, error="first failure")
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        submission_id = report.submission_id
        assert submission_id is not None
        buffer.results["x"] = CreatePostResult(ok=False, error=None)

        # Act
        retry_report = publisher.retry(submission_id, approved=True)

        # Assert
        assert retry_report.results[0].status == "failed"
        post = next(p for p in repo.list_posts_by_submission(submission_id))
        assert post.error_message is not None

    def test_retry_reconstructs_timing_without_due_at_for_non_custom_scheduled(
        self, publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
    ) -> None:
        # Arrange: an addToQueue post whose due_at was later backfilled by sync
        # (see Publisher.sync), then failed again for an unrelated reason.
        # Rebuilding Timing(mode="addToQueue", due_at=<something>) would raise
        # a ValidationError (due_at is only valid for customScheduled) unless
        # retry omits due_at for non-customScheduled modes.
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        submission_id = report.submission_id
        assert submission_id is not None
        post = repo.list_posts_by_submission(submission_id)[0]
        repo.update_post(
            post.id, status=PostStatus.FAILED, due_at=_future(), error_message="unrelated failure"
        )
        buffer.results["x"] = CreatePostResult(ok=True, post_id="post-x-2", status="scheduled")

        # Act
        retry_report = publisher.retry(submission_id, approved=True)

        # Assert: no exception, retry proceeds normally
        assert retry_report.results[0].status == "scheduled"

    def test_retry_skips_posts_not_in_failed_status(
        self, publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
    ) -> None:
        # Arrange: a post left `pending` (e.g. from an ambiguous createPost) must
        # never be auto-resent by retry() — only an explicit failed post is a
        # retry candidate.
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        submission_id = report.submission_id
        assert submission_id is not None
        post = repo.list_posts_by_submission(submission_id)[0]
        repo.update_post(post.id, status=PostStatus.PENDING, error_message="ambiguous outcome")
        buffer.calls.clear()

        # Act
        retry_report = publisher.retry(submission_id, approved=True)

        # Assert
        assert retry_report.results == []
        assert buffer.calls == []
        assert repo.get_post(post.id).status == PostStatus.PENDING  # type: ignore[union-attr]

    def test_retry_ambiguous_outcome_leaves_pending_and_reports_unknown(
        self, publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
    ) -> None:
        # Arrange
        buffer.results["x"] = CreatePostResult(ok=False, error="first failure")
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        submission_id = report.submission_id
        assert submission_id is not None
        buffer.exceptions["x"] = BufferAmbiguousError("boom")

        # Act
        retry_report = publisher.retry(submission_id, approved=True)

        # Assert
        assert retry_report.results[0].status == "unknown"
        post = repo.list_posts_by_submission(submission_id)[0]
        assert post.status == PostStatus.PENDING
        assert post.retry_count == 1

    def test_retry_protocol_error_leaves_pending_and_reports_unknown(
        self, publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
    ) -> None:
        # Arrange: same ambiguity as BufferAmbiguousError — see
        # test_retry_ambiguous_outcome_leaves_pending_and_reports_unknown.
        buffer.results["x"] = CreatePostResult(ok=False, error="first failure")
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        submission_id = report.submission_id
        assert submission_id is not None
        buffer.exceptions["x"] = BufferProtocolError("boom")

        # Act
        retry_report = publisher.retry(submission_id, approved=True)

        # Assert
        assert retry_report.results[0].status == "unknown"
        post = repo.list_posts_by_submission(submission_id)[0]
        assert post.status == PostStatus.PENDING
        assert post.retry_count == 1

    def test_retry_restores_thumbnail_offset_ms_from_platform_options(
        self, publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
    ) -> None:
        # Arrange
        buffer.results["x"] = CreatePostResult(ok=False, error="first failure")
        request = _request(_targets("x"), thumbnail_offset_ms=3000)
        report = publisher.publish(request, approved=True)
        submission_id = report.submission_id
        assert submission_id is not None
        buffer.results["x"] = CreatePostResult(ok=True, post_id="post-x-2", status="scheduled")
        buffer.requests.clear()

        # Act
        publisher.retry(submission_id, approved=True)

        # Assert
        assert buffer.requests[-1].thumbnail_offset_ms == 3000


class TestEdit:
    def _scheduled_post_id(self, publisher: Publisher, request: PublishRequest) -> object:
        report = publisher.publish(request, approved=True)
        return report.results[0].post_id

    def test_edit_caption_calls_buffer_and_updates_row(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)

        # Act
        result = publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]

        # Assert
        assert result.status == "scheduled"
        assert buffer.edit_calls[0][1] == "new caption"
        row = publisher._repo.get_post(post_id)  # type: ignore[arg-type]
        assert row is not None
        assert row.caption == "new caption"
        assert result.post_id == post_id
        assert result.buffer_post_id == row.buffer_post_id == "post-x"

    def test_edit_uses_buffer_response_status_and_message_on_error(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: Buffer accepted the edit but the post itself immediately
        # entered an error state (e.g. media rejected) — edit() must surface
        # that, not silently report the pre-edit status.
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)
        buffer.edit_results["post-x"] = CreatePostResult(
            ok=True, post_id="post-x", status="error", error="media rejected"
        )

        # Act
        result = publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]

        # Assert
        assert result.status == "failed"
        assert result.error == "media rejected"
        row = publisher._repo.get_post(post_id)  # type: ignore[arg-type]
        assert row is not None
        assert row.status == PostStatus.FAILED
        assert row.error_message == "media rejected"

    def test_edit_uses_buffer_response_sent_status_and_sets_sent_at(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: Buffer sends the post almost immediately after accepting
        # the edit (e.g. a near-due post whose caption was just tweaked).
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)
        buffer.edit_results["post-x"] = CreatePostResult(ok=True, post_id="post-x", status="sent")

        # Act
        result = publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]

        # Assert
        assert result.status == "sent"
        row = publisher._repo.get_post(post_id)  # type: ignore[arg-type]
        assert row is not None
        assert row.status == PostStatus.SENT
        assert row.sent_at is not None

    def test_edit_uses_buffer_response_caption_over_requested_caption(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange: Buffer's own response is authoritative, not merely an
        # echo of what we asked for.
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)
        buffer.edit_results["post-x"] = CreatePostResult(
            ok=True, post_id="post-x", status="scheduled", caption="buffer's own caption"
        )

        # Act
        publisher.edit(post_id, caption="requested caption")  # type: ignore[arg-type]

        # Assert
        row = publisher._repo.get_post(post_id)  # type: ignore[arg-type]
        assert row is not None
        assert row.caption == "buffer's own caption"

    def test_edit_uses_buffer_response_due_at_over_requested_due_at(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)
        requested_due_at = FIXED_NOW + timedelta(hours=2)
        buffer_due_at = FIXED_NOW + timedelta(hours=5)
        buffer.edit_results["post-x"] = CreatePostResult(
            ok=True, post_id="post-x", status="scheduled", due_at=buffer_due_at.isoformat()
        )

        # Act
        result = publisher.edit(post_id, due_at=requested_due_at)  # type: ignore[arg-type]

        # Assert
        assert result.due_at == buffer_due_at.isoformat()
        row = publisher._repo.get_post(post_id)  # type: ignore[arg-type]
        assert row is not None
        assert row.due_at == buffer_due_at

    def test_edit_db_write_failure_after_buffer_success_reports_db_write_failed(
        self, buffer: FakeBuffer, storage: FakeStorage, repo: InMemoryRepo
    ) -> None:
        # Arrange: Buffer already applied the edit; our own DB write then
        # fails. The caller must be told the DB is now out of sync, not that
        # the edit itself failed.
        setup_publisher = Publisher(
            buffer=buffer, storage=storage, repo=repo, probe=lambda path: VALID_VIDEO, clock=lambda: FIXED_NOW
        )
        request = _request(_targets("x"))
        post_id = setup_publisher.publish(request, approved=True).results[0].post_id
        wrapped_repo = _RepoWithUpdateFailureForPostId(repo, fail_for_post_id=post_id)
        publisher = Publisher(
            buffer=buffer,
            storage=storage,
            repo=wrapped_repo,
            probe=lambda path: VALID_VIDEO,
            clock=lambda: FIXED_NOW,
        )

        # Act
        result = publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]

        # Assert
        assert result.status == "db_write_failed"
        assert result.post_id == post_id
        assert result.buffer_post_id == "post-x"

    def test_edit_refused_within_5_minutes_of_due_at(
        self, publisher: Publisher, buffer: FakeBuffer, repo: InMemoryRepo
    ) -> None:
        # Arrange: Buffer may already be sending — editing now risks a race.
        near_due_at = FIXED_NOW + timedelta(minutes=4)
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=near_due_at))
        request = _request([target])
        post_id = self._scheduled_post_id(publisher, request)

        # Act / Assert
        with pytest.raises(ValueError, match="5分前"):
            publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]
        assert buffer.edit_calls == []

    def test_edit_allowed_just_outside_5_minute_guard(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange
        due_at = FIXED_NOW + timedelta(minutes=6)
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=due_at))
        request = _request([target])
        post_id = self._scheduled_post_id(publisher, request)

        # Act
        result = publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]

        # Assert
        assert result.status == "scheduled"

    @pytest.mark.parametrize(
        ("delta", "should_raise"),
        [
            (timedelta(minutes=5), True),  # exactly the boundary: due_at <= now + 5min -> refused
            (timedelta(minutes=4, seconds=59), True),  # 1 second inside the guard
            (timedelta(minutes=5, seconds=1), False),  # 1 second outside the guard
        ],
    )
    def test_edit_near_due_guard_boundary(
        self, publisher: Publisher, buffer: FakeBuffer, delta: timedelta, should_raise: bool
    ) -> None:
        # Arrange
        due_at = FIXED_NOW + delta
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=due_at))
        request = _request([target])
        post_id = self._scheduled_post_id(publisher, request)

        # Act / Assert
        if should_raise:
            with pytest.raises(ValueError, match="5分前"):
                publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]
            assert buffer.edit_calls == []
        else:
            result = publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]
            assert result.status == "scheduled"

    def test_edit_allowed_when_due_at_is_null(self, publisher: Publisher) -> None:
        # Arrange: addToQueue posts have no due_at until Buffer assigns
        # one — the near-due guard only applies when due_at is known, so it
        # must never block a null-due_at post regardless of how close "now" is.
        request = _request(_targets("x"))  # addToQueue mode -> due_at=None
        post_id = self._scheduled_post_id(publisher, request)

        # Act
        result = publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]

        # Assert
        assert result.status == "scheduled"

    def test_edit_invalid_caption_raises_and_does_not_call_buffer(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)

        # Act / Assert: an expected client-input problem -> RequestValidationError
        # (a ValueError subclass, so `except ValueError` call sites are unaffected).
        with pytest.raises(RequestValidationError, match="X"):
            publisher.edit(post_id, caption="あ" * 141)  # type: ignore[arg-type]
        assert buffer.edit_calls == []

    def test_edit_past_due_at_raises(self, publisher: Publisher) -> None:
        # Arrange
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)
        past = datetime.now(UTC) - timedelta(hours=1)

        # Act / Assert
        with pytest.raises(RequestValidationError, match="未来"):
            publisher.edit(post_id, due_at=past)  # type: ignore[arg-type]

    def test_edit_naive_due_at_raises(self, publisher: Publisher) -> None:
        # Arrange
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)
        naive = datetime.now() + timedelta(hours=1)  # noqa: DTZ005

        # Act / Assert
        with pytest.raises(RequestValidationError, match="タイムゾーン"):
            publisher.edit(post_id, due_at=naive)  # type: ignore[arg-type]

    def test_edit_non_editable_status_raises(self, publisher: Publisher) -> None:
        # Arrange: a failed post cannot be edited
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]
        publisher._repo.update_post(post_id, status=PostStatus.FAILED, error_message="x")

        # Act / Assert
        with pytest.raises(ValueError, match="failed"):
            publisher.edit(post_id, caption="ok")

    @pytest.mark.parametrize(
        "status", [PostStatus.SENT, PostStatus.FAILED, PostStatus.CANCELED, PostStatus.PENDING]
    )
    def test_edit_refused_for_non_editable_status_has_no_side_effects(
        self, publisher: Publisher, buffer: FakeBuffer, status: PostStatus
    ) -> None:
        # Arrange
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]
        error_message = "prior failure" if status == PostStatus.FAILED else None
        publisher._repo.update_post(post_id, status=status, error_message=error_message)
        before = publisher._repo.get_post(post_id)

        # Act / Assert
        with pytest.raises(ValueError):
            publisher.edit(post_id, caption="new caption")
        assert buffer.edit_calls == []
        assert publisher._repo.get_post(post_id) == before

    def test_edit_past_due_at_does_not_call_buffer(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)
        past = datetime.now(UTC) - timedelta(hours=1)

        # Act / Assert
        with pytest.raises(ValueError, match="未来"):
            publisher.edit(post_id, due_at=past)  # type: ignore[arg-type]
        assert buffer.edit_calls == []

    def test_edit_buffer_failure_leaves_db_unchanged(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)
        buffer.edit_results["post-x"] = CreatePostResult(ok=False, error="buffer rejected edit")
        before = publisher._repo.get_post(post_id)  # type: ignore[arg-type]

        # Act / Assert
        with pytest.raises(ValueError, match="buffer rejected edit"):
            publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]
        assert publisher._repo.get_post(post_id) == before  # type: ignore[arg-type]

    def test_edit_ambiguous_buffer_outcome_raises_and_leaves_db_unchanged(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        request = _request(_targets("x"))
        post_id = self._scheduled_post_id(publisher, request)
        buffer.edit_exceptions["post-x"] = BufferAmbiguousError("boom")
        before = publisher._repo.get_post(post_id)  # type: ignore[arg-type]

        # Act / Assert: propagates as-is (not wrapped) so the API layer can
        # distinguish "ambiguous" (409) from other 400-level errors.
        with pytest.raises(BufferAmbiguousError):
            publisher.edit(post_id, caption="new caption")  # type: ignore[arg-type]
        assert publisher._repo.get_post(post_id) == before  # type: ignore[arg-type]


class TestCancel:
    def test_cancel_calls_buffer_and_marks_canceled(self, publisher: Publisher, buffer: FakeBuffer) -> None:
        # Arrange
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]

        # Act
        result = publisher.cancel(post_id)

        # Assert
        assert result.status == "canceled"
        assert buffer.delete_calls == ["post-x"]
        row = publisher._repo.get_post(post_id)
        assert row is not None
        assert row.status == PostStatus.CANCELED
        assert result.post_id == post_id
        assert result.buffer_post_id == "post-x"

    def test_cancel_db_write_failure_after_buffer_success_reports_db_write_failed(
        self, buffer: FakeBuffer, storage: FakeStorage, repo: InMemoryRepo
    ) -> None:
        # Arrange: Buffer already deleted the post; our own DB write then
        # fails. The caller must be told the DB is now out of sync, not that
        # the cancellation itself failed.
        setup_publisher = Publisher(
            buffer=buffer, storage=storage, repo=repo, probe=lambda path: VALID_VIDEO, clock=lambda: FIXED_NOW
        )
        request = _request(_targets("x"))
        post_id = setup_publisher.publish(request, approved=True).results[0].post_id
        wrapped_repo = _RepoWithUpdateFailureForPostId(repo, fail_for_post_id=post_id)
        publisher = Publisher(
            buffer=buffer,
            storage=storage,
            repo=wrapped_repo,
            probe=lambda path: VALID_VIDEO,
            clock=lambda: FIXED_NOW,
        )

        # Act
        result = publisher.cancel(post_id)  # type: ignore[arg-type]

        # Assert
        assert result.status == "db_write_failed"
        assert result.post_id == post_id
        assert result.buffer_post_id == "post-x"

    def test_cancel_refused_within_5_minutes_of_due_at(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        near_due_at = FIXED_NOW + timedelta(minutes=1)
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=near_due_at))
        request = _request([target])
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]

        # Act / Assert
        with pytest.raises(ValueError, match="5分前"):
            publisher.cancel(post_id)
        assert buffer.delete_calls == []

    @pytest.mark.parametrize(
        ("delta", "should_raise"),
        [
            (timedelta(minutes=5), True),  # exactly the boundary: due_at <= now + 5min -> refused
            (timedelta(minutes=4, seconds=59), True),  # 1 second inside the guard
            (timedelta(minutes=5, seconds=1), False),  # 1 second outside the guard
        ],
    )
    def test_cancel_near_due_guard_boundary(
        self, publisher: Publisher, buffer: FakeBuffer, delta: timedelta, should_raise: bool
    ) -> None:
        # Arrange
        due_at = FIXED_NOW + delta
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=due_at))
        request = _request([target])
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]

        # Act / Assert
        if should_raise:
            with pytest.raises(ValueError, match="5分前"):
                publisher.cancel(post_id)
            assert buffer.delete_calls == []
        else:
            result = publisher.cancel(post_id)
            assert result.status == "canceled"

    def test_cancel_allowed_when_due_at_is_null(self, publisher: Publisher) -> None:
        # Arrange: addToQueue posts have no due_at — the near-due guard must
        # never block them regardless of how close "now" is.
        request = _request(_targets("x"))  # addToQueue mode -> due_at=None
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]

        # Act
        result = publisher.cancel(post_id)

        # Assert
        assert result.status == "canceled"

    def test_cancel_buffer_failure_raises_and_does_not_change_status(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]
        buffer.delete_results["post-x"] = CreatePostResult(ok=False, error="already sent")

        # Act / Assert
        with pytest.raises(ValueError, match="already sent"):
            publisher.cancel(post_id)
        row = publisher._repo.get_post(post_id)
        assert row is not None
        assert row.status == PostStatus.SCHEDULED

    def test_cancel_non_editable_status_raises(self, publisher: Publisher) -> None:
        # Arrange
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]
        publisher._repo.update_post(post_id, status=PostStatus.CANCELED)

        # Act / Assert
        with pytest.raises(ValueError, match="canceled"):
            publisher.cancel(post_id)

    @pytest.mark.parametrize(
        "status", [PostStatus.SENT, PostStatus.FAILED, PostStatus.CANCELED, PostStatus.PENDING]
    )
    def test_cancel_refused_for_non_editable_status_has_no_side_effects(
        self, publisher: Publisher, buffer: FakeBuffer, status: PostStatus
    ) -> None:
        # Arrange
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]
        error_message = "prior failure" if status == PostStatus.FAILED else None
        publisher._repo.update_post(post_id, status=status, error_message=error_message)
        before = publisher._repo.get_post(post_id)

        # Act / Assert
        with pytest.raises(ValueError):
            publisher.cancel(post_id)
        assert buffer.delete_calls == []
        assert publisher._repo.get_post(post_id) == before

    def test_cancel_ambiguous_buffer_outcome_raises_and_leaves_db_unchanged(
        self, publisher: Publisher, buffer: FakeBuffer
    ) -> None:
        # Arrange
        request = _request(_targets("x"))
        report = publisher.publish(request, approved=True)
        post_id = next(p.id for p in publisher._repo.list_posts_by_submission(report.submission_id))  # type: ignore[arg-type]
        buffer.delete_exceptions["post-x"] = BufferAmbiguousError("boom")
        before = publisher._repo.get_post(post_id)

        # Act / Assert
        with pytest.raises(BufferAmbiguousError):
            publisher.cancel(post_id)
        assert publisher._repo.get_post(post_id) == before
