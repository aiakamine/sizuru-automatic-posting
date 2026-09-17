"""Tests for sizuru_post.config: env loading and clear errors on missing keys."""

from pathlib import Path

import pytest

from sizuru_post.config import Settings

REQUIRED_ENV = {
    "BUFFER_API_KEY": "buffer-key",
    "S3_BUCKET": "my-bucket",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-key",
}


def test_settings_loads_from_env_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BUFFER_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("S3_REGION", raising=False)
    monkeypatch.delenv("S3_PUBLIC_PREFIX", raising=False)

    # Act: _env_file=None keeps this hermetic regardless of a real local .env
    settings = Settings(_env_file=None)

    # Assert
    assert settings.buffer_api_key.get_secret_value() == "buffer-key"
    assert settings.buffer_organization_id is None
    assert settings.s3_bucket == "my-bucket"
    assert settings.s3_region == "ap-northeast-1"
    assert settings.s3_public_prefix == "public/"
    assert settings.supabase_url == "https://example.supabase.co"
    assert settings.supabase_service_role_key.get_secret_value() == "service-role-key"


def test_settings_secret_fields_do_not_leak_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    # Act
    settings = Settings(_env_file=None)

    # Assert: str()/repr() of a SecretStr never renders the underlying value
    assert "buffer-key" not in repr(settings)
    assert "service-role-key" not in repr(settings)


def test_settings_missing_required_key_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)

    # Act / Assert: _env_file=None keeps this hermetic regardless of a real local .env
    with pytest.raises(Exception, match="BUFFER_API_KEY"):
        Settings(_env_file=None)


def test_dotenv_takes_priority_over_os_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: the user's shell exports SUPABASE_URL for a DIFFERENT project
    # than this repo's own .env — this repo's .env must win, or the CLI
    # silently targets the wrong Supabase project.
    monkeypatch.chdir(tmp_path)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SUPABASE_URL", "https://os-env-project.supabase.co")
    (tmp_path / ".env").write_text("SUPABASE_URL=https://dotenv-project.supabase.co\n")

    # Act
    settings = Settings()

    # Assert
    assert settings.supabase_url == "https://dotenv-project.supabase.co"


def test_init_kwargs_take_priority_over_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.chdir(tmp_path)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    (tmp_path / ".env").write_text("SUPABASE_URL=https://dotenv-project.supabase.co\n")

    # Act
    settings = Settings(supabase_url="https://explicit-project.supabase.co")

    # Assert
    assert settings.supabase_url == "https://explicit-project.supabase.co"


def test_empty_buffer_organization_id_env_var_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: an empty (but present) BUFFER_ORGANIZATION_ID must not become
    # "" — Buffer rejects "" with "Invalid OrganizationId format" instead of
    # us correctly falling back to the first organization.
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BUFFER_ORGANIZATION_ID", "")

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.buffer_organization_id is None


def test_empty_buffer_organization_id_in_dotenv_becomes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.chdir(tmp_path)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BUFFER_ORGANIZATION_ID", raising=False)
    (tmp_path / ".env").write_text("BUFFER_ORGANIZATION_ID=\n")

    # Act
    settings = Settings()

    # Assert
    assert settings.buffer_organization_id is None


def test_s3_bucket_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: the API Lambda never uploads, so it must be able to construct
    # Settings without any S3_* vars configured at all.
    for key, value in REQUIRED_ENV.items():
        if key == "S3_BUCKET":
            continue
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("S3_BUCKET", raising=False)

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.s3_bucket is None


def test_supabase_jwt_issuer_and_audience_are_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: the CLI never sets these (only api/handler.py's in-Lambda JWT
    # verification needs them), so Settings must construct fine without them.
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SUPABASE_JWT_ISSUER", raising=False)
    monkeypatch.delenv("SUPABASE_JWT_AUDIENCE", raising=False)

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.supabase_jwt_issuer is None
    assert settings.supabase_jwt_audience is None


def test_supabase_jwt_issuer_and_audience_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: how the API Lambda actually gets them (see
    # infra/sam/template.yaml's ApiFunction Environment.Variables).
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", "https://example.supabase.co/auth/v1")
    monkeypatch.setenv("SUPABASE_JWT_AUDIENCE", "authenticated")

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.supabase_jwt_issuer == "https://example.supabase.co/auth/v1"
    assert settings.supabase_jwt_audience == "authenticated"
