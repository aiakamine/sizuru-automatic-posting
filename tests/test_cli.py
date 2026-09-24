"""Tests for sizuru_post.cli: exit codes and JSON output, adapters replaced with fakes."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

import sizuru_post.cli as cli_module
from sizuru_post.adapters.buffer import BufferAmbiguousError, BufferProtocolError, CreatePostResult
from sizuru_post.adapters.repo import ChannelRow
from sizuru_post.domain.models import VideoInfo
from tests.fakes import FakeBuffer, FakeStorage, InMemoryRepo

runner = CliRunner()

VALID_VIDEO = VideoInfo(duration_sec=10.0, width=1080, height=1920, video_codec="h264")


def _channel(service: str) -> ChannelRow:
    return ChannelRow(
        id=uuid4(),
        buffer_channel_id=f"buf-{service}",
        service=service,
        name=service,
        display_name=service,
        is_disconnected=False,
        is_queue_paused=False,
    )


@pytest.fixture
def repo() -> InMemoryRepo:
    return InMemoryRepo(channels=[_channel("x"), _channel("tiktok")])


@pytest.fixture
def buffer() -> FakeBuffer:
    return FakeBuffer()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture(autouse=True)
def fake_publisher(
    monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo, buffer: FakeBuffer, storage: FakeStorage
) -> None:
    def _build_publisher(settings: object) -> object:
        from sizuru_post.publisher import Publisher

        return Publisher(buffer=buffer, storage=storage, repo=repo, probe=lambda path: VALID_VIDEO)

    monkeypatch.setattr(cli_module, "build_publisher", _build_publisher)
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")


def _write_request(tmp_path: Path, **overrides: object) -> Path:
    body: dict[str, object] = {
        "video_path": "clip.mp4",
        "timing": {"mode": "addToQueue"},
        "targets": [{"service": "x", "caption": "hello"}],
    }
    body.update(overrides)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(body))
    return path


class TestValidateCommand:
    def test_valid_request_exits_zero(self, tmp_path: Path) -> None:
        # Arrange
        request_path = _write_request(tmp_path)

        # Act
        result = runner.invoke(cli_module.app, ["validate", str(request_path)])

        # Assert
        assert result.exit_code == 0

    def test_invalid_caption_exits_one(self, tmp_path: Path) -> None:
        # Arrange
        request_path = _write_request(tmp_path, targets=[{"service": "x", "caption": "あ" * 141}])

        # Act
        result = runner.invoke(cli_module.app, ["validate", str(request_path)])

        # Assert
        assert result.exit_code == 1

    def test_invalid_json_request_file_exits_nonzero_with_clear_error(self, tmp_path: Path) -> None:
        # Arrange: malformed JSON, not a schema violation
        request_path = tmp_path / "request.json"
        request_path.write_text("{not valid json")

        # Act
        result = runner.invoke(cli_module.app, ["validate", str(request_path)])

        # Assert
        assert result.exit_code != 0
        assert result.exception is not None

    def test_nonexistent_video_path_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        repo: InMemoryRepo,
        buffer: FakeBuffer,
        storage: FakeStorage,
    ) -> None:
        # Arrange: use the real probe_video (no fake) against a path that doesn't exist
        from sizuru_post.publisher import Publisher

        def _build_publisher_with_real_probe(settings: object) -> object:
            return Publisher(buffer=buffer, storage=storage, repo=repo)

        monkeypatch.setattr(cli_module, "build_publisher", _build_publisher_with_real_probe)
        request_path = _write_request(tmp_path, video_path=str(tmp_path / "missing.mp4"))

        # Act
        result = runner.invoke(cli_module.app, ["validate", str(request_path)])

        # Assert
        assert result.exit_code != 0
        assert result.exception is not None


class TestSettingsErrorHandling:
    def test_missing_env_var_reports_name_only_and_never_leaks_present_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: BUFFER_API_KEY/SUPABASE_SERVICE_ROLE_KEY are set (secrets present),
        # but SUPABASE_URL is missing. chdir so Settings' env_file=".env" can't
        # fall back to this repo's real .env (which has a real SUPABASE_URL).
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BUFFER_API_KEY", "supersecret123")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "role-secret-xyz")
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        request_path = _write_request(tmp_path)

        # Act
        result = runner.invoke(cli_module.app, ["validate", str(request_path)])

        # Assert
        assert result.exit_code == 1
        assert "supersecret123" not in result.output
        assert "role-secret-xyz" not in result.output
        assert "SUPABASE_URL" in result.output


class TestPublishCommand:
    def test_without_yes_requires_approval_and_exits_two(self, tmp_path: Path) -> None:
        # Arrange
        request_path = _write_request(tmp_path)

        # Act
        result = runner.invoke(cli_module.app, ["publish", str(request_path)])

        # Assert
        assert result.exit_code == 2

    def test_with_yes_publishes_and_prints_report(self, tmp_path: Path) -> None:
        # Arrange
        request_path = _write_request(tmp_path)

        # Act
        result = runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])

        # Assert
        assert result.exit_code == 0
        report = json.loads(result.stdout)
        assert report["submission_id"] is not None
        assert report["results"][0]["service"] == "x"
        assert report["results"][0]["status"] == "scheduled"

    def test_report_exposes_both_db_and_buffer_post_ids(self, tmp_path: Path, repo: InMemoryRepo) -> None:
        # Arrange: edit/cancel take the DB post id, not Buffer's, so a skill
        # consuming this JSON needs both to be present and distinguishable.
        request_path = _write_request(tmp_path)

        # Act
        result = runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])

        # Assert
        report = json.loads(result.stdout)
        target_result = report["results"][0]
        assert target_result["buffer_post_id"] == "post-x"
        db_post_id = target_result["post_id"]
        assert repo.get_post(UUID(db_post_id)) is not None

    def test_target_failure_exits_three_but_still_prints_report(
        self, tmp_path: Path, buffer: FakeBuffer
    ) -> None:
        # Arrange
        buffer.results["x"] = CreatePostResult(ok=False, error="rate limited")
        request_path = _write_request(tmp_path)

        # Act
        result = runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])

        # Assert: JSON is still printed even though the exit code signals failure
        assert result.exit_code == 3
        report = json.loads(result.stdout)
        assert report["results"][0]["status"] == "failed"


class TestRetryCommand:
    def test_retry_uses_submission_id_argument(self, tmp_path: Path, buffer: FakeBuffer) -> None:
        # Arrange: publish once with a failure, then retry that submission
        buffer.results["x"] = CreatePostResult(ok=False, error="temporary")
        request_path = _write_request(tmp_path)
        publish_result = runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])
        submission_id = json.loads(publish_result.stdout)["submission_id"]
        buffer.results["x"] = CreatePostResult(ok=True, post_id="post-x-2", status="scheduled")

        # Act
        result = runner.invoke(cli_module.app, ["retry", submission_id, "--yes"])

        # Assert
        assert result.exit_code == 0
        report = json.loads(result.stdout)
        assert report["results"][0]["status"] == "scheduled"

    def test_retry_without_yes_exits_two(self) -> None:
        # Act
        result = runner.invoke(cli_module.app, ["retry", str(uuid4())])

        # Assert
        assert result.exit_code == 2

    def test_retry_still_failing_exits_three(self, tmp_path: Path, buffer: FakeBuffer) -> None:
        # Arrange
        buffer.results["x"] = CreatePostResult(ok=False, error="temporary")
        request_path = _write_request(tmp_path)
        publish_result = runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])
        submission_id = json.loads(publish_result.stdout)["submission_id"]
        buffer.results["x"] = CreatePostResult(ok=False, error="still failing")

        # Act
        result = runner.invoke(cli_module.app, ["retry", submission_id, "--yes"])

        # Assert
        assert result.exit_code == 3
        report = json.loads(result.stdout)
        assert report["results"][0]["status"] == "failed"


class TestEditCommand:
    def test_edit_caption_file_updates_and_prints_result(self, tmp_path: Path, repo: InMemoryRepo) -> None:
        # Arrange
        request_path = _write_request(tmp_path)
        runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])
        post_id = repo.list_posts_by_submission(next(iter(repo._submissions)))[0].id
        caption_file = tmp_path / "caption.txt"
        caption_file.write_text("new caption")

        # Act
        result = runner.invoke(
            cli_module.app, ["edit", str(post_id), "--caption-file", str(caption_file), "--yes"]
        )

        # Assert
        assert result.exit_code == 0
        report = json.loads(result.stdout)
        assert report["service"] == "x"
        assert repo.get_post(post_id).caption == "new caption"  # type: ignore[union-attr]

    def test_edit_without_yes_exits_two(self) -> None:
        # Act
        result = runner.invoke(cli_module.app, ["edit", str(uuid4())])

        # Assert
        assert result.exit_code == 2

    def test_edit_invalid_post_id_exits_one(self) -> None:
        # Act
        result = runner.invoke(cli_module.app, ["edit", str(uuid4()), "--yes"])

        # Assert
        assert result.exit_code == 1

    def test_edit_ambiguous_buffer_outcome_exits_one(
        self, tmp_path: Path, repo: InMemoryRepo, buffer: FakeBuffer
    ) -> None:
        # Arrange: Publisher.edit lets BufferAmbiguousError propagate as-is
        # (not wrapped in ValueError) so the API layer can map it to 409;
        # the CLI must still catch it and exit cleanly like any other error.
        request_path = _write_request(tmp_path)
        runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])
        post_id = repo.list_posts_by_submission(next(iter(repo._submissions)))[0].id
        buffer.edit_exceptions["post-x"] = BufferAmbiguousError("boom")
        caption_file = tmp_path / "c.txt"
        caption_file.write_text("new caption")

        # Act
        result = runner.invoke(
            cli_module.app, ["edit", str(post_id), "--caption-file", str(caption_file), "--yes"]
        )

        # Assert
        assert result.exit_code == 1

    def test_edit_protocol_error_outcome_exits_one(
        self, tmp_path: Path, repo: InMemoryRepo, buffer: FakeBuffer
    ) -> None:
        # Arrange: a malformed/undecodable Buffer "success" response after
        # the mutation was dispatched is just as ambiguous as
        # BufferAmbiguousError — the CLI must catch it too, not crash with
        # an uncaught traceback.
        request_path = _write_request(tmp_path)
        runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])
        post_id = repo.list_posts_by_submission(next(iter(repo._submissions)))[0].id
        buffer.edit_exceptions["post-x"] = BufferProtocolError("boom")
        caption_file = tmp_path / "c.txt"
        caption_file.write_text("new caption")

        # Act
        result = runner.invoke(
            cli_module.app, ["edit", str(post_id), "--caption-file", str(caption_file), "--yes"]
        )

        # Assert: must be caught and reported cleanly (typer.Exit(1) with a
        # message), not an uncaught BufferProtocolError traceback.
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "boom" in result.output

    def test_edit_naive_due_at_exits_one_with_clear_message(self, tmp_path: Path, repo: InMemoryRepo) -> None:
        # Arrange
        request_path = _write_request(tmp_path)
        runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])
        post_id = repo.list_posts_by_submission(next(iter(repo._submissions)))[0].id

        # Act: no timezone offset in the ISO string
        result = runner.invoke(
            cli_module.app, ["edit", str(post_id), "--due-at", "2030-01-01T09:00:00", "--yes"]
        )

        # Assert
        assert result.exit_code == 1
        assert "タイムゾーン" in result.output

    def test_edit_unparsable_due_at_exits_one_with_clear_message(
        self, tmp_path: Path, repo: InMemoryRepo
    ) -> None:
        # Arrange
        request_path = _write_request(tmp_path)
        runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])
        post_id = repo.list_posts_by_submission(next(iter(repo._submissions)))[0].id

        # Act
        result = runner.invoke(cli_module.app, ["edit", str(post_id), "--due-at", "not-a-date", "--yes"])

        # Assert
        assert result.exit_code == 1
        assert "due-at" in result.output or "due_at" in result.output


class TestCancelCommand:
    def test_cancel_marks_canceled_and_prints_result(self, tmp_path: Path, repo: InMemoryRepo) -> None:
        # Arrange
        request_path = _write_request(tmp_path)
        runner.invoke(cli_module.app, ["publish", str(request_path), "--yes"])
        post_id = repo.list_posts_by_submission(next(iter(repo._submissions)))[0].id

        # Act
        result = runner.invoke(cli_module.app, ["cancel", str(post_id), "--yes"])

        # Assert
        assert result.exit_code == 0
        report = json.loads(result.stdout)
        assert report["status"] == "canceled"

    def test_cancel_without_yes_exits_two(self) -> None:
        # Act
        result = runner.invoke(cli_module.app, ["cancel", str(uuid4())])

        # Assert
        assert result.exit_code == 2

    def test_cancel_invalid_post_id_exits_one(self) -> None:
        # Act: post doesn't exist -> Publisher.cancel raises ValueError
        result = runner.invoke(cli_module.app, ["cancel", str(uuid4()), "--yes"])

        # Assert
        assert result.exit_code == 1


class TestSyncCommand:
    def test_sync_prints_counts(self) -> None:
        # Act
        result = runner.invoke(cli_module.app, ["sync"])

        # Assert
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "sent": 0,
            "failed": 0,
            "canceled": 0,
            "unchanged": 0,
            "stuck_pending": 0,
        }

    def test_sync_calls_publisher_with_full_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: the CLI's sync command is a manual/human-triggered action
        # (like the dashboard's "sync now" button), so it must reconcile
        # every scheduled/draft post regardless of local due_at.
        from sizuru_post.publisher import Publisher

        calls: list[dict[str, object]] = []

        def _fake_sync(self: Publisher, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"sent": 0, "failed": 0, "canceled": 0, "unchanged": 0, "stuck_pending": 0}

        monkeypatch.setattr(Publisher, "sync", _fake_sync)

        # Act
        result = runner.invoke(cli_module.app, ["sync"])

        # Assert
        assert result.exit_code == 0
        assert calls == [{"full": True}]


class TestChannelsCommand:
    def test_upserts_channels_and_prints_a_table(
        self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo, buffer: FakeBuffer
    ) -> None:
        # Arrange
        buffer.organizations = [{"id": "org-1"}]
        buffer.channels_by_organization = {
            "org-1": [{"id": "c1", "service": "tiktok", "name": "n1", "displayName": "TikTok Ch"}]
        }
        monkeypatch.setattr(cli_module, "build_buffer_client", lambda settings: buffer)
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)

        # Act
        result = runner.invoke(cli_module.app, ["channels"])

        # Assert
        assert result.exit_code == 0
        assert "TikTok Ch" in result.output
        assert any(c.buffer_channel_id == "c1" for c in repo.list_channels())

    def test_exits_one_with_clear_message_when_no_organization_found(
        self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo, buffer: FakeBuffer
    ) -> None:
        # Arrange
        buffer.organizations = []
        monkeypatch.setattr(cli_module, "build_buffer_client", lambda settings: buffer)
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)

        # Act
        result = runner.invoke(cli_module.app, ["channels"])

        # Assert
        assert result.exit_code == 1
        assert "BUFFER_ORGANIZATION_ID" in result.output

    def test_marks_channels_missing_from_buffer_as_disconnected(
        self, monkeypatch: pytest.MonkeyPatch, buffer: FakeBuffer
    ) -> None:
        # Arrange: repo already has an old channel Buffer no longer reports
        # (root cause of a live incident: `channels` upserted the new
        # channel but never flagged the old one as gone).
        stale = _channel("x")
        repo = InMemoryRepo(channels=[stale])
        buffer.organizations = [{"id": "org-1"}]
        buffer.channels_by_organization = {"org-1": [{"id": "buf-new-x", "service": "x", "name": "new"}]}
        monkeypatch.setattr(cli_module, "build_buffer_client", lambda settings: buffer)
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)

        # Act
        result = runner.invoke(cli_module.app, ["channels"])

        # Assert
        assert result.exit_code == 0
        updated_stale = next(c for c in repo.list_channels() if c.id == stale.id)
        assert updated_stale.is_disconnected is True


class TestAdminAddCommand:
    def test_creates_an_account_and_prints_the_password_once(
        self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo
    ) -> None:
        # Arrange
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)

        # Act
        result = runner.invoke(cli_module.app, ["admin", "add", "sizuru-admin1", "--display-name", "管理者1"])

        # Assert
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["id"] == "sizuru-admin1"
        assert payload["email"] == "sizuru-admin1@sizuru.local"
        assert payload["display_name"] == "管理者1"
        assert len(payload["password"]) == 16
        assert len(repo.list_admin_users()) == 1

    def test_invalid_login_id_exits_one(self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo) -> None:
        # Arrange
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)

        # Act
        result = runner.invoke(cli_module.app, ["admin", "add", "AB"])

        # Assert
        assert result.exit_code == 1
        assert repo.list_admin_users() == []

    def test_duplicate_login_id_exits_one_without_overwriting(
        self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo
    ) -> None:
        # Arrange
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)
        first = runner.invoke(cli_module.app, ["admin", "add", "sizuru-admin1", "--display-name", "管理者1"])
        assert first.exit_code == 0

        # Act
        second = runner.invoke(cli_module.app, ["admin", "add", "sizuru-admin1", "--display-name", "別名"])

        # Assert
        assert second.exit_code == 1
        rows = repo.list_admin_users()
        assert len(rows) == 1
        assert rows[0].display_name == "管理者1"

    def test_normalizes_login_id_case_and_whitespace(
        self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo
    ) -> None:
        # Arrange
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)

        # Act
        result = runner.invoke(cli_module.app, ["admin", "add", "  Sizuru-Admin1  "])

        # Assert
        assert result.exit_code == 0
        assert json.loads(result.output)["id"] == "sizuru-admin1"

    def test_rollback_failure_exits_one_with_details_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo
    ) -> None:
        # Arrange: both the admin_users insert and the rollback delete fail
        # — the CLI must surface this (not swallow it) and exit non-zero.
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)
        repo.insert_admin_user_exception = RuntimeError("db down")
        repo.delete_auth_user_exception = RuntimeError("network down")

        # Act
        result = runner.invoke(cli_module.app, ["admin", "add", "sizuru-admin1"])

        # Assert
        assert result.exit_code == 1
        assert "db down" in result.output
        assert "network down" in result.output


class TestAdminResetPasswordCommand:
    def test_reissues_the_password_for_an_existing_account(
        self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo
    ) -> None:
        # Arrange
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)
        runner.invoke(cli_module.app, ["admin", "add", "sizuru-admin1"])

        # Act
        result = runner.invoke(cli_module.app, ["admin", "reset-password", "sizuru-admin1"])

        # Assert
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["id"] == "sizuru-admin1"
        assert payload["email"] == "sizuru-admin1@sizuru.local"
        assert len(payload["password"]) == 16

    def test_unknown_login_id_exits_one(self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo) -> None:
        # Arrange
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)

        # Act
        result = runner.invoke(cli_module.app, ["admin", "reset-password", "no-such-admin"])

        # Assert
        assert result.exit_code == 1

    def test_does_not_reset_an_auth_user_that_is_not_an_admin(
        self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo
    ) -> None:
        # Arrange: SECURITY regression — an auth user exists for this email
        # but was never added to admin_users.
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)
        repo.create_auth_user(email="sizuru-admin1@sizuru.local", password="whatever-not-admin1")

        # Act
        result = runner.invoke(cli_module.app, ["admin", "reset-password", "sizuru-admin1"])

        # Assert
        assert result.exit_code == 1

    def test_normalizes_login_id_case_and_whitespace(
        self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo
    ) -> None:
        # Arrange
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)
        runner.invoke(cli_module.app, ["admin", "add", "sizuru-admin1"])

        # Act
        result = runner.invoke(cli_module.app, ["admin", "reset-password", "  Sizuru-Admin1  "])

        # Assert
        assert result.exit_code == 0
        assert json.loads(result.output)["id"] == "sizuru-admin1"


class TestAdminListCommand:
    def test_lists_accounts_without_passwords(self, monkeypatch: pytest.MonkeyPatch, repo: InMemoryRepo) -> None:
        # Arrange
        monkeypatch.setattr(cli_module, "build_repo", lambda settings: repo)
        runner.invoke(cli_module.app, ["admin", "add", "sizuru-admin1", "--display-name", "管理者1"])
        runner.invoke(cli_module.app, ["admin", "add", "sizuru-admin2", "--display-name", "管理者2"])

        # Act
        result = runner.invoke(cli_module.app, ["admin", "list"])

        # Assert
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert {row["id"] for row in payload} == {"sizuru-admin1", "sizuru-admin2"}
        assert all("password" not in row for row in payload)
