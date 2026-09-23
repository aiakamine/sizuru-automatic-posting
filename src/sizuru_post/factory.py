"""Settings -> adapter/Publisher construction, shared by the CLI and the
API Lambda handler.

Kept separate from cli.py so the API handler doesn't pull in typer, and
from the adapters themselves so tests can monkeypatch a single call site
(`build_publisher`) without needing real AWS/Supabase credentials.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import boto3
from botocore.exceptions import ClientError

from sizuru_post.adapters.buffer import BufferClient
from sizuru_post.adapters.repo import SupabaseRepo
from sizuru_post.adapters.storage import S3ClientProtocol, S3Storage, StoredObject
from sizuru_post.config import Settings
from sizuru_post.publisher import Publisher
from supabase import create_client

_SSM_PREFIX_ENV = "SSM_PREFIX"
# In-process cache, keyed by full parameter name (prefix + name). Lives for
# the lifetime of the Lambda execution environment (container reuse across
# warm invocations), so a parameter is fetched from SSM at most once per
# container instead of on every request.
_ssm_cache: dict[str, str] = {}


def _resolve_ssm_parameter(prefix: str, name: str, *, required: bool) -> str | None:
    key = f"{prefix}{name}"
    if key in _ssm_cache:
        return _ssm_cache[key]
    client = boto3.client("ssm")
    try:
        response = client.get_parameter(Name=key, WithDecryption=True)
    except ClientError as exc:
        # Only an *optional* parameter (SCHEDULER_API_KEY: the scheduling
        # API may simply not be deployed in this environment) tolerates
        # "doesn't exist" — required parameters propagate the error as-is,
        # same as before. Not cached, so a parameter created later is
        # picked up without waiting for a cold start.
        if not required and exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise
    _ssm_cache[key] = str(response["Parameter"]["Value"])
    return _ssm_cache[key]


def _resolve_ssm_secret(prefix: str, name: str) -> str:
    value = _resolve_ssm_parameter(prefix, name, required=True)
    assert value is not None  # noqa: S101 - required=True guarantees this (or ClientError was raised)
    return value


def _resolve_optional_ssm_secret(prefix: str, name: str) -> str | None:
    return _resolve_ssm_parameter(prefix, name, required=False)


def reset_ssm_cache() -> None:
    """Clear the in-process SSM secret cache.

    Called by the Lambda handlers when Buffer rejects our API key
    (BufferAuthError): the secret may have been rotated in SSM, so the
    *next* invocation should re-fetch it fresh instead of continuing to use
    a stale value for the rest of this container's lifetime. Does not
    retry the request that triggered it.
    """
    _ssm_cache.clear()


def build_settings(*, include_scheduler_key: bool = True) -> Settings:
    """Construct Settings for any entry point (CLI or Lambda).

    BUFFER_API_KEY / SUPABASE_SERVICE_ROLE_KEY are resolved from SSM
    SecureString parameters (`{SSM_PREFIX}BUFFER_API_KEY` etc.) instead of
    plain environment variables when SSM_PREFIX is set — that's how the
    Lambdas get them, so the actual secret values never sit in plain
    Lambda environment variables. SUPABASE_URL and everything else stays a
    plain env var either way. Falls back to plain Settings() (env/.env)
    when SSM_PREFIX is unset, e.g. local CLI usage.

    SCHEDULER_API_KEY is resolved the same way but optionally (a missing
    parameter becomes `None` instead of raising, since not every deployment
    needs the scheduling API — see config.Settings.scheduler_api_key /
    api/schedule._require_api_key) — AND only when `include_scheduler_key`
    is True. Callers whose IAM role was never granted read access to that
    parameter at all (api/sync_handler.py's SyncFunction — only
    ApiFunction serves /schedule/* routes) must pass False: the missing
    grant would surface as an *unhandled* AccessDenied ClientError here
    (only "the parameter doesn't exist" is tolerated, not "denied"), which
    is exactly what took SyncFunction down in production. Skipping the
    call entirely is simpler and safer than trying to special-case that
    error code.
    """
    prefix = os.environ.get(_SSM_PREFIX_ENV)
    if not prefix:
        return Settings()
    return Settings(
        buffer_api_key=_resolve_ssm_secret(prefix, "BUFFER_API_KEY"),
        supabase_service_role_key=_resolve_ssm_secret(prefix, "SUPABASE_SERVICE_ROLE_KEY"),
        scheduler_api_key=(
            _resolve_optional_ssm_secret(prefix, "SCHEDULER_API_KEY") if include_scheduler_key else None
        ),
    )


def build_buffer_client(
    settings: Settings,
    *,
    timeout: float = 30.0,
    max_retries: int = 4,
    max_retry_sleep_seconds: float | None = None,
) -> BufferClient:
    return BufferClient(
        api_key=settings.buffer_api_key.get_secret_value(),
        timeout=timeout,
        max_retries=max_retries,
        max_retry_sleep_seconds=max_retry_sleep_seconds,
    )


def build_storage(settings: Settings) -> S3Storage:
    """Build a real S3Storage. Raises a clear error if S3_BUCKET isn't
    configured — only callers that actually upload (the CLI's `publish`)
    need this; the API Lambda uses `build_publisher_without_uploads`
    instead, which never calls this."""
    if settings.s3_bucket is None:
        raise ValueError("S3_BUCKET が設定されていません。動画のアップロードにはS3の設定が必要です。")
    session = boto3.Session(profile_name=settings.aws_profile) if settings.aws_profile else boto3.Session()
    # boto3-stubs types put_object's kwargs via Unpack[TypedDict], which mypy
    # treats as structurally distinct from our simple **kwargs: Any port;
    # the real client satisfies S3ClientProtocol at runtime, so cast here.
    client = cast(S3ClientProtocol, session.client("s3", region_name=settings.s3_region))
    return S3Storage(
        bucket=settings.s3_bucket,
        region=settings.s3_region,
        prefix=settings.s3_public_prefix,
        client=client,
    )


def build_repo(settings: Settings) -> SupabaseRepo:
    client = create_client(settings.supabase_url, settings.supabase_service_role_key.get_secret_value())
    return SupabaseRepo(client)


def build_publisher(settings: Settings) -> Publisher:
    """Full Publisher wired for uploads (the CLI's `validate`/`publish`/
    `retry` commands). Requires S3 settings."""
    return Publisher(
        buffer=build_buffer_client(settings),
        storage=build_storage(settings),
        repo=build_repo(settings),
    )


class _UnavailableStorage:
    """A StoragePort stand-in for contexts that never upload (the API
    Lambda only edits/cancels/syncs existing posts). Raises clearly if
    ever actually used, instead of silently no-op'ing or requiring S3
    configuration that context has no reason to need."""

    _MESSAGE = "この実行コンテキストでは動画アップロードは利用できません（S3未設定）"

    def upload_video(self, path: Path) -> StoredObject:
        raise RuntimeError(self._MESSAGE)

    def upload_thumbnail(self, path: Path) -> StoredObject:
        raise RuntimeError(self._MESSAGE)


def build_publisher_without_uploads(settings: Settings) -> Publisher:
    """Publisher wired for edit/cancel/sync only (no uploads) — used by the
    API/sync Lambdas, which never need S3 configured.

    Uses a short Buffer timeout, a single retry, and a capped retry sleep
    (vs the CLI's generous/unbounded defaults): a Lambda invocation has a
    hard wall-clock deadline, so a slow, erroring, or rate-limited Buffer
    response must not be allowed to consume it (see
    adapters.buffer.BufferClient._sleep_or_raise).
    """
    return Publisher(
        buffer=build_buffer_client(settings, timeout=8.0, max_retries=1, max_retry_sleep_seconds=5.0),
        storage=_UnavailableStorage(),
        repo=build_repo(settings),
    )
