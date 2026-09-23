"""Per-Lambda-container cache shared by api/handler.py's admin routes and
api/schedule.py's /schedule/* routes — both are dispatched from the same
ApiFunction Lambda (see infra/sam/template.yaml), so a single consolidated
cache means a key rotation (BUFFER_API_KEY, SUPABASE_SERVICE_ROLE_KEY,
SCHEDULER_API_KEY, ...) is picked up by BOTH route families together,
instead of whichever module happened to build its own Settings first.

Two independent staleness-handling mechanisms:
  - `reset()`: called on BufferAuthError (api/handler.py's `handler()`,
    which wraps both route families) — Buffer told us the key it just
    tried is wrong, so rebuild everything from a fresh SSM read before the
    *next* invocation.
  - Settings TTL (`_SETTINGS_TTL_SECONDS`): a *newly created*
    SCHEDULER_API_KEY, or a BUFFER_API_KEY rotated away without Buffer
    ever seeing the old value fail (e.g. it's simply never called again
    before rotation), would otherwise never trigger the above on a
    long-lived warm container. Settings are rebuilt at least this often
    regardless, so rotation takes effect without waiting for a cold start.

Rebuilding Settings (either way) also drops every cache derived from it
(Publisher, S3Storage, Repo, the JWKS client) — they'd otherwise keep
holding secrets baked in at construction time from the stale Settings.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import jwt

from sizuru_post.adapters.storage import S3Storage
from sizuru_post.api.auth import build_jwk_client
from sizuru_post.config import Settings
from sizuru_post.factory import (
    build_publisher_without_uploads,
    build_repo,
    build_settings,
    build_storage,
    reset_ssm_cache,
)
from sizuru_post.publisher import Publisher

if TYPE_CHECKING:
    from sizuru_post.adapters.repo import Repo

_SETTINGS_TTL_SECONDS = 300.0

# Overridable by tests (monkeypatch this name) instead of real wall-clock
# sleeps, to exercise TTL expiry deterministically.
_clock = time.monotonic

_settings: Settings | None = None
_settings_loaded_at: float | None = None
_publisher: Publisher | None = None
_storage: S3Storage | None = None
_repo: Repo | None = None
_jwk_client: jwt.PyJWKClient | None = None


def get_settings() -> Settings:
    """Build Settings lazily, rebuilding at least every
    `_SETTINGS_TTL_SECONDS` so a rotated/newly-created secret takes effect
    on a long-lived warm container (see this module's docstring).

    include_scheduler_key defaults to True here (unlike
    api/sync_handler.py's own get_settings): this container backs
    ApiFunction, which — unlike SyncFunction — actually serves
    /schedule/* routes and has the matching IAM grant for
    SCHEDULER_API_KEY (see infra/sam/template.yaml).
    """
    global _settings, _settings_loaded_at
    now = _clock()
    age = None if _settings_loaded_at is None else now - _settings_loaded_at
    is_stale = _settings is None or age is None or age > _SETTINGS_TTL_SECONDS
    if is_stale:
        reset_ssm_cache()  # otherwise build_settings() just re-reads the same cached SSM values
        _settings = build_settings(include_scheduler_key=True)
        _settings_loaded_at = now
        _invalidate_dependents()
    assert _settings is not None  # noqa: S101 - is_stale guarantees a build happened above if it was None
    return _settings


def get_publisher() -> Publisher:
    """Build the Publisher lazily, once per Settings generation (Lambda
    execution environment reuse avoids rebuilding it on every warm
    invocation). Used by both api/handler.py (edit/cancel/sync) and
    api/schedule.py (publish_uploaded/plan_with_video never touch
    StoragePort, so this no-uploads Publisher works for both).

    Calls get_settings() unconditionally (not just when `_publisher is
    None`) so a TTL-triggered invalidation is always observed here even if
    nothing else happened to call get_settings() first in this
    invocation — see get_settings()'s own `_invalidate_dependents()` call.
    """
    settings = get_settings()
    global _publisher
    if _publisher is None:
        _publisher = build_publisher_without_uploads(settings)
    return _publisher


def get_storage() -> S3Storage:
    """A real (uploading) S3Storage — unlike get_publisher()'s — for
    api/schedule.py's presign_post()/exists(), which aren't part of
    Publisher's StoragePort. See get_publisher()'s docstring for why
    get_settings() is called unconditionally here too."""
    settings = get_settings()
    global _storage
    if _storage is None:
        _storage = build_storage(settings)
    return _storage


def get_repo() -> Repo:
    """A standalone Repo — for api/schedule.py's GET /schedule/posts,
    which needs list_posts_in_range() directly rather than through
    Publisher. See get_publisher()'s docstring for why get_settings() is
    called unconditionally here too."""
    settings = get_settings()
    global _repo
    if _repo is None:
        _repo = build_repo(settings)
    return _repo


def get_jwk_client() -> jwt.PyJWKClient:
    """Build the JWKS resolver lazily, once per Settings generation — it
    caches keys in-process for its own lifespan (see
    api/auth.build_jwk_client), so reusing one instance across invocations
    avoids refetching the JWKS on every request. Used only by
    api/handler.py's admin routes (api/schedule.py has no JWT). See
    get_publisher()'s docstring for why get_settings() is called
    unconditionally here too."""
    settings = get_settings()
    global _jwk_client
    if _jwk_client is None:
        if not settings.supabase_jwt_issuer:
            raise RuntimeError("SUPABASE_JWT_ISSUER が設定されていません")
        _jwk_client = build_jwk_client(settings.supabase_jwt_issuer)
    return _jwk_client


def _invalidate_dependents() -> None:
    global _publisher, _storage, _repo, _jwk_client
    _publisher = None
    _storage = None
    _repo = None
    _jwk_client = None


def reset() -> None:
    """Drop every per-container cache — called on BufferAuthError (the key
    may have been rotated), so the *next* invocation of either route
    family rebuilds everything from a fresh SSM read. Does NOT retry the
    request that triggered this."""
    global _settings, _settings_loaded_at
    _settings = None
    _settings_loaded_at = None
    _invalidate_dependents()
    reset_ssm_cache()
