"""Tests for sizuru_post.factory: Settings -> adapter/Publisher construction.

No real AWS/Supabase calls happen here — boto3.Session()/create_client()
just build lightweight client objects without touching the network until a
request method is actually called (none are, in these tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import sizuru_post.factory as factory
from sizuru_post.config import Settings
from sizuru_post.factory import (
    build_buffer_client,
    build_publisher,
    build_publisher_without_uploads,
    build_settings,
    build_storage,
    reset_ssm_cache,
)

REQUIRED_FIELDS: dict[str, object] = {
    "buffer_api_key": "buffer-key",
    "supabase_url": "https://example.supabase.co",
    "supabase_service_role_key": "service-role-key",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED_FIELDS, **overrides})  # type: ignore[arg-type]


def test_build_storage_raises_clear_error_when_s3_bucket_missing() -> None:
    # Arrange
    settings = _settings()  # no S3_BUCKET
    assert settings.s3_bucket is None

    # Act / Assert
    with pytest.raises(ValueError, match="S3_BUCKET"):
        build_storage(settings)


def test_build_storage_succeeds_when_s3_bucket_present() -> None:
    # Arrange
    settings = _settings(s3_bucket="my-bucket")

    # Act
    storage = build_storage(settings)

    # Assert
    assert storage is not None


def test_build_publisher_raises_clear_error_when_s3_bucket_missing() -> None:
    # Arrange: the CLI's publish path needs real storage
    settings = _settings()

    # Act / Assert
    with pytest.raises(ValueError, match="S3_BUCKET"):
        build_publisher(settings)


def test_build_publisher_without_uploads_does_not_require_s3_bucket() -> None:
    # Arrange: the API Lambda only edits/cancels/syncs existing posts
    settings = _settings()
    assert settings.s3_bucket is None

    # Act
    publisher = build_publisher_without_uploads(settings)

    # Assert
    assert publisher is not None


def test_build_buffer_client_defaults_to_30_second_timeout() -> None:
    # Arrange: the CLI relies on this generous default.
    settings = _settings()

    # Act
    client = build_buffer_client(settings)

    # Assert
    assert client._http.timeout.connect == 30.0  # noqa: SLF001 - intentional port access in a test


def test_build_buffer_client_accepts_explicit_timeout_and_max_retries() -> None:
    # Arrange: the Lambda factory needs a short timeout + minimal retries so
    # a slow/erroring Buffer response can't blow the invocation's deadline.
    settings = _settings()

    # Act
    client = build_buffer_client(settings, timeout=8.0, max_retries=1)

    # Assert
    assert client._http.timeout.connect == 8.0  # noqa: SLF001
    assert client._max_retries == 1  # noqa: SLF001


def test_build_publisher_without_uploads_uses_short_timeout_and_single_retry() -> None:
    # Arrange: the API/sync Lambdas must not let a slow Buffer response
    # consume the whole invocation's timeout budget.
    settings = _settings()

    # Act
    publisher = build_publisher_without_uploads(settings)

    # Assert
    assert publisher._buffer._http.timeout.connect == 8.0  # noqa: SLF001
    assert publisher._buffer._max_retries == 1  # noqa: SLF001


def test_build_publisher_uses_default_timeout_and_retries_for_cli() -> None:
    # Arrange
    settings = _settings(s3_bucket="my-bucket")

    # Act
    publisher = build_publisher(settings)

    # Assert
    assert publisher._buffer._http.timeout.connect == 30.0  # noqa: SLF001
    assert publisher._buffer._max_retries == 4  # noqa: SLF001


def test_build_publisher_without_uploads_storage_raises_if_ever_used(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    settings = _settings()
    publisher = build_publisher_without_uploads(settings)

    # Act / Assert: defensive guard — this Publisher is only ever used for
    # edit/cancel/sync, none of which call storage, but if that ever
    # changes it must fail loudly rather than silently no-op.
    with pytest.raises(RuntimeError):
        publisher._storage.upload_video(__import__("pathlib").Path("x.mp4"))


class _FakeSsmClient:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.calls: list[str] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, dict[str, str]]:
        self.calls.append(Name)
        return {"Parameter": {"Value": self._values[Name]}}


class TestBuildSettings:
    def test_uses_plain_env_when_ssm_prefix_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: local CLI usage, no SSM_PREFIX -> Settings() as normal
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SSM_PREFIX", raising=False)
        monkeypatch.setenv("BUFFER_API_KEY", "plain-key")
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "plain-role-key")

        # Act
        settings = build_settings()

        # Assert
        assert settings.buffer_api_key.get_secret_value() == "plain-key"
        assert settings.supabase_service_role_key.get_secret_value() == "plain-role-key"

    def test_resolves_secrets_from_ssm_when_prefix_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: Lambda usage — the two secrets come from SSM, never from
        # plain Lambda environment variables.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SSM_PREFIX", "/sizuru/")
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.delenv("BUFFER_API_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        fake_client = _FakeSsmClient(
            {
                "/sizuru/BUFFER_API_KEY": "ssm-buffer-key",
                "/sizuru/SUPABASE_SERVICE_ROLE_KEY": "ssm-role-key",
            }
        )
        monkeypatch.setattr(factory.boto3, "client", lambda service: fake_client)
        factory._ssm_cache.clear()

        # Act
        settings = build_settings()

        # Assert
        assert settings.buffer_api_key.get_secret_value() == "ssm-buffer-key"
        assert settings.supabase_service_role_key.get_secret_value() == "ssm-role-key"
        assert fake_client.calls == [
            "/sizuru/BUFFER_API_KEY",
            "/sizuru/SUPABASE_SERVICE_ROLE_KEY",
        ]

    def test_ssm_secret_is_cached_across_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setenv("SSM_PREFIX", "/sizuru-cache-test/")
        fake_client = _FakeSsmClient({"/sizuru-cache-test/X": "value"})
        monkeypatch.setattr(factory.boto3, "client", lambda service: fake_client)
        factory._ssm_cache.clear()

        # Act: same container reusing a warm Lambda would call this twice
        factory._resolve_ssm_secret("/sizuru-cache-test/", "X")
        factory._resolve_ssm_secret("/sizuru-cache-test/", "X")

        # Assert
        assert fake_client.calls == ["/sizuru-cache-test/X"]

    def test_ssm_secret_fetched_once_across_multiple_build_settings_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: a warm Lambda container calls build_settings() again on
        # every invocation — each parameter must still be fetched from SSM
        # at most once per container (see factory.py's module docstring).
        monkeypatch.setenv("SSM_PREFIX", "/sizuru-multi-build/")
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        fake_client = _FakeSsmClient(
            {
                "/sizuru-multi-build/BUFFER_API_KEY": "key",
                "/sizuru-multi-build/SUPABASE_SERVICE_ROLE_KEY": "role",
            }
        )
        monkeypatch.setattr(factory.boto3, "client", lambda service: fake_client)
        factory._ssm_cache.clear()

        # Act: two separate invocations reusing the same warm container
        build_settings()
        build_settings()

        # Assert: each name fetched exactly once, not twice
        assert fake_client.calls == [
            "/sizuru-multi-build/BUFFER_API_KEY",
            "/sizuru-multi-build/SUPABASE_SERVICE_ROLE_KEY",
        ]

    def test_ssm_client_error_propagates_clearly_without_a_secret_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: SSM itself errors (e.g. throttled, parameter deleted).
        # The parameter fetch never succeeded, so there is no secret value
        # to have leaked — the raw ClientError should propagate as-is
        # rather than being masked, and its message must not contain any
        # secret (there isn't one to contain).
        from botocore.exceptions import ClientError

        monkeypatch.setenv("SSM_PREFIX", "/sizuru-error-test/")
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")

        class _FailingSsmClient:
            def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, dict[str, str]]:
                raise ClientError(
                    {"Error": {"Code": "ParameterNotFound", "Message": "parameter not found"}},
                    "GetParameter",
                )

        monkeypatch.setattr(factory.boto3, "client", lambda service: _FailingSsmClient())
        factory._ssm_cache.clear()

        # Act / Assert
        with pytest.raises(ClientError) as exc_info:
            build_settings()
        assert "parameter not found" in str(exc_info.value)
        assert factory._ssm_cache == {}

    def test_reset_ssm_cache_forces_a_fresh_fetch_on_next_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: on BufferAuthError (a possibly-rotated-away secret), the
        # handlers clear this cache so the *next* invocation re-reads SSM
        # instead of reusing the stale value for the rest of the
        # container's lifetime.
        monkeypatch.setenv("SSM_PREFIX", "/sizuru-reset-test/")
        fake_client = _FakeSsmClient({"/sizuru-reset-test/X": "value"})
        monkeypatch.setattr(factory.boto3, "client", lambda service: fake_client)
        factory._ssm_cache.clear()
        factory._resolve_ssm_secret("/sizuru-reset-test/", "X")
        assert fake_client.calls == ["/sizuru-reset-test/X"]

        # Act
        reset_ssm_cache()
        factory._resolve_ssm_secret("/sizuru-reset-test/", "X")

        # Assert: fetched again, not served from the (now-cleared) cache
        assert fake_client.calls == ["/sizuru-reset-test/X", "/sizuru-reset-test/X"]
