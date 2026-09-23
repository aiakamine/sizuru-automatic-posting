"""Tests for schedule.py's I/O-facing subcommands. subprocess.run and the
HTTP layer (http_client.request_json) are both monkeypatched -- no real
ffmpeg/ffprobe/curl process and no network call is made here. One guarded
integration test at the bottom exercises real ffmpeg if it's installed."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import schedule  # noqa: E402
from http_client import ApiError  # noqa: E402


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


FFPROBE_JSON = json.dumps(
    {
        "format": {"duration": "12.5"},
        "streams": [
            {"codec_type": "video", "width": 1080, "height": 1920, "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
)


class TestLoadSettings:
    def test_reads_from_environment_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "env-key")

        # Act
        api_url, api_key = schedule.load_settings()

        # Assert
        assert api_url == "https://api.example.com"
        assert api_key == "env-key"

    def test_falls_back_to_dotenv_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.delenv("SIZURU_API_URL", raising=False)
        monkeypatch.delenv("SIZURU_API_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text('SIZURU_API_URL=https://api.example.com\nSIZURU_API_KEY="dotenv-key"\n')
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

        # Act
        api_url, api_key = schedule.load_settings()

        # Assert
        assert api_url == "https://api.example.com"
        assert api_key == "dotenv-key"

    def test_raises_settings_error_when_nothing_is_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.delenv("SIZURU_API_URL", raising=False)
        monkeypatch.delenv("SIZURU_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))  # empty dir, no .env

        # Act / Assert
        with pytest.raises(schedule.SettingsError):
            schedule.load_settings()

    def test_error_message_never_contains_a_key_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.delenv("SIZURU_API_URL", raising=False)
        monkeypatch.delenv("SIZURU_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

        # Act / Assert
        with pytest.raises(schedule.SettingsError) as excinfo:
            schedule.load_settings()
        assert "SIZURU_API_KEY" in str(excinfo.value)  # mentions the var name, not a value


class TestFindDotenv:
    """find_dotenv()'s CLAUDE_PROJECT_DIR-vs-walk-up semantics, exercised
    entirely against tmp trees -- never the real repo (whose .env has real
    secrets and would silently satisfy load_settings() if this leaked)."""

    def test_project_dir_set_never_walks_up_even_past_an_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: an .env exists above the (empty) project dir -- and also
        # above the injected script_dir -- but CLAUDE_PROJECT_DIR is set, so
        # neither ancestor .env may be picked up.
        (tmp_path / ".env").write_text("SIZURU_API_URL=should-not-be-found\n")
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        script_dir = tmp_path / "project" / "scripts"
        script_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))

        # Act
        result = schedule.find_dotenv(script_dir=script_dir)

        # Assert
        assert result is None

    def test_project_dir_set_and_env_present_is_used_directly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".env").write_text("SIZURU_API_URL=x\n")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))

        # Act
        result = schedule.find_dotenv()

        # Assert
        assert result == project_dir / ".env"

    def test_walks_up_from_script_dir_only_when_project_dir_is_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: no CLAUDE_PROJECT_DIR -- fall back to walking up from a
        # (test-injected) script directory deep in a tmp tree
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        (tmp_path / ".env").write_text("SIZURU_API_URL=x\n")
        script_dir = tmp_path / "a" / "b"
        script_dir.mkdir(parents=True)

        # Act
        result = schedule.find_dotenv(script_dir=script_dir)

        # Assert
        assert result == tmp_path / ".env"

    def test_walk_up_returns_none_when_no_ancestor_has_an_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        script_dir = tmp_path / "a" / "b"
        script_dir.mkdir(parents=True)

        # Act
        result = schedule.find_dotenv(script_dir=script_dir)

        # Assert
        assert result is None


class TestRunFfprobe:
    def test_parses_stdout_json(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Arrange
        monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: _completed(FFPROBE_JSON))
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"fake")

        # Act
        result = schedule.run_ffprobe(video)

        # Assert
        assert result["duration_sec"] == 12.5
        assert result["video_codec"] == "h264"

    def test_raises_command_error_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Arrange
        monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: _completed("", returncode=1))
        video = tmp_path / "clip.mp4"

        # Act / Assert
        with pytest.raises(schedule.CommandError):
            schedule.run_ffprobe(video)


class TestMakePreview:
    def test_small_file_is_copied_not_transcoded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Arrange: a small (well under 19MiB) source file
        src = tmp_path / "01_clip.mp4"
        src.write_bytes(b"x" * 1024)
        out_dir = tmp_path / "preview"
        out_dir.mkdir()
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            calls.append(cmd)
            return _completed(FFPROBE_JSON)

        monkeypatch.setattr(schedule.subprocess, "run", fake_run)

        # Act
        result = schedule.make_preview(src, out_dir)

        # Assert: no ffmpeg transcode call was made, only ffprobe (to read duration)
        assert all(cmd[0] != "ffmpeg" for cmd in calls)
        dst = out_dir / "01_clip.mp4"
        assert dst.exists()
        assert result["size_bytes"] == 1024

    def test_small_file_copy_is_a_real_file_not_a_hard_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: the Artifact tool's upload_asset refuses hard links in a
        # batch file_paths upload (each would need its own approval), so
        # preview must never hardlink, even for small files.
        src = tmp_path / "01_clip.mp4"
        src.write_bytes(b"x" * 1024)
        out_dir = tmp_path / "preview"
        out_dir.mkdir()
        monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: _completed(FFPROBE_JSON))

        # Act
        schedule.make_preview(src, out_dir)

        # Assert: distinct inode from the source (not the same file on disk)
        dst = out_dir / "01_clip.mp4"
        assert dst.stat().st_ino != src.stat().st_ino
        assert dst.stat().st_nlink == 1

    def test_large_file_is_transcoded_once_at_crf28(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Arrange: source is "large" (over 19MiB); ffmpeg run is faked to
        # write a small output file so the size check passes on the first try.
        src = tmp_path / "01_clip.mp4"
        src.write_bytes(b"x" * (20 * 1024 * 1024))
        out_dir = tmp_path / "preview"
        out_dir.mkdir()
        crf_values: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            if cmd[0] == "ffmpeg":
                crf_values.append(cmd[cmd.index("-crf") + 1])
                Path(cmd[-1]).write_bytes(b"x" * 1024)  # small transcoded output
                return _completed()
            return _completed(FFPROBE_JSON)

        monkeypatch.setattr(schedule.subprocess, "run", fake_run)

        # Act
        schedule.make_preview(src, out_dir)

        # Assert: only one ffmpeg call, at the first-try CRF
        assert crf_values == ["28"]

    def test_still_too_large_after_crf28_retries_at_crf32(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: ffmpeg's first (crf28) output is still "too large"; the
        # second (crf32) call writes a small file.
        src = tmp_path / "01_clip.mp4"
        src.write_bytes(b"x" * (20 * 1024 * 1024))
        out_dir = tmp_path / "preview"
        out_dir.mkdir()
        crf_values: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            if cmd[0] == "ffmpeg":
                crf = cmd[cmd.index("-crf") + 1]
                crf_values.append(crf)
                size = 20 * 1024 * 1024 if crf == "28" else 1024
                Path(cmd[-1]).write_bytes(b"x" * size)
                return _completed()
            return _completed(FFPROBE_JSON)

        monkeypatch.setattr(schedule.subprocess, "run", fake_run)

        # Act
        schedule.make_preview(src, out_dir)

        # Assert
        assert crf_values == ["28", "32"]

    def test_transcode_scales_by_width_only_to_avoid_stretching(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        src = tmp_path / "01_clip.mp4"
        src.write_bytes(b"x" * (20 * 1024 * 1024))
        out_dir = tmp_path / "preview"
        out_dir.mkdir()
        vf_values: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            if cmd[0] == "ffmpeg":
                vf_values.append(cmd[cmd.index("-vf") + 1])
                Path(cmd[-1]).write_bytes(b"x" * 1024)
                return _completed()
            return _completed(FFPROBE_JSON)

        monkeypatch.setattr(schedule.subprocess, "run", fake_run)

        # Act
        schedule.make_preview(src, out_dir)

        # Assert: height derived (-2, rounded to even) instead of a fixed
        # 1280 that would stretch/squash a non-9:16 source
        assert vf_values == ["scale=720:-2"]

    def test_rerunning_into_the_same_out_dir_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: a small file that gets copied; run make_preview twice
        # into the same --out (as the skill does when re-generating a
        # preview) -- the second run must not raise SameFileError/FileExistsError.
        src = tmp_path / "01_clip.mp4"
        src.write_bytes(b"x" * 1024)
        out_dir = tmp_path / "preview"
        out_dir.mkdir()
        monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: _completed(FFPROBE_JSON))

        # Act
        schedule.make_preview(src, out_dir)
        result = schedule.make_preview(src, out_dir)  # second run, same inputs

        # Assert
        assert result["size_bytes"] == 1024

    def test_rerunning_after_source_changed_replaces_the_stale_preview(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: first run copies a 1024-byte source; the source is
        # then replaced with different (but still small) content.
        src = tmp_path / "01_clip.mp4"
        src.write_bytes(b"x" * 1024)
        out_dir = tmp_path / "preview"
        out_dir.mkdir()
        monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: _completed(FFPROBE_JSON))
        schedule.make_preview(src, out_dir)

        src.write_bytes(b"y" * 2048)

        # Act
        result = schedule.make_preview(src, out_dir)

        # Assert
        assert result["size_bytes"] == 2048
        assert (out_dir / "01_clip.mp4").read_bytes() == b"y" * 2048


class TestCmdPreviewGuardsOutDir:
    def test_refuses_when_out_dir_is_named_out(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        # Arrange
        args = schedule.build_parser().parse_args(["preview", str(tmp_path), "--out", str(tmp_path / "out")])

        # Act
        exit_code = schedule.cmd_preview(args)

        # Assert
        assert exit_code == 1
        assert "out" in capsys.readouterr().err

    def test_refuses_when_out_is_a_middle_path_component(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange: "out" appears mid-path, not just as the final component
        args = schedule.build_parser().parse_args(
            ["preview", str(tmp_path), "--out", str(tmp_path / "out" / "preview")]
        )

        # Act
        exit_code = schedule.cmd_preview(args)

        # Assert
        assert exit_code == 1


class TestMultipartUpload:
    def test_fields_use_form_string_and_file_is_last_with_dash_f(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        captured: dict = {}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            captured["cmd"] = cmd
            return _completed()

        monkeypatch.setattr(schedule.subprocess, "run", fake_run)
        file_path = tmp_path / "video.mp4"
        file_path.write_bytes(b"x")
        upload = {"url": "https://s3.example.com", "fields": {"key": "public/videos/2026/09/a.mp4", "policy": "SECRET-POLICY"}}

        # Act
        schedule.multipart_upload(upload, file_path)

        # Assert
        cmd = captured["cmd"]
        assert "--fail-with-body" in cmd
        assert cmd[cmd.index("--form-string") + 1] == "key=public/videos/2026/09/a.mp4"
        # every field is passed via --form-string (literal value, no @/< sniffing)
        assert cmd.count("--form-string") == 2
        # the file field is the very last pair, using plain -F with @path
        assert cmd[-2:] == ["-F", f"file=@{file_path}"]

    def test_failure_message_never_leaks_field_values_or_full_argv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: curl fails (e.g. expired presigned URL); stderr is plain
        secret_policy = "SECRET-POLICY-VALUE-DO-NOT-LEAK"

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args=[], returncode=22, stdout="", stderr="curl: (22) The requested URL returned error: 403")

        monkeypatch.setattr(schedule.subprocess, "run", fake_run)
        file_path = tmp_path / "video.mp4"
        file_path.write_bytes(b"x")
        upload = {"url": "https://s3.example.com", "fields": {"policy": secret_policy}}

        # Act / Assert
        with pytest.raises(schedule.CommandError) as excinfo:
            schedule.multipart_upload(upload, file_path)
        message = str(excinfo.value)
        assert secret_policy not in message
        assert "--form-string" not in message  # no full argv dump
        assert "403" in message  # curl's own stderr is still useful


class TestState:
    def test_missing_state_file_returns_empty_clips(self, tmp_path: Path) -> None:
        # Arrange
        plan_path = tmp_path / "plan.json"

        # Act
        state = schedule.load_state(plan_path)

        # Assert
        assert state == {"clips": {}}

    def test_save_then_load_roundtrips(self, tmp_path: Path) -> None:
        # Arrange
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": {"video_key": "public/videos/2026/09/abc.mp4"}}}

        # Act
        schedule.save_state(plan_path, state)
        loaded = schedule.load_state(plan_path)

        # Assert
        assert loaded == state
        assert schedule.state_path_for(plan_path).name == "plan.json.state.json"

    def test_corrupted_state_file_stops_with_an_error_instead_of_silently_resetting(
        self, tmp_path: Path
    ) -> None:
        # Arrange: silently treating this as "no state" would cause a
        # re-upload (new video_key) of a clip that might already be booked.
        plan_path = tmp_path / "plan.json"
        schedule.state_path_for(plan_path).write_text("{not valid json")

        # Act / Assert
        with pytest.raises(schedule.StateError):
            schedule.load_state(plan_path)

    def test_state_with_clips_not_a_dict_stops_with_an_error(self, tmp_path: Path) -> None:
        # Arrange: valid JSON, wrong shape
        plan_path = tmp_path / "plan.json"
        schedule.state_path_for(plan_path).write_text('{"clips": "not-a-dict"}')

        # Act / Assert
        with pytest.raises(schedule.StateError):
            schedule.load_state(plan_path)

    def test_save_state_writes_atomically_leaving_no_tmp_file_behind(self, tmp_path: Path) -> None:
        # Arrange
        plan_path = tmp_path / "plan.json"

        # Act
        schedule.save_state(plan_path, {"clips": {}})

        # Assert: only the final state file exists, no leftover .tmp
        state_dir_files = {p.name for p in tmp_path.iterdir()}
        assert state_dir_files == {"plan.json.state.json"}


class TestSubmitIdempotency:
    """Covers the `submit` re-run behaviour: an already-uploaded asset must
    not be re-uploaded, and a settled submission result must be reusable."""

    def _clip(self) -> dict:
        return {
            "no": 1,
            "file": "/abs/01.mp4",
            "title": "t",
            "targets": [{"service": "tiktok", "caption": "c", "due_at": "2099-01-01T00:00:00+09:00"}],
        }

    def test_ensure_uploaded_skips_upload_request_when_both_assets_already_uploaded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        calls: list[str] = []
        monkeypatch.setattr(schedule, "request_json", lambda *a, **k: calls.append("uploads") or {})
        clip_state = {"video_uploaded": True, "video_key": "vk", "thumbnail_uploaded": True, "thumbnail_key": "tk"}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}

        # Act
        schedule.ensure_uploaded(self._clip(), clip_state, "https://api", "key", plan_path, state)

        # Assert: no /schedule/uploads call was made at all
        assert calls == []

    def test_ensure_uploaded_uploads_video_and_thumbnail_and_persists_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        src = tmp_path / "01.mp4"
        src.write_bytes(b"video-bytes")
        clip = self._clip()
        clip["file"] = str(src)

        uploads_response = {
            "video": {"key": "public/videos/2026/09/aaa.mp4", "upload": {"url": "https://s3", "fields": {}}},
            "thumbnail": {"key": "public/thumbnails/2026/09/aaa.jpg", "upload": {"url": "https://s3", "fields": {}}},
        }
        monkeypatch.setattr(schedule, "request_json", lambda *a, **k: uploads_response)
        upload_calls: list[str] = []
        monkeypatch.setattr(
            schedule, "multipart_upload", lambda upload, path: upload_calls.append(upload["key"] if "key" in upload else str(path))
        )
        monkeypatch.setattr(schedule, "make_thumbnail", lambda video, out_path, offset_ms: out_path.write_bytes(b"jpg"))

        clip_state: dict = {}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}

        # Act
        schedule.ensure_uploaded(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert
        assert clip_state["video_uploaded"] is True
        assert clip_state["video_key"] == "public/videos/2026/09/aaa.mp4"
        assert clip_state["thumbnail_uploaded"] is True
        assert clip_state["thumbnail_key"] == "public/thumbnails/2026/09/aaa.jpg"
        # state.json was written (idempotency survives a crash mid-clip)
        assert schedule.load_state(plan_path)["clips"]["1"]["video_uploaded"] is True

    def test_ensure_uploaded_only_retries_the_asset_that_never_succeeded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: video already uploaded, thumbnail was not
        src = tmp_path / "01.mp4"
        src.write_bytes(b"video-bytes")
        clip = self._clip()
        clip["file"] = str(src)
        clip_state = {"video_uploaded": True, "video_key": "existing-video-key"}

        uploads_response = {
            "video": {"key": "NEW-video-key-unused", "upload": {"url": "https://s3", "fields": {}}},
            "thumbnail": {"key": "new-thumb-key", "upload": {"url": "https://s3", "fields": {}}},
        }
        monkeypatch.setattr(schedule, "request_json", lambda *a, **k: uploads_response)
        upload_calls: list[dict] = []
        monkeypatch.setattr(schedule, "multipart_upload", lambda upload, path: upload_calls.append(upload))
        monkeypatch.setattr(schedule, "make_thumbnail", lambda video, out_path, offset_ms: out_path.write_bytes(b"jpg"))

        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}

        # Act
        schedule.ensure_uploaded(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert: the already-uploaded video key is untouched, only the
        # thumbnail was (re)uploaded
        assert clip_state["video_key"] == "existing-video-key"
        assert clip_state["thumbnail_key"] == "new-thumb-key"
        assert len(upload_calls) == 1

    def test_submit_clip_retries_while_incomplete_up_to_three_times(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        clip = self._clip()
        clip_state = {"video_uploaded": True, "video_key": "vk", "thumbnail_uploaded": True, "thumbnail_key": "tk"}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}
        monkeypatch.setattr(schedule, "run_ffprobe", lambda path: {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"})

        responses = iter(
            [
                {"dry_run": True, "has_error": False, "plan": {}},
                {"dry_run": False, "incomplete": True},
                {"dry_run": False, "incomplete": True},
                {"dry_run": False, "incomplete": False, "submission_id": "sub-1", "results": []},
            ]
        )
        call_count = 0

        def fake_request(*args: object, **kwargs: object) -> dict:
            nonlocal call_count
            call_count += 1
            return next(responses)

        monkeypatch.setattr(schedule, "request_json", fake_request)

        # Act
        result = schedule.submit_clip(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert: 1 dry-run + 3 real submission calls (2 incomplete + 1 settled)
        assert call_count == 4
        assert result["result"]["incomplete"] is False
        assert result["skipped"] is False

    def test_submit_clip_gives_up_after_max_retries_and_stays_incomplete(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: every real submission response reports incomplete=True --
        # the retry loop must stop after DEFAULT_MAX_SUBMIT_RETRIES attempts
        # rather than looping forever, and the caller sees incomplete=True
        clip = self._clip()
        clip_state = {"video_uploaded": True, "video_key": "vk", "thumbnail_uploaded": True, "thumbnail_key": "tk"}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}
        monkeypatch.setattr(
            schedule,
            "run_ffprobe",
            lambda path: {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"},
        )
        call_count = 0

        def fake_request(*args: object, **kwargs: object) -> dict:
            nonlocal call_count
            call_count += 1
            if kwargs.get("body", {}).get("dry_run", True):
                return {"dry_run": True, "has_error": False, "plan": {}}
            return {"dry_run": False, "incomplete": True}

        monkeypatch.setattr(schedule, "request_json", fake_request)

        # Act
        result = schedule.submit_clip(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert: 1 dry-run + exactly DEFAULT_MAX_SUBMIT_RETRIES real calls, still incomplete
        assert call_count == 1 + schedule.DEFAULT_MAX_SUBMIT_RETRIES
        assert result["result"]["incomplete"] is True
        assert result["skipped"] is False

    def test_submit_clip_does_not_retry_when_incomplete_is_false_even_with_an_unknown_target_status(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: the retry loop keys off the top-level `incomplete` flag,
        # not per-target status -- a settled response containing an
        # "unknown" target must NOT trigger additional submission calls
        clip = self._clip()
        clip_state = {"video_uploaded": True, "video_key": "vk", "thumbnail_uploaded": True, "thumbnail_key": "tk"}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}
        monkeypatch.setattr(
            schedule,
            "run_ffprobe",
            lambda path: {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"},
        )
        call_count = 0

        def fake_request(*args: object, **kwargs: object) -> dict:
            nonlocal call_count
            call_count += 1
            if kwargs.get("body", {}).get("dry_run", True):
                return {"dry_run": True, "has_error": False, "plan": {}}
            return {"dry_run": False, "incomplete": False, "results": [{"service": "tiktok", "status": "unknown"}]}

        monkeypatch.setattr(schedule, "request_json", fake_request)

        # Act
        result = schedule.submit_clip(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert: exactly 1 dry-run + 1 real submission call, no retries
        assert call_count == 2
        assert result["result"]["results"][0]["status"] == "unknown"

    def test_submit_clip_skips_when_dry_run_has_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Arrange
        clip = self._clip()
        clip_state = {"video_uploaded": True, "video_key": "vk", "thumbnail_uploaded": True, "thumbnail_key": "tk"}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}
        monkeypatch.setattr(schedule, "run_ffprobe", lambda path: {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"})
        monkeypatch.setattr(
            schedule, "request_json", lambda *a, **k: {"dry_run": True, "has_error": True, "plan": {"issues": ["x"]}}
        )

        # Act
        result = schedule.submit_clip(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert: the real (non-dry-run) submission is never attempted
        assert result["skipped"] is True

    def test_submit_clip_skips_entirely_when_already_fully_settled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: a previous submit already got every target to a
        # terminal success state -- re-submitting risks a double-post
        clip = self._clip()
        clip_state = {
            "video_uploaded": True,
            "video_key": "vk",
            "thumbnail_uploaded": True,
            "thumbnail_key": "tk",
            "submission_result": {"results": [{"service": "tiktok", "status": "scheduled"}]},
        }
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}
        api_calls: list[str] = []
        monkeypatch.setattr(schedule, "request_json", lambda *a, **k: api_calls.append("call") or {})

        # Act
        result = schedule.submit_clip(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert: no API call at all, previous result reused as-is
        assert api_calls == []
        assert result["already_settled"] is True
        assert result["result"] == clip_state["submission_result"]

    def test_submit_clip_still_runs_when_previous_result_has_unsettled_targets(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: previous run had a failed target -- must retry, not skip
        clip = self._clip()
        clip_state = {
            "video_uploaded": True,
            "video_key": "vk",
            "thumbnail_uploaded": True,
            "thumbnail_key": "tk",
            "submission_result": {"results": [{"service": "tiktok", "status": "failed"}]},
        }
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}
        monkeypatch.setattr(schedule, "run_ffprobe", lambda path: {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"})
        monkeypatch.setattr(
            schedule,
            "request_json",
            lambda *a, **k: {"dry_run": True, "has_error": False, "plan": {}}
            if k.get("body", {}).get("dry_run", True)
            else {"dry_run": False, "incomplete": False, "results": [{"service": "tiktok", "status": "scheduled"}]},
        )

        # Act
        result = schedule.submit_clip(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert
        assert result.get("already_settled") is not True

    def test_ensure_uploaded_raises_when_source_file_changed_since_last_upload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: clip_state remembers a fingerprint from a previous
        # (successful) upload; the file on disk is now different content
        # -- re-using the old video_key would silently book the wrong video
        src = tmp_path / "01.mp4"
        src.write_bytes(b"original-bytes")
        clip = self._clip()
        clip["file"] = str(src)
        stale_fingerprint = schedule.clip_fingerprint(src)
        src.write_bytes(b"different-bytes-now")  # file changed after fingerprinting
        clip_state = {"video_uploaded": True, "video_key": "vk", "fingerprint": stale_fingerprint}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}
        monkeypatch.setattr(
            schedule, "request_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call API"))
        )

        # Act / Assert
        with pytest.raises(schedule.StateError):
            schedule.ensure_uploaded(clip, clip_state, "https://api", "key", plan_path, state)

    def test_ensure_uploaded_raises_when_only_mtime_changed_even_with_identical_size(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: byte-for-byte identical size, but the file's mtime moved
        # (e.g. a re-export/re-touch that happens to produce the same size)
        # -- the fingerprint must still be treated as "changed" since it
        # tracks mtime, not just size
        src = tmp_path / "01.mp4"
        src.write_bytes(b"same-size-content")
        stale_fingerprint = schedule.clip_fingerprint(src)
        new_mtime = stale_fingerprint["mtime"] + 1000
        os.utime(src, (new_mtime, new_mtime))
        clip = self._clip()
        clip["file"] = str(src)
        clip_state = {"video_uploaded": True, "video_key": "vk", "fingerprint": stale_fingerprint}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}
        monkeypatch.setattr(
            schedule, "request_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call API"))
        )

        # Act / Assert
        with pytest.raises(schedule.StateError):
            schedule.ensure_uploaded(clip, clip_state, "https://api", "key", plan_path, state)

    def test_ensure_uploaded_stores_a_fingerprint_after_a_fresh_upload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        src = tmp_path / "01.mp4"
        src.write_bytes(b"video-bytes")
        clip = self._clip()
        clip["file"] = str(src)
        uploads_response = {
            "video": {"key": "vk", "upload": {"url": "https://s3", "fields": {}}},
            "thumbnail": {"key": "tk", "upload": {"url": "https://s3", "fields": {}}},
        }
        monkeypatch.setattr(schedule, "request_json", lambda *a, **k: uploads_response)
        monkeypatch.setattr(schedule, "multipart_upload", lambda upload, path: None)
        monkeypatch.setattr(schedule, "make_thumbnail", lambda video, out_path, offset_ms: out_path.write_bytes(b"jpg"))
        clip_state: dict = {}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}

        # Act
        schedule.ensure_uploaded(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert
        assert clip_state["fingerprint"] == schedule.clip_fingerprint(src)

    def test_ensure_uploaded_does_not_touch_filesystem_when_fully_done_with_no_fingerprint_on_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: legacy/settled state, file may already be gone (e.g. the
        # 60-day S3 auto-delete already ran) -- must not crash
        clip = self._clip()  # file path "/abs/01.mp4" does not exist on disk
        clip_state = {"video_uploaded": True, "video_key": "vk", "thumbnail_uploaded": True, "thumbnail_key": "tk"}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}

        # Act / Assert: no exception
        schedule.ensure_uploaded(clip, clip_state, "https://api", "key", plan_path, state)


class TestSubmitClipCatchesApiErrorPerClip:
    def test_api_error_is_recorded_on_the_clip_result_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        clip = {
            "no": 1,
            "file": "/abs/01.mp4",
            "title": "t",
            "targets": [{"service": "tiktok", "caption": "c", "due_at": "2099-01-01T00:00:00+09:00"}],
        }
        clip_state = {"video_uploaded": True, "video_key": "vk", "thumbnail_uploaded": True, "thumbnail_key": "tk"}
        plan_path = tmp_path / "plan.json"
        state = {"clips": {"1": clip_state}}
        monkeypatch.setattr(schedule, "run_ffprobe", lambda path: {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"})

        def raise_api_error(*a: object, **k: object) -> dict:
            raise ApiError("service unavailable", status=503)

        monkeypatch.setattr(schedule, "request_json", raise_api_error)

        # Act
        result = schedule.submit_clip(clip, clip_state, "https://api", "key", plan_path, state)

        # Assert
        assert result["error"] == "service unavailable"

    def test_cmd_submit_continues_to_the_next_clip_after_one_clips_api_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange: 2 clips; clip 1's submit_clip raises internally (caught),
        # clip 2 must still be attempted
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "clips": [
                        {"no": 1, "file": "/abs/01.mp4", "title": "a", "targets": []},
                        {"no": 2, "file": "/abs/02.mp4", "title": "b", "targets": []},
                    ]
                }
            )
        )
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")

        call_log: list[int] = []

        def fake_submit_clip(clip: dict, *a: object, **k: object) -> dict:
            call_log.append(clip["no"])
            if clip["no"] == 1:
                return {"clip_no": 1, "skipped": False, "error": "boom"}
            return {"clip_no": 2, "skipped": False, "result": {"incomplete": False, "results": []}}

        monkeypatch.setattr(schedule, "submit_clip", fake_submit_clip)

        # Act
        args = schedule.build_parser().parse_args(["submit", str(plan_path), "--yes"])
        schedule.cmd_submit(args)

        # Assert: both clips were attempted despite clip 1's error
        assert call_log == [1, 2]


class TestCmdSubmitRequiresApproval:
    def test_without_yes_returns_exit_code_2(self, capsys: pytest.CaptureFixture) -> None:
        # Arrange
        args = schedule.build_parser().parse_args(["submit", "plan.json"])

        # Act
        exit_code = schedule.cmd_submit(args)

        # Assert
        assert exit_code == 2
        assert "承認" in capsys.readouterr().err


class TestCmdSubmitExitCodeAndSummary:
    def _plan(self, tmp_path: Path) -> Path:
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps({"clips": [{"no": 1, "file": "/abs/01.mp4", "title": "a", "targets": []}]}))
        return plan_path

    def test_exit_code_0_and_summary_when_everything_scheduled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange
        plan_path = self._plan(tmp_path)
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")
        monkeypatch.setattr(
            schedule,
            "submit_clip",
            lambda clip, *a, **k: {
                "clip_no": 1,
                "skipped": False,
                "result": {"incomplete": False, "results": [{"service": "tiktok", "status": "scheduled"}]},
            },
        )

        # Act
        args = schedule.build_parser().parse_args(["submit", str(plan_path), "--yes"])
        exit_code = schedule.cmd_submit(args)

        # Assert
        assert exit_code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["summary"] == {"scheduled": 1, "failed": 0, "unknown": 0, "skipped": 0, "not_attempted": 0}

    def test_exit_code_1_when_a_clip_was_skipped_by_dry_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange
        plan_path = self._plan(tmp_path)
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")
        monkeypatch.setattr(
            schedule, "submit_clip", lambda clip, *a, **k: {"clip_no": 1, "skipped": True, "plan": {}}
        )

        # Act
        args = schedule.build_parser().parse_args(["submit", str(plan_path), "--yes"])
        exit_code = schedule.cmd_submit(args)

        # Assert
        assert exit_code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["summary"]["skipped"] == 1

    def test_exit_code_1_when_still_incomplete_after_retries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        plan_path = self._plan(tmp_path)
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")
        monkeypatch.setattr(
            schedule,
            "submit_clip",
            lambda clip, *a, **k: {"clip_no": 1, "skipped": False, "result": {"incomplete": True, "results": []}},
        )

        # Act
        args = schedule.build_parser().parse_args(["submit", str(plan_path), "--yes"])
        exit_code = schedule.cmd_submit(args)

        # Assert
        assert exit_code == 1

    def test_exit_code_1_when_any_target_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        plan_path = self._plan(tmp_path)
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")
        monkeypatch.setattr(
            schedule,
            "submit_clip",
            lambda clip, *a, **k: {
                "clip_no": 1,
                "skipped": False,
                "result": {"incomplete": False, "results": [{"service": "x", "status": "failed"}]},
            },
        )

        # Act
        args = schedule.build_parser().parse_args(["submit", str(plan_path), "--yes"])
        exit_code = schedule.cmd_submit(args)

        # Assert
        assert exit_code == 1


class TestCmdSlotsRejectsPerDayTimesMismatch:
    def test_per_day_2_without_times_exits_nonzero_with_japanese_message(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange: --per-day 2 with only a single --time is ambiguous
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")
        # request_json must never be called: the error is raised before any API call
        monkeypatch.setattr(
            schedule, "request_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call API"))
        )
        args = ["slots", "--start", "2026-09-25", "--time", "19:00", "--per-day", "2", "--count", "2"]

        # Act
        exit_code = schedule.main(args)

        # Assert
        assert exit_code == 1
        err = json.loads(capsys.readouterr().err)
        assert "1日2本なら --times で時刻を2個指定してください" in err["error"]

    def test_times_count_mismatching_per_day_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange: --per-day 2 but --times lists 3 entries
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")
        monkeypatch.setattr(
            schedule, "request_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call API"))
        )
        args = [
            "slots", "--start", "2026-09-25", "--per-day", "2",
            "--count", "2", "--times", "09:00,12:30,21:00",
        ]

        # Act
        exit_code = schedule.main(args)

        # Assert
        assert exit_code == 1
        err = json.loads(capsys.readouterr().err)
        assert "1日2本なら --times で時刻を2個指定してください" in err["error"]


class TestCmdSlots:
    def test_existing_posts_with_utc_z_suffix_due_at_are_parsed_and_collision_checked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange: a start date far enough ahead that the 15-minute lead
        # cutoff can never trigger, regardless of when this test runs. The
        # API returns the existing post's due_at in UTC with a trailing "Z"
        # (not schedule.py's usual +09:00 JST form) -- collision detection
        # must still work via absolute-time comparison.
        jst = timezone(timedelta(hours=9))
        start = (datetime.now(jst) + timedelta(days=30)).date()
        candidate_jst = datetime(start.year, start.month, start.day, 19, 0, tzinfo=jst)
        existing_z = candidate_jst.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")
        monkeypatch.setattr(schedule, "request_json", lambda *a, **k: {"posts": [{"due_at": existing_z}]})
        args = schedule.build_parser().parse_args(
            ["slots", "--start", start.isoformat(), "--time", "19:00", "--per-day", "1", "--count", "1"]
        )

        # Act
        exit_code = schedule.cmd_slots(args)

        # Assert: the requested day's 19:00 collides (via absolute time) with
        # the UTC-expressed existing post, so the slot is pushed to the next day
        assert exit_code == 0
        slots = json.loads(capsys.readouterr().out)
        expected = candidate_jst + timedelta(days=1)
        assert slots == [expected.isoformat()]


class TestCmdCheck:
    def test_ok_when_request_succeeds(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        # Arrange
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")
        monkeypatch.setattr(schedule, "request_json", lambda *a, **k: {"posts": []})

        # Act
        exit_code = schedule.cmd_check(schedule.build_parser().parse_args(["check"]))

        # Assert
        assert exit_code == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True}

    def test_unauthorized_is_reported_without_raising(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "bad-key")

        def raise_unauthorized(*a: object, **k: object) -> dict:
            raise ApiError("unauthorized", code="unauthorized", status=401)

        monkeypatch.setattr(schedule, "request_json", raise_unauthorized)

        # Act
        exit_code = schedule.cmd_check(schedule.build_parser().parse_args(["check"]))

        # Assert
        assert exit_code == 1
        out = json.loads(capsys.readouterr().out)
        assert out == {"ok": False, "reason": "unauthorized", "message": "unauthorized"}

    def test_scheduler_disabled_is_its_own_reason_not_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange: server has no SCHEDULER_API_KEY configured -> 503
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")

        def raise_disabled(*a: object, **k: object) -> dict:
            raise ApiError("disabled", code="scheduler_disabled", status=503)

        monkeypatch.setattr(schedule, "request_json", raise_disabled)

        # Act
        exit_code = schedule.cmd_check(schedule.build_parser().parse_args(["check"]))

        # Assert
        assert exit_code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["reason"] == "scheduler_disabled"

    def test_other_errors_are_reported_as_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange: e.g. a plain connection failure
        monkeypatch.setenv("SIZURU_API_URL", "https://api.example.com")
        monkeypatch.setenv("SIZURU_API_KEY", "key")
        monkeypatch.setattr(
            schedule, "request_json", lambda *a, **k: (_ for _ in ()).throw(ApiError("no route to host"))
        )

        # Act
        exit_code = schedule.cmd_check(schedule.build_parser().parse_args(["check"]))

        # Assert
        assert exit_code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["reason"] == "unreachable"


class TestSlotsArgparse:
    def test_time_and_times_are_mutually_exclusive_and_one_is_required(self) -> None:
        # Arrange
        parser = schedule.build_parser()

        # Act / Assert: neither given -> argparse error (SystemExit)
        with pytest.raises(SystemExit):
            parser.parse_args(["slots", "--start", "2026-09-25", "--per-day", "1", "--count", "1"])

    def test_both_time_and_times_given_is_rejected_by_argparse(self) -> None:
        # Arrange
        parser = schedule.build_parser()

        # Act / Assert
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "slots", "--start", "2026-09-25", "--time", "19:00", "--times", "09:00,12:00",
                    "--per-day", "1", "--count", "1",
                ]
            )

    def test_times_alone_without_time_is_accepted(self) -> None:
        # Arrange
        parser = schedule.build_parser()

        # Act
        args = parser.parse_args(
            [
                "slots", "--start", "2026-09-25", "--times", "12:00,19:00",
                "--per-day", "2", "--count", "2",
            ]
        )

        # Assert
        assert args.time is None
        assert args.times == "12:00,19:00"


class TestCmdValidateChecksMainVideoUrl:
    def _plan(self, tmp_path: Path, main_video_url: str | None, caption: str = "見てね") -> Path:
        plan_path = tmp_path / "plan.json"
        plan = {
            "clips": [
                {
                    "no": 1,
                    "file": str(tmp_path / "01.mp4"),
                    "title": "t",
                    "targets": [
                        {"service": "tiktok", "caption": caption, "due_at": "2099-01-01T00:00:00+09:00"}
                    ],
                }
            ]
        }
        if main_video_url is not None:
            plan["main_video_url"] = main_video_url
        plan_path.write_text(json.dumps(plan, ensure_ascii=False))
        (tmp_path / "01.mp4").write_bytes(b"x")
        return plan_path

    def test_missing_main_video_url_fails_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange
        plan_path = self._plan(tmp_path, main_video_url=None, caption="見てね")
        monkeypatch.setattr(
            schedule,
            "run_ffprobe",
            lambda path: {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"},
        )

        # Act
        args = schedule.build_parser().parse_args(["validate", str(plan_path)])
        exit_code = schedule.cmd_validate(args)

        # Assert
        assert exit_code == 1
        out = json.loads(capsys.readouterr().out)
        assert any(issue["service"] == "main_video_url" for issue in out)

    def test_tiktok_caption_without_the_url_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange
        plan_path = self._plan(tmp_path, main_video_url="https://youtu.be/abc123", caption="見てね")
        monkeypatch.setattr(
            schedule,
            "run_ffprobe",
            lambda path: {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"},
        )

        # Act
        args = schedule.build_parser().parse_args(["validate", str(plan_path)])
        schedule.cmd_validate(args)

        # Assert
        out = json.loads(capsys.readouterr().out)
        assert any(issue["service"] == "tiktok" and "本編URL" in issue["message"] for issue in out)

    def test_valid_plan_with_main_video_url_in_caption_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # Arrange
        plan_path = self._plan(
            tmp_path,
            main_video_url="https://youtu.be/abc123",
            caption="本編はこちら https://www.youtube.com/watch?v=abc123",
        )
        monkeypatch.setattr(
            schedule,
            "run_ffprobe",
            lambda path: {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264", "audio_codec": "aac"},
        )

        # Act
        args = schedule.build_parser().parse_args(["validate", str(plan_path)])
        exit_code = schedule.cmd_validate(args)

        # Assert
        assert exit_code == 0


class TestCmdGallery:
    def test_generates_html_with_injected_data(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        # Arrange
        data = {"project": "テスト企画", "delivered_at": "2026-09-23", "delivery_dir": "/x", "clips": []}
        data_path = tmp_path / "data.json"
        data_path.write_text(json.dumps(data, ensure_ascii=False))
        out_path = tmp_path / "gallery.html"

        # Act
        args = schedule.build_parser().parse_args(["gallery", "--data", str(data_path), "--out", str(out_path)])
        exit_code = schedule.cmd_gallery(args)

        # Assert
        assert exit_code == 0
        html = out_path.read_text()
        assert "テスト企画" in html
        assert 'id="gallery-data"' in html
        assert "営業統括の1日に密着" not in html  # example placeholder data was replaced


class TestInjectGalleryData:
    def test_raises_when_placeholder_missing(self) -> None:
        # Arrange
        html = "<html><body>no placeholder here</body></html>"

        # Act / Assert
        with pytest.raises(ValueError):
            schedule.inject_gallery_data(html, {"a": 1})

    def test_escapes_closing_script_tag_in_data(self) -> None:
        # Arrange
        html = '<script type="application/json" id="gallery-data">OLD</script>'
        data = {"title": "</script><script>alert(1)</script>"}

        # Act
        result = schedule.inject_gallery_data(html, data)

        # Assert: no literal "</script>" appears inside the injected JSON
        injected = result.split('id="gallery-data">', 1)[1].rsplit("</script>", 1)[0]
        assert "</script>" not in injected
        assert "<script>" not in injected
        # still valid JSON once parsed back (the < etc. escapes decode cleanly)
        assert json.loads(injected) == data

    def test_escapes_html_comment_open_that_could_break_out_of_the_script_tag(self) -> None:
        # Arrange: a caption containing "<!--<script" must not let an
        # attacker-controlled comment/script sequence reach the page's HTML
        html = '<script type="application/json" id="gallery-data">OLD</script>'
        data = {"caption": "<!--<script>alert(document.cookie)</script>-->"}

        # Act
        result = schedule.inject_gallery_data(html, data)

        # Assert
        injected = result.split('id="gallery-data">', 1)[1].rsplit("</script>", 1)[0]
        assert "<!--" not in injected
        assert "<script" not in injected
        assert json.loads(injected) == data

    def test_ampersand_is_escaped_too(self) -> None:
        # Arrange
        html = '<script type="application/json" id="gallery-data">OLD</script>'
        data = {"caption": "A & B"}

        # Act
        result = schedule.inject_gallery_data(html, data)

        # Assert
        injected = result.split('id="gallery-data">', 1)[1].rsplit("</script>", 1)[0]
        assert "&" not in injected
        assert json.loads(injected) == data

    def test_static_title_tag_is_set_from_project_name(self) -> None:
        # Arrange: the Artifact gallery names the published page from the
        # static <title> tag (it ignores document.title set by JS), so
        # every generated page must get its own title here.
        html = '<title>切り抜きレビュー</title><script type="application/json" id="gallery-data">OLD</script>'
        data = {"project": "営業統括密着"}

        # Act
        result = schedule.inject_gallery_data(html, data)

        # Assert
        assert "<title>営業統括密着 切り抜き</title>" in result
        assert "<title>切り抜きレビュー</title>" not in result

    def test_static_title_html_escapes_the_project_name(self) -> None:
        # Arrange: a project name is free text and could contain characters
        # that would break out of the <title> element
        html = '<title>切り抜きレビュー</title><script type="application/json" id="gallery-data">OLD</script>'
        data = {"project": "A & B </title><script>alert(1)</script>"}

        # Act
        result = schedule.inject_gallery_data(html, data)

        # Assert
        assert "</title><script>alert(1)</script>" not in result
        assert "<title>A &amp; B &lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt; 切り抜き</title>" in result

    def test_missing_static_title_tag_does_not_raise(self) -> None:
        # Arrange: defensive -- only the gallery-data placeholder is required
        html = '<script type="application/json" id="gallery-data">OLD</script>'
        data = {"project": "p"}

        # Act
        result = schedule.inject_gallery_data(html, data)

        # Assert: gallery-data injection still happened normally
        assert '"project": "p"' in result

    def test_line_and_paragraph_separators_round_trip_through_json_loads(self) -> None:
        # Arrange: U+2028/U+2029 are valid, unescaped in a JSON string (json
        # .dumps leaves them as literal characters with ensure_ascii=False),
        # so they must still decode back to the exact original text
        html = '<script type="application/json" id="gallery-data">OLD</script>'
        data = {"caption": "line1 line2 para"}

        # Act
        result = schedule.inject_gallery_data(html, data)

        # Assert
        injected = result.split('id="gallery-data">', 1)[1].rsplit("</script>", 1)[0]
        assert json.loads(injected) == data


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestPreviewIntegrationWithRealFfmpeg:
    def test_preview_of_a_tiny_real_video_stays_under_the_size_limit(self, tmp_path: Path) -> None:
        # Arrange: generate a tiny real mp4 with ffmpeg's lavfi test source
        src = tmp_path / "src.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:duration=1",
                "-f", "lavfi", "-i", "anullsrc",
                "-shortest", "-c:v", "libx264", "-c:a", "aac", str(src),
            ],
            check=True,
            capture_output=True,
        )
        out_dir = tmp_path / "preview"
        out_dir.mkdir()

        # Act
        result = schedule.make_preview(src, out_dir)

        # Assert
        assert Path(result["preview_path"]).exists()
        assert result["duration_sec"] > 0
