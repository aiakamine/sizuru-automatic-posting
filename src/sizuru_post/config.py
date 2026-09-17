"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the sizuru-post CLI.

    Missing required variables raise a pydantic ValidationError whose
    message names the offending environment variable, so failures are
    immediately actionable. Secret fields are typed as SecretStr (never
    rendered by repr()/str()) and `hide_input_in_errors` keeps raw input
    values out of validation error messages, so secrets can't leak into
    logs or CLI output through either path.

    Source priority is explicitly init kwargs > this repo's .env > OS
    environment > secrets file — NOT pydantic-settings' default of env
    before dotenv. Without this override, a value the user's shell happens
    to export (e.g. SUPABASE_URL for an unrelated project) silently wins
    over this repo's own .env, pointing the CLI at the wrong project.
    `env_ignore_empty` treats a present-but-empty variable (e.g.
    `BUFFER_ORGANIZATION_ID=` with nothing after it) the same as unset,
    rather than as the literal empty string.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        env_ignore_empty=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, dotenv_settings, env_settings, file_secret_settings)

    buffer_api_key: SecretStr = Field(validation_alias="BUFFER_API_KEY")
    buffer_organization_id: str | None = Field(default=None, validation_alias="BUFFER_ORGANIZATION_ID")

    # S3 is optional: only the CLI's `publish` command uploads a video, so
    # the API Lambda (edit/cancel/sync only) can construct Settings without
    # any S3_* vars. factory.build_storage() raises a clear error if a
    # caller that DOES need uploads finds this unset.
    aws_profile: str | None = Field(default=None, validation_alias="AWS_PROFILE")
    s3_bucket: str | None = Field(default=None, validation_alias="S3_BUCKET")
    s3_region: str = Field(default="ap-northeast-1", validation_alias="S3_REGION")
    s3_public_prefix: str = Field(default="public/", validation_alias="S3_PUBLIC_PREFIX")

    supabase_url: str = Field(validation_alias="SUPABASE_URL")
    supabase_service_role_key: SecretStr = Field(validation_alias="SUPABASE_SERVICE_ROLE_KEY")

    # Only used by the API Lambda (api/handler.py) to verify the admin
    # dashboard's Supabase-issued JWT itself: API Gateway HTTP API's native
    # JWT authorizer only supports RSA, but Supabase signs with ES256 (see
    # infra/sam/README.md). Optional since the CLI never sets these.
    supabase_jwt_issuer: str | None = Field(default=None, validation_alias="SUPABASE_JWT_ISSUER")
    supabase_jwt_audience: str | None = Field(default=None, validation_alias="SUPABASE_JWT_AUDIENCE")
