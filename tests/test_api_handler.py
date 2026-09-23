"""Tests for sizuru_post.api.handler: API Gateway HTTP API (payload v2) Lambda.

Publisher/Repo are fakes throughout — no real AWS/Supabase/Buffer calls.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import sizuru_post.api._container as container_module
import sizuru_post.api.handler as handler_module
from sizuru_post.adapters.buffer import (
    BufferAmbiguousError,
    BufferAuthError,
    BufferHTTPError,
    BufferProtocolError,
    CreatePostResult,
)
from sizuru_post.adapters.repo import ChannelRow
from sizuru_post.adapters.storage import PresignedUpload
from sizuru_post.config import Settings
from sizuru_post.domain.models import PostStatus, Target, Timing, VideoInfo
from sizuru_post.publisher import Publisher
from tests.fakes import FakeBuffer, FakeStorage, InMemoryRepo

ADMIN_USER_ID = "admin-user-id"
VALID_VIDEO = VideoInfo(duration_sec=10.0, width=1080, height=1920, video_codec="h264")
FIXED_NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)

# --- JWT test fixtures: the admin dashboard's tokens are ES256-signed
# (Supabase Auth), which API Gateway's native JWT authorizer can't verify
# (RSA-only) — so the Lambda verifies them itself (see api/auth.py). These
# tests inject a fake JWKS resolver instead of hitting a real endpoint.
JWT_ISSUER = "https://hdqrvhnhlyykocphetfg.supabase.co/auth/v1"
JWT_AUDIENCE = "authenticated"

_EC_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_EC_PRIVATE_PEM = _EC_PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
_EC_PUBLIC_PEM = (
    _EC_PRIVATE_KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)


class _FakeSigningKey:
    def __init__(self, key: str) -> None:
        self.key = key


class _FakeJwkClient:
    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(_EC_PUBLIC_PEM)


def _make_bearer_token(sub: str, **extra_claims: Any) -> str:
    payload: dict[str, Any] = {
        "sub": sub,
        "aud": JWT_AUDIENCE,
        "iss": JWT_ISSUER,
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    payload.update(extra_claims)
    return jwt.encode(payload, _EC_PRIVATE_PEM, algorithm="ES256")


_UNSET = object()

SCHEDULER_API_KEY_VALUE = "test-scheduler-key"


def _channel(service: str) -> ChannelRow:
    return ChannelRow(
        id=uuid4(),
        buffer_channel_id=f"buf-{service}",
        service=service,  # type: ignore[arg-type]
        name=service,
        display_name=service,
        is_disconnected=False,
        is_queue_paused=False,
    )


@pytest.fixture
def repo() -> InMemoryRepo:
    # All 3 services + the admin user: api/handler.py's admin routes and
    # api/schedule.py's /schedule/* routes are dispatched from the same
    # consolidated container cache (see api/_container.py), so in
    # production they share one real Repo/Publisher/Settings too — this
    # one repo/publisher/settings set backs both here.
    return InMemoryRepo(
        channels=[_channel("x"), _channel("tiktok"), _channel("instagram")],
        admin_user_ids={ADMIN_USER_ID},
    )


@pytest.fixture
def buffer() -> FakeBuffer:
    return FakeBuffer()


@pytest.fixture
def publisher(buffer: FakeBuffer, repo: InMemoryRepo) -> Publisher:
    return Publisher(
        buffer=buffer,
        storage=FakeStorage(),
        repo=repo,
        probe=lambda path: VALID_VIDEO,  # type: ignore[arg-type]
        clock=lambda: FIXED_NOW,
    )


@pytest.fixture
def settings() -> Settings:
    """One Settings satisfying both api/handler.py's admin routes
    (supabase_jwt_issuer/audience) and api/schedule.py's /schedule/*
    routes (scheduler_api_key) — see api/_container.py's docstring."""
    return Settings(
        _env_file=None,
        buffer_api_key="k",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="k",
        supabase_jwt_issuer=JWT_ISSUER,
        supabase_jwt_audience=JWT_AUDIENCE,
        scheduler_api_key=SCHEDULER_API_KEY_VALUE,
    )


class _FakeScheduleStorage:
    """Fake for the S3Storage methods api/schedule.py calls directly
    (presign_post/head/public_url_for) — distinct from the FakeStorage
    Protocol used by Publisher (upload_video/upload_thumbnail)."""

    def __init__(self, existing_keys: set[str] | None = None) -> None:
        self.existing_keys = existing_keys or set()
        self.presign_calls: list[dict[str, Any]] = []

    def presign_post(
        self, *, subdir: str, extension: str, content_type: str, max_bytes: int, expires_in: int
    ) -> PresignedUpload:
        self.presign_calls.append(
            {
                "subdir": subdir,
                "extension": extension,
                "content_type": content_type,
                "max_bytes": max_bytes,
                "expires_in": expires_in,
            }
        )
        key = f"public/{subdir}/2030/01/{'a' * 32}{extension}"
        return PresignedUpload(
            key=key,
            public_url=self.public_url_for(key),
            upload_url="https://my-bucket.s3.amazonaws.com/",
            fields={"key": key, "Content-Type": content_type, "policy": "fake-policy"},
        )

    def exists(self, key: str) -> bool:
        return key in self.existing_keys

    def public_url_for(self, key: str) -> str:
        return f"https://cdn.example.com/{key}"


def _schedule_future(days: int = 1) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


VALID_SCHEDULE_VIDEO_KEY = f"public/videos/2030/01/{'a' * 32}.mp4"
VALID_SCHEDULE_THUMBNAIL_KEY = f"public/thumbnails/2030/01/{'b' * 32}.jpg"


@pytest.fixture
def schedule_storage() -> _FakeScheduleStorage:
    return _FakeScheduleStorage(existing_keys={VALID_SCHEDULE_VIDEO_KEY, VALID_SCHEDULE_THUMBNAIL_KEY})


@pytest.fixture(autouse=True)
def patch_container(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    publisher: Publisher,
    schedule_storage: _FakeScheduleStorage,
    repo: InMemoryRepo,
) -> None:
    """api/handler.py and api/schedule.py both read through
    api/_container.py's single shared cache (see that module's
    docstring) — patching it once here covers both route families."""
    monkeypatch.setattr(container_module, "get_settings", lambda: settings)
    monkeypatch.setattr(container_module, "get_publisher", lambda: publisher)
    monkeypatch.setattr(container_module, "get_storage", lambda: schedule_storage)
    monkeypatch.setattr(container_module, "get_repo", lambda: repo)
    monkeypatch.setattr(container_module, "get_jwk_client", lambda: _FakeJwkClient())


# --- back-compat aliases: the schedule-route tests below were written
# against these names before api/handler.py and api/schedule.py shared one
# container cache; kept so those tests don't need touching one-by-one.
@pytest.fixture
def schedule_settings(settings: Settings) -> Settings:
    return settings


@pytest.fixture
def schedule_repo(repo: InMemoryRepo) -> InMemoryRepo:
    return repo


@pytest.fixture
def schedule_buffer(buffer: FakeBuffer) -> FakeBuffer:
    return buffer


@pytest.fixture
def schedule_publisher(publisher: Publisher) -> Publisher:
    return publisher


def _scheduled_post_id(publisher: Publisher, repo: InMemoryRepo, **target_overrides: Any) -> str:
    from sizuru_post.domain.models import PublishRequest

    defaults: dict[str, Any] = {"service": "x", "caption": "hello"}
    defaults.update(target_overrides)
    request = PublishRequest(
        video_path="clip.mp4",  # type: ignore[arg-type]
        timing=Timing(mode="addToQueue"),
        targets=[Target(**defaults)],  # type: ignore[arg-type]
    )
    report = publisher.publish(request, approved=True)
    return str(next(iter(repo.list_posts_by_submission(report.submission_id))).id)  # type: ignore[arg-type]


def _event(
    method: str,
    route_key: str,
    *,
    path_params: dict[str, str] | None = None,
    body: dict[str, Any] | str | None = None,
    user_id: str | None = ADMIN_USER_ID,
    authorization: str | None | object = _UNSET,
    api_key: str | None = None,
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": "/" + route_key.split(" ", 1)[1].lstrip("/"),
        "requestContext": {"http": {"method": method}},
        "pathParameters": path_params or {},
        "headers": {},
    }
    # `authorization` lets a test send a specific/malformed header value;
    # otherwise a real, valid token is generated for `user_id` (or the
    # header is omitted entirely if user_id=None — "no token supplied").
    if authorization is not _UNSET:
        if authorization is not None:
            event["headers"]["authorization"] = authorization
    elif user_id is not None:
        event["headers"]["authorization"] = f"Bearer {_make_bearer_token(user_id)}"
    if api_key is not None:
        event["headers"]["x-api-key"] = api_key
    if query_params is not None:
        event["queryStringParameters"] = query_params
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


class TestAuth:
    def test_missing_authorization_header_returns_401(self) -> None:
        # Arrange
        event = _event("POST", "POST /sync", user_id=None)

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        body = json.loads(response["body"])
        assert body["error"]["code"] == "unauthorized"

    def test_non_admin_user_returns_403(self) -> None:
        # Arrange
        event = _event("POST", "POST /sync", user_id="not-an-admin")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"]["code"] == "forbidden"

    def test_expired_token_returns_401(self) -> None:
        # Arrange: end-to-end proof that handler() actually verifies the
        # JWT (not just checking presence) — see tests/test_auth.py for
        # exhaustive verify_bearer_token edge cases (wrong aud/iss, bad
        # signature, alg confusion, ...).
        token = jwt.encode(
            {
                "sub": ADMIN_USER_ID,
                "aud": JWT_AUDIENCE,
                "iss": JWT_ISSUER,
                "exp": datetime.now(UTC) - timedelta(hours=1),
            },
            _EC_PRIVATE_PEM,
            algorithm="ES256",
        )
        event = _event("POST", "POST /sync", authorization=f"Bearer {token}")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"

    def test_malformed_authorization_header_returns_401(self) -> None:
        # Arrange: no "Bearer " prefix
        event = _event("POST", "POST /sync", authorization="not-a-bearer-token")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"

    def test_capitalized_authorization_header_name_is_still_recognized(self) -> None:
        # Arrange: HTTP API payload v2 always lowercases header names, but
        # _get_authorization_header must stay defensive (proxying,
        # hand-built events, ...) and match case-insensitively.
        event = _event("POST", "POST /sync", authorization=None)
        event["headers"]["Authorization"] = f"Bearer {_make_bearer_token(ADMIN_USER_ID)}"

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200

    def test_token_with_surrounding_whitespace_is_accepted(self) -> None:
        # Arrange: `.strip()` after slicing off "Bearer " must tolerate
        # stray leading/trailing whitespace around the token itself.
        token = _make_bearer_token(ADMIN_USER_ID)
        event = _event("POST", "POST /sync", authorization=f"Bearer   {token}  ")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200

    def test_lowercase_bearer_scheme_is_rejected(self) -> None:
        # Arrange: the "Bearer " prefix check is case-sensitive by design
        # (matches RFC 6750's exact casing) — "bearer" must not pass.
        token = _make_bearer_token(ADMIN_USER_ID)
        event = _event("POST", "POST /sync", authorization=f"bearer {token}")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"


class TestRouting:
    def test_unknown_route_returns_404(self) -> None:
        # Arrange
        event = _event("GET", "GET /nope")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 404
        assert json.loads(response["body"])["error"]["code"] == "not_found"

    @pytest.mark.parametrize(
        "route_key",
        [
            "GET /posts/{post_id}",  # right path, wrong method
            "DELETE /posts/{post_id}",
            "POST /posts/{post_id}",  # missing /cancel
            "GET /sync",  # right path, wrong method
        ],
    )
    def test_unmapped_method_for_a_known_path_returns_404(self, route_key: str) -> None:
        # Arrange: routeKey is matched as a whole (method + path); a method
        # that isn't wired for an otherwise-valid path must 404, not be
        # dispatched to some other handler.
        event = _event(route_key.split(" ", 1)[0], route_key, path_params={"post_id": str(uuid4())})

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 404
        assert json.loads(response["body"])["error"]["code"] == "not_found"

    def test_cancel_malformed_post_id_returns_404(self) -> None:
        # Arrange: /posts/{post_id}/cancel must validate post_id the same
        # way PATCH /posts/{post_id} does.
        event = _event("POST", "POST /posts/{post_id}/cancel", path_params={"post_id": "not-a-uuid"})

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 404


class TestAllResponsesAreSafeJson:
    """Every response, success or error, must be uniformly-shaped JSON and
    must never echo internal details back to the client."""

    _PLANTED_SECRET = "sw0rdf1sh-service-role-key-do-not-leak"

    def test_every_response_has_json_content_type(self, publisher: Publisher) -> None:
        # Arrange / Act
        responses = [
            handler_module.handler(_event("POST", "POST /sync")),
            handler_module.handler(_event("GET", "GET /nope")),
            handler_module.handler(_event("POST", "POST /sync", user_id=None)),
        ]

        # Assert
        for response in responses:
            assert response["headers"]["Content-Type"] == "application/json"
            json.loads(response["body"])  # must parse as JSON

    def test_unexpected_exception_body_never_contains_a_planted_secret(
        self, publisher: Publisher, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        def _boom(**kwargs: Any) -> Any:
            raise RuntimeError(self._PLANTED_SECRET)

        monkeypatch.setattr(publisher, "sync", _boom)

        # Act
        response = handler_module.handler(_event("POST", "POST /sync"))

        # Assert
        assert response["statusCode"] == 500
        assert self._PLANTED_SECRET not in response["body"]

    def test_admin_check_raising_returns_500_not_403_and_never_leaks_details(
        self, publisher: Publisher, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: is_admin() itself blowing up (e.g. a DB error) is our own
        # bug, not evidence the caller lacks permission — must not be
        # reported as a (misleading) 403, and must not leak the exception.
        def _boom(user_id: str) -> bool:
            raise RuntimeError(self._PLANTED_SECRET)

        monkeypatch.setattr(publisher._repo, "is_admin", _boom)  # noqa: SLF001

        # Act
        response = handler_module.handler(_event("POST", "POST /sync"))

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["error"]["code"] == "internal"
        assert self._PLANTED_SECRET not in response["body"]


class TestSync:
    def test_sync_returns_200_with_sync_key(self) -> None:
        # Act
        response = handler_module.handler(_event("POST", "POST /sync"))

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "sync" in body
        assert body["sync"]["sent"] == 0

    def test_sync_buffer_auth_error_returns_502(
        self, monkeypatch: pytest.MonkeyPatch, publisher: Publisher
    ) -> None:
        # Arrange
        def _raise_auth_error(**kwargs: Any) -> dict[str, int]:
            raise BufferAuthError("bad key")

        monkeypatch.setattr(publisher, "sync", _raise_auth_error)

        # Act
        response = handler_module.handler(_event("POST", "POST /sync"))

        # Assert
        assert response["statusCode"] == 502
        assert json.loads(response["body"])["error"]["code"] == "upstream"

    def test_sync_buffer_auth_error_resets_cached_publisher(
        self, monkeypatch: pytest.MonkeyPatch, publisher: Publisher
    ) -> None:
        # Arrange: the secret may have been rotated away — the *next*
        # invocation must rebuild the Publisher (re-reading SSM) rather than
        # keep using a possibly-stale one for the container's lifetime.
        # container.get_publisher() itself is monkeypatched away by
        # patch_container, so pre-seed the raw cache directly (matching
        # what a real warm container would have) to prove reset() clears
        # it — see api/_container.py's reset().
        def _raise_auth_error(**kwargs: Any) -> dict[str, int]:
            raise BufferAuthError("bad key")

        monkeypatch.setattr(publisher, "sync", _raise_auth_error)
        container_module._publisher = publisher
        reset_calls: list[bool] = []
        monkeypatch.setattr(container_module, "reset_ssm_cache", lambda: reset_calls.append(True))

        # Act
        handler_module.handler(_event("POST", "POST /sync"))

        # Assert
        assert container_module._publisher is None
        assert reset_calls == [True]

    def test_sync_full_true_reconciles_regardless_of_due_at(self, publisher: Publisher) -> None:
        # Arrange: the manual /sync endpoint (dashboard button) must
        # reconcile everything, unlike the scheduler's frequent full=False.
        calls: list[dict[str, Any]] = []

        def _spy_sync(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"sent": 0, "failed": 0, "canceled": 0, "unchanged": 0, "stuck_pending": 0}

        publisher.sync = _spy_sync  # type: ignore[method-assign]

        # Act
        response = handler_module.handler(_event("POST", "POST /sync"))

        # Assert
        assert response["statusCode"] == 200
        assert calls[0]["full"] is True


class TestEdit:
    def test_edit_success_returns_200_with_cli_shaped_result(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        event = _event(
            "PATCH",
            "PATCH /posts/{post_id}",
            path_params={"post_id": post_id},
            body={"caption": "new caption"},
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        result = body["result"]
        assert set(result.keys()) == {
            "service",
            "status",
            "post_id",
            "buffer_post_id",
            "due_at",
            "error",
            "issues",
        }
        assert result["post_id"] == post_id
        assert result["status"] == "scheduled"

    def test_edit_with_due_at_updates_schedule(self, publisher: Publisher, repo: InMemoryRepo) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        new_due_at = (FIXED_NOW + timedelta(hours=2)).isoformat()
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"due_at": new_due_at}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200

    def test_edit_invalid_json_body_returns_400(self, publisher: Publisher, repo: InMemoryRepo) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body="{not valid json"
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "bad_request"

    def test_edit_naive_due_at_returns_400_validation(self, publisher: Publisher, repo: InMemoryRepo) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        event = _event(
            "PATCH",
            "PATCH /posts/{post_id}",
            path_params={"post_id": post_id},
            body={"due_at": "2030-01-01T09:00:00"},
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_edit_json_array_body_returns_400_bad_request(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange: valid JSON, but not an object — _parse_body requires a dict.
        post_id = _scheduled_post_id(publisher, repo)
        event = _event("PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body="[1, 2, 3]")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "bad_request"

    def test_edit_empty_string_body_returns_400_bad_request(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange: an empty body isn't valid JSON (unlike a missing body,
        # which _parse_body treats as {}).
        post_id = _scheduled_post_id(publisher, repo)
        event = _event("PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body="")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "bad_request"

    def test_edit_missing_body_is_treated_as_empty_object_and_succeeds(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange: no body at all (caption=None, due_at=None) is a valid
        # no-op edit request, distinct from an empty-string body.
        post_id = _scheduled_post_id(publisher, repo)
        event = _event("PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id})

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200

    def test_edit_unknown_extra_fields_in_body_are_silently_ignored(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange: only caption/due_at are read; anything else is ignored
        # rather than rejected.
        post_id = _scheduled_post_id(publisher, repo)
        event = _event(
            "PATCH",
            "PATCH /posts/{post_id}",
            path_params={"post_id": post_id},
            body={"caption": "hello", "unexpected_field": "value", "another": 123},
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200

    def test_edit_caption_as_integer_returns_400_validation(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange: caption must be a string, not merely JSON-serializable.
        post_id = _scheduled_post_id(publisher, repo)
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"caption": 12345}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_edit_unparsable_due_at_returns_400_validation(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange: a syntactically-string-but-not-ISO8601 due_at.
        post_id = _scheduled_post_id(publisher, repo)
        event = _event(
            "PATCH",
            "PATCH /posts/{post_id}",
            path_params={"post_id": post_id},
            body={"due_at": "not-a-date"},
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_edit_base64_encoded_body_is_decoded_and_processed(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange: API Gateway HTTP API (payload v2) base64-encodes the body
        # and sets isBase64Encoded=True in some cases (e.g. certain
        # proxy/CDN paths or non-UTF8-safe content negotiation).
        import base64

        post_id = _scheduled_post_id(publisher, repo)
        raw_body = json.dumps({"caption": "hello from base64"})
        event = _event(
            "PATCH",
            "PATCH /posts/{post_id}",
            path_params={"post_id": post_id},
            body=base64.b64encode(raw_body.encode()).decode(),
        )
        event["isBase64Encoded"] = True

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200

    def test_edit_invalid_base64_body_returns_400_bad_request(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        event = _event(
            "PATCH",
            "PATCH /posts/{post_id}",
            path_params={"post_id": post_id},
            body="not-valid-base64!!!",
        )
        event["isBase64Encoded"] = True

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "bad_request"

    def test_edit_caption_too_long_returns_400_validation(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        event = _event(
            "PATCH",
            "PATCH /posts/{post_id}",
            path_params={"post_id": post_id},
            body={"caption": "あ" * 141},
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_edit_unknown_post_returns_404(self) -> None:
        # Arrange
        event = _event(
            "PATCH",
            "PATCH /posts/{post_id}",
            path_params={"post_id": str(uuid4())},
            body={"caption": "hi"},
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 404
        assert json.loads(response["body"])["error"]["code"] == "not_found"

    def test_edit_malformed_post_id_returns_404(self) -> None:
        # Arrange
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": "not-a-uuid"}, body={"caption": "hi"}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 404

    def test_edit_non_editable_status_returns_400_invalid_state(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        from uuid import UUID

        from sizuru_post.domain.models import PostStatus

        repo.update_post(UUID(post_id), status=PostStatus.CANCELED)
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"caption": "hi"}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "invalid_state"

    def test_edit_buffer_rejected_returns_400_invalid_state(
        self, publisher: Publisher, repo: InMemoryRepo, buffer: FakeBuffer
    ) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        buffer.edit_results["post-x"] = CreatePostResult(ok=False, error="already sent")
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"caption": "hi"}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "invalid_state"

    def test_edit_ambiguous_buffer_outcome_returns_409(
        self, publisher: Publisher, repo: InMemoryRepo, buffer: FakeBuffer
    ) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        buffer.edit_exceptions["post-x"] = BufferAmbiguousError("boom")
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"caption": "hi"}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 409
        assert json.loads(response["body"])["error"]["code"] == "ambiguous"

    def test_edit_protocol_error_outcome_returns_409(
        self, publisher: Publisher, repo: InMemoryRepo, buffer: FakeBuffer
    ) -> None:
        # Arrange: a malformed/undecodable Buffer "success" response after
        # the mutation was dispatched is just as ambiguous as
        # BufferAmbiguousError.
        post_id = _scheduled_post_id(publisher, repo)
        buffer.edit_exceptions["post-x"] = BufferProtocolError("boom")
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"caption": "hi"}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 409
        assert json.loads(response["body"])["error"]["code"] == "ambiguous"

    def test_edit_db_write_failed_returns_502_with_dedicated_code(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange: Buffer already applied the edit; only our own DB write
        # failed. The client must know to re-sync, not assume it failed
        # outright, and must NOT get a 200.
        post_id = _scheduled_post_id(publisher, repo)
        from sizuru_post.publisher import TargetResult

        monkeypatched_result = TargetResult(
            service="x", status="db_write_failed", post_id=None, buffer_post_id="post-x"
        )
        publisher.edit = lambda *args, **kwargs: monkeypatched_result  # type: ignore[method-assign]
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"caption": "hi"}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 502
        body = json.loads(response["body"])
        assert body["error"]["code"] == "db_write_failed"
        assert "更新ボタン" in body["error"]["message"]

    def test_edit_other_buffer_error_returns_502(
        self, publisher: Publisher, repo: InMemoryRepo, buffer: FakeBuffer
    ) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        buffer.edit_exceptions["post-x"] = BufferHTTPError("boom")
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"caption": "hi"}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 502
        assert json.loads(response["body"])["error"]["code"] == "upstream"

    def test_edit_unexpected_exception_returns_500_and_never_leaks_details(
        self, publisher: Publisher, repo: InMemoryRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: a bare RuntimeError is our own bug, not an upstream
        # (Buffer) problem — must map to 500 "internal", not 502.
        post_id = _scheduled_post_id(publisher, repo)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("supersecret-internal-detail")

        monkeypatch.setattr(publisher, "edit", _boom)
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"caption": "hi"}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 500
        assert json.loads(response["body"])["error"]["code"] == "internal"
        assert "supersecret-internal-detail" not in response["body"]

    def test_edit_unexpected_value_error_returns_500_internal_and_never_leaks_details(
        self, publisher: Publisher, repo: InMemoryRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: an unexpected ValueError (pydantic/config/repo model
        # validation, ...) is NOT the caller's fault — only our own
        # RequestValidationError (and the other specific exceptions above)
        # should map to 400. Everything else must be a generic 500.
        post_id = _scheduled_post_id(publisher, repo)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("supersecret-internal-detail")

        monkeypatch.setattr(publisher, "edit", _boom)
        event = _event(
            "PATCH", "PATCH /posts/{post_id}", path_params={"post_id": post_id}, body={"caption": "hi"}
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["error"]["code"] == "internal"
        assert "supersecret-internal-detail" not in response["body"]


class TestCancel:
    def test_cancel_success_returns_200(self, publisher: Publisher, repo: InMemoryRepo) -> None:
        # Arrange
        post_id = _scheduled_post_id(publisher, repo)
        event = _event("POST", "POST /posts/{post_id}/cancel", path_params={"post_id": post_id})

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200
        result = json.loads(response["body"])["result"]
        assert result["status"] == "canceled"
        assert result["post_id"] == post_id

    def test_cancel_unknown_post_returns_404(self) -> None:
        # Arrange
        event = _event("POST", "POST /posts/{post_id}/cancel", path_params={"post_id": str(uuid4())})

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 404

    def test_cancel_db_write_failed_returns_502_with_dedicated_code(
        self, publisher: Publisher, repo: InMemoryRepo
    ) -> None:
        # Arrange: Buffer already canceled the post; only our own DB write
        # failed.
        post_id = _scheduled_post_id(publisher, repo)
        from sizuru_post.publisher import TargetResult

        monkeypatched_result = TargetResult(
            service="x", status="db_write_failed", post_id=None, buffer_post_id="post-x"
        )
        publisher.cancel = lambda *args, **kwargs: monkeypatched_result  # type: ignore[method-assign]
        event = _event("POST", "POST /posts/{post_id}/cancel", path_params={"post_id": post_id})

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 502
        body = json.loads(response["body"])
        assert body["error"]["code"] == "db_write_failed"
        assert "更新ボタン" in body["error"]["message"]


class _FakeLambdaContext:
    """See api/_deadline.make_should_stop: only needs
    get_remaining_time_in_millis()."""

    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


class TestScheduleAuth:
    """`/schedule/*` uses a completely separate x-api-key scheme — never
    the admin dashboard's JWT, and vice versa (see api/handler.py's and
    api/schedule.py's docstrings)."""

    def test_missing_api_key_returns_401(self) -> None:
        # Arrange
        event = _event("POST", "POST /schedule/uploads", user_id=None)

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"

    def test_wrong_api_key_returns_401(self) -> None:
        # Arrange
        event = _event("POST", "POST /schedule/uploads", user_id=None, api_key="wrong-key")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"

    def test_non_ascii_api_key_returns_401_not_500(self) -> None:
        # Arrange: hmac.compare_digest(str, str) raises TypeError on
        # non-ASCII input — a wrong key with non-ASCII characters must
        # still be a clean 401, never an unhandled 500.
        event = _event("POST", "POST /schedule/uploads", user_id=None, api_key="日本語キー🔑")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"

    def test_correct_api_key_succeeds(self) -> None:
        # Arrange
        event = _event("POST", "POST /schedule/uploads", user_id=None, api_key=SCHEDULER_API_KEY_VALUE)

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200

    def test_api_key_header_name_is_case_insensitive(self) -> None:
        # Arrange
        event = _event("POST", "POST /schedule/uploads", user_id=None)
        event["headers"]["X-API-Key"] = SCHEDULER_API_KEY_VALUE

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200

    def test_scheduler_disabled_returns_503_when_key_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        disabled_settings = Settings(
            _env_file=None,
            buffer_api_key="k",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="k",
        )
        monkeypatch.setattr(container_module, "get_settings", lambda: disabled_settings)
        event = _event("POST", "POST /schedule/uploads", user_id=None, api_key=SCHEDULER_API_KEY_VALUE)

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 503
        assert json.loads(response["body"])["error"]["code"] == "scheduler_disabled"

    def test_valid_admin_jwt_does_not_authenticate_schedule_routes(self) -> None:
        # Arrange: a real, valid admin Bearer token, but no x-api-key.
        event = _event("POST", "POST /schedule/uploads")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"

    def test_scheduler_api_key_does_not_authenticate_admin_routes(self) -> None:
        # Arrange: a valid x-api-key, but no Authorization header.
        event = _event("POST", "POST /sync", user_id=None, api_key=SCHEDULER_API_KEY_VALUE)

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"

    def test_empty_string_api_key_returns_401(self) -> None:
        # Arrange: header present but empty — distinct from omitted entirely
        # (get_header would return None for that); this must still be a
        # clean 401 via hmac.compare_digest, not a crash.
        event = _event("POST", "POST /schedule/uploads", user_id=None, api_key="")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"

    def test_whitespace_only_api_key_returns_401(self) -> None:
        # Arrange
        event = _event("POST", "POST /schedule/uploads", user_id=None, api_key="   ")

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthorized"

    def test_empty_string_configured_key_is_treated_as_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: an empty-but-not-None SCHEDULER_API_KEY is a misconfigured
        # operator mistake — must be treated the same as "not configured"
        # (503), NOT as a secret that an equally-empty provided key can
        # match via hmac.compare_digest(b"", b"") == True.
        empty_key_settings = Settings(
            _env_file=None,
            buffer_api_key="k",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="k",
            scheduler_api_key="",
        )
        monkeypatch.setattr(container_module, "get_settings", lambda: empty_key_settings)

        # Act
        wrong = handler_module.handler(
            _event("POST", "POST /schedule/uploads", user_id=None, api_key="anything")
        )
        empty = handler_module.handler(_event("POST", "POST /schedule/uploads", user_id=None, api_key=""))

        # Assert
        assert wrong["statusCode"] == 503
        assert json.loads(wrong["body"])["error"]["code"] == "scheduler_disabled"
        assert empty["statusCode"] == 503
        assert json.loads(empty["body"])["error"]["code"] == "scheduler_disabled"

    def test_whitespace_only_configured_key_is_treated_as_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: same misconfiguration as the empty-string case above.
        whitespace_key_settings = Settings(
            _env_file=None,
            buffer_api_key="k",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="k",
            scheduler_api_key="   ",
        )
        monkeypatch.setattr(container_module, "get_settings", lambda: whitespace_key_settings)

        # Act
        response = handler_module.handler(
            _event("POST", "POST /schedule/uploads", user_id=None, api_key="anything")
        )

        # Assert
        assert response["statusCode"] == 503
        assert json.loads(response["body"])["error"]["code"] == "scheduler_disabled"


class TestScheduleUploads:
    def test_returns_presigned_video_and_thumbnail_with_expected_shape(self) -> None:
        # Arrange
        event = _event("POST", "POST /schedule/uploads", user_id=None, api_key=SCHEDULER_API_KEY_VALUE)

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["expires_in"] == 900
        assert body["max_bytes"] == 314_572_800
        assert body["thumbnail_max_bytes"] == 5_242_880
        for section in ("video", "thumbnail"):
            assert set(body[section].keys()) == {"key", "public_url", "upload"}
            assert set(body[section]["upload"].keys()) == {"url", "fields"}

    def test_video_presign_uses_mp4_and_thumbnail_uses_jpeg_with_a_smaller_size_cap(
        self, schedule_storage: _FakeScheduleStorage
    ) -> None:
        # Arrange
        event = _event("POST", "POST /schedule/uploads", user_id=None, api_key=SCHEDULER_API_KEY_VALUE)

        # Act
        handler_module.handler(event)

        # Assert
        calls_by_subdir = {call["subdir"]: call for call in schedule_storage.presign_calls}
        assert calls_by_subdir["videos"]["content_type"] == "video/mp4"
        assert calls_by_subdir["videos"]["extension"] == ".mp4"
        assert calls_by_subdir["videos"]["max_bytes"] == 314_572_800
        assert calls_by_subdir["videos"]["expires_in"] == 900
        assert calls_by_subdir["thumbnails"]["content_type"] == "image/jpeg"
        assert calls_by_subdir["thumbnails"]["extension"] == ".jpg"
        # Thumbnails are a single JPEG frame, not a 300MB video — a much
        # smaller cap here limits abuse of the presigned upload.
        assert calls_by_subdir["thumbnails"]["max_bytes"] == 5_242_880


class TestSchedulePosts:
    def _get(self, **query_params: str) -> dict[str, Any]:
        event = _event(
            "GET",
            "GET /schedule/posts",
            user_id=None,
            api_key=SCHEDULER_API_KEY_VALUE,
            query_params=query_params,
        )
        return handler_module.handler(event)  # type: ignore[no-any-return]

    def test_missing_from_returns_400_validation(self) -> None:
        # Act
        response = self._get(to=_schedule_future())

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_missing_to_returns_400_validation(self) -> None:
        # Act
        response = self._get(**{"from": _schedule_future()})

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_from_without_timezone_returns_400_validation(self) -> None:
        # Act
        response = self._get(**{"from": "2035-01-01T00:00:00", "to": _schedule_future(2)})

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_to_before_from_returns_400_validation(self) -> None:
        # Act
        response = self._get(**{"from": _schedule_future(5), "to": _schedule_future(1)})

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_to_equal_to_from_returns_400_validation(self) -> None:
        # Arrange: the rule is `end <= start`, not `end < start` — an empty
        # (zero-width) range must also be rejected.
        same = _schedule_future()

        # Act
        response = self._get(**{"from": same, "to": same})

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_range_over_62_days_returns_400_validation(self) -> None:
        # Arrange
        start = datetime.now(UTC) + timedelta(days=1)
        end = start + timedelta(days=63)

        # Act
        response = self._get(**{"from": start.isoformat(), "to": end.isoformat()})

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_range_of_exactly_62_days_is_accepted(self) -> None:
        # Arrange: the rule is `> _MAX_RANGE_DAYS`, so exactly 62 days is
        # the largest still-valid range, not an off-by-one rejection.
        start = datetime.now(UTC) + timedelta(days=1)
        end = start + timedelta(days=62)

        # Act
        response = self._get(**{"from": start.isoformat(), "to": end.isoformat()})

        # Assert
        assert response["statusCode"] == 200

    def test_range_of_62_days_and_one_second_returns_400_validation(self) -> None:
        # Arrange: the smallest possible overage past the 62-day cap.
        start = datetime.now(UTC) + timedelta(days=1)
        end = start + timedelta(days=62, seconds=1)

        # Act
        response = self._get(**{"from": start.isoformat(), "to": end.isoformat()})

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_from_with_z_suffix_and_to_with_numeric_offset_are_both_accepted(self) -> None:
        # Arrange: `datetime.fromisoformat` (Python 3.11+) accepts a
        # trailing "Z" as UTC — a client mixing "Z" and a numeric UTC
        # offset across from/to must not be rejected just for that.
        start = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
        end = start + timedelta(days=1)
        end_jst = end.astimezone(timezone(timedelta(hours=9)))

        # Act
        response = self._get(**{"from": start.isoformat().replace("+00:00", "Z"), "to": end_jst.isoformat()})

        # Assert
        assert response["statusCode"] == 200

    def test_missing_query_string_parameters_entirely_returns_400_validation(self) -> None:
        # Arrange: `event.get("queryStringParameters")` is None (the key is
        # absent entirely, e.g. API Gateway sends no query string at all) —
        # distinct from an empty dict, which `_parse_range` must also
        # tolerate via its `or {}` fallback rather than raising a TypeError.
        event = _event("GET", "GET /schedule/posts", user_id=None, api_key=SCHEDULER_API_KEY_VALUE)
        assert "queryStringParameters" not in event

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_returns_posts_in_range_excluding_canceled_and_failed(self, schedule_repo: InMemoryRepo) -> None:
        # Arrange
        base = datetime.now(UTC) + timedelta(days=1)
        x_channel = next(c for c in schedule_repo.list_channels() if c.service == "x")
        tiktok_channel = next(c for c in schedule_repo.list_channels() if c.service == "tiktok")

        def _seed(channel_id: Any, due_at: datetime, status: PostStatus, s3_key: str) -> None:
            submission_id = schedule_repo.create_submission(
                title="clip",
                s3_key=s3_key,
                video_url="https://cdn.example.com/x.mp4",
                thumbnail_url=None,
                duration_sec=10.0,
                approved_at=FIXED_NOW,
            )
            post, _created = schedule_repo.claim_pending_post(
                submission_id=submission_id,
                channel_id=channel_id,
                caption="hi",
                mode="customScheduled",
                due_at=due_at,
                is_draft=False,
                platform_options={},
            )
            schedule_repo.update_post(post.id, status=status)

        _seed(x_channel.id, base + timedelta(hours=1), PostStatus.SCHEDULED, "public/videos/2030/01/in.mp4")
        _seed(
            tiktok_channel.id,
            base + timedelta(hours=2),
            PostStatus.CANCELED,
            "public/videos/2030/01/canceled.mp4",
        )
        _seed(x_channel.id, base + timedelta(hours=3), PostStatus.FAILED, "public/videos/2030/01/failed.mp4")
        _seed(
            x_channel.id,
            base + timedelta(days=10),
            PostStatus.SCHEDULED,
            "public/videos/2030/01/outside.mp4",
        )

        # Act
        response = self._get(**{"from": base.isoformat(), "to": (base + timedelta(days=1)).isoformat()})

        # Assert
        assert response["statusCode"] == 200
        posts = json.loads(response["body"])["posts"]
        assert len(posts) == 1
        assert posts[0]["service"] == "x"
        assert posts[0]["status"] == "scheduled"
        assert posts[0]["title"] == "clip"


class TestScheduleSubmissions:
    def _body(self, **overrides: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "video_key": VALID_SCHEDULE_VIDEO_KEY,
            "video": {"duration_sec": 10.0, "width": 1080, "height": 1920, "video_codec": "h264"},
            "targets": [{"service": "x", "caption": "hello", "due_at": _schedule_future()}],
        }
        defaults.update(overrides)
        return defaults

    def _submit(self, body: dict[str, Any], context: object = None) -> dict[str, Any]:
        event = _event(
            "POST",
            "POST /schedule/submissions",
            user_id=None,
            api_key=SCHEDULER_API_KEY_VALUE,
            body=body,
        )
        return handler_module.handler(event, context=context)

    def test_dry_run_returns_plan_without_side_effects(
        self, schedule_buffer: FakeBuffer, schedule_repo: InMemoryRepo
    ) -> None:
        # Act
        response = self._submit(self._body(dry_run=True))

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["dry_run"] is True
        assert body["has_error"] is False
        assert "plan" in body
        assert schedule_buffer.calls == []
        assert schedule_repo.get_submission_by_s3_key(VALID_SCHEDULE_VIDEO_KEY) is None

    def test_dry_run_after_a_real_submission_still_makes_no_further_changes(
        self, schedule_buffer: FakeBuffer, schedule_repo: InMemoryRepo
    ) -> None:
        # Arrange: a submission for this video already exists (and is
        # already scheduled) — a dry run must be a pure read even then, not
        # just when nothing exists yet.
        real = self._submit(self._body())
        assert real["statusCode"] == 200
        submission_before = schedule_repo.get_submission_by_s3_key(VALID_SCHEDULE_VIDEO_KEY)
        assert submission_before is not None
        posts_before = schedule_repo.list_posts_by_submission(submission_before.id)
        calls_before = len(schedule_buffer.calls)

        # Act: dry run with a different caption/due_at for the same video.
        response = self._submit(
            self._body(
                dry_run=True,
                targets=[{"service": "x", "caption": "a totally different caption", "due_at": _schedule_future(9)}],
            )
        )

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["dry_run"] is True
        assert len(schedule_buffer.calls) == calls_before
        submission_after = schedule_repo.get_submission_by_s3_key(VALID_SCHEDULE_VIDEO_KEY)
        assert submission_after == submission_before
        assert schedule_repo.list_posts_by_submission(submission_before.id) == posts_before

    def test_malformed_json_body_returns_400_bad_request(self) -> None:
        # Arrange
        event = _event(
            "POST",
            "POST /schedule/submissions",
            user_id=None,
            api_key=SCHEDULER_API_KEY_VALUE,
            body="{not valid json",
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "bad_request"

    def test_json_array_body_returns_400_bad_request(self) -> None:
        # Arrange: valid JSON, but not an object — parse_body requires a dict.
        event = _event(
            "POST",
            "POST /schedule/submissions",
            user_id=None,
            api_key=SCHEDULER_API_KEY_VALUE,
            body="[1, 2, 3]",
        )

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "bad_request"

    def test_base64_encoded_body_is_decoded_and_accepted(self) -> None:
        # Arrange: API Gateway HTTP API sometimes base64-encodes the body
        # and sets isBase64Encoded=True — /schedule/submissions must decode
        # it the same way the admin routes do (shared parse_body).
        raw = json.dumps(self._body(dry_run=True))
        event = _event(
            "POST",
            "POST /schedule/submissions",
            user_id=None,
            api_key=SCHEDULER_API_KEY_VALUE,
            body=base64.b64encode(raw.encode("utf-8")).decode("ascii"),
        )
        event["isBase64Encoded"] = True

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["dry_run"] is True

    def test_invalid_base64_body_returns_400_bad_request(self) -> None:
        # Arrange
        event = _event(
            "POST",
            "POST /schedule/submissions",
            user_id=None,
            api_key=SCHEDULER_API_KEY_VALUE,
            body="not-valid-base64!!!",
        )
        event["isBase64Encoded"] = True

        # Act
        response = handler_module.handler(event)

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "bad_request"

    def test_missing_video_upload_returns_400_upload_missing(self) -> None:
        # Act
        response = self._submit(self._body(video_key=f"public/videos/2030/01/{'c' * 32}.mp4"))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "upload_missing"

    def test_invalid_video_key_format_returns_400_validation(self) -> None:
        # Act
        response = self._submit(self._body(video_key="public/videos/not-a-valid-key.mp4"))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_video_key_with_trailing_newline_returns_400_validation(self) -> None:
        # Arrange: `$` (unlike \Z or fullmatch) matches just before a
        # trailing newline, not only the true end of string — a key with a
        # smuggled trailing "\n" must still be rejected.
        response = self._submit(self._body(video_key=VALID_SCHEDULE_VIDEO_KEY + "\n"))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_video_key_with_fullwidth_unicode_digits_returns_400_validation(self) -> None:
        # Arrange: `\d` (without re.ASCII) matches Unicode digit categories
        # too, e.g. full-width digits — only real ASCII 0-9 should count.
        fullwidth_key = f"public/videos/２０３０/01/{'a' * 32}.mp4"
        response = self._submit(self._body(video_key=fullwidth_key))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_invalid_thumbnail_key_format_returns_400_validation(self) -> None:
        # Act
        response = self._submit(self._body(thumbnail_key="public/thumbnails/bad.jpg"))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_thumbnail_key_with_correct_shape_but_wrong_extension_returns_400_validation(self) -> None:
        # Arrange: right layout, but a ".png" instead of the required
        # ".jpg" — presign_post only ever issues ".jpg" thumbnail keys.
        wrong_extension_key = f"public/thumbnails/2030/01/{'b' * 32}.png"

        # Act
        response = self._submit(self._body(thumbnail_key=wrong_extension_key))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_video_key_under_a_different_top_level_prefix_returns_400_validation(self) -> None:
        # Arrange: `_validate_key` anchors on `settings.s3_public_prefix`
        # ("public/") — a key under any other prefix (e.g. a private/
        # bucket area) must never be accepted as if it were presigned.
        other_prefix_key = f"private/videos/2030/01/{'a' * 32}.mp4"

        # Act
        response = self._submit(self._body(video_key=other_prefix_key))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_video_key_with_path_traversal_sequence_returns_400_validation(self) -> None:
        # Arrange: a ".." inserted where the fixed-width uuid segment
        # should be must never match the `[0-9a-f]{32}` pattern.
        traversal_key = "public/videos/2030/01/../../../etc/passwd.mp4"

        # Act
        response = self._submit(self._body(video_key=traversal_key))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_past_due_at_returns_400_validation(self) -> None:
        # Act
        response = self._submit(
            self._body(targets=[{"service": "x", "caption": "hi", "due_at": "2020-01-01T00:00:00+00:00"}])
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_due_at_without_timezone_returns_400_validation(self) -> None:
        # Act
        response = self._submit(
            self._body(targets=[{"service": "x", "caption": "hi", "due_at": "2035-01-01T00:00:00"}])
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_duplicate_service_targets_returns_400_validation(self) -> None:
        # Act
        response = self._submit(
            self._body(
                targets=[
                    {"service": "x", "caption": "a", "due_at": _schedule_future()},
                    {"service": "x", "caption": "b", "due_at": _schedule_future()},
                ]
            )
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_more_than_three_targets_returns_400_validation(self) -> None:
        # Act
        response = self._submit(
            self._body(
                targets=[
                    {"service": s, "caption": "a", "due_at": _schedule_future()}
                    for s in ("tiktok", "instagram", "x", "tiktok")
                ]
            )
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_empty_targets_returns_400_validation(self) -> None:
        # Act
        response = self._submit(self._body(targets=[]))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_invalid_service_literal_returns_400_validation(self) -> None:
        # Act
        response = self._submit(
            self._body(targets=[{"service": "youtube", "caption": "hi", "due_at": _schedule_future()}])
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_empty_caption_string_returns_400_validation(self) -> None:
        # Arrange: ScheduleTargetInput requires at least one non-whitespace
        # character — an empty caption must be rejected before ever
        # reaching validate_caption's (upper-bound-only) checks.
        response = self._submit(
            self._body(targets=[{"service": "x", "caption": "", "due_at": _schedule_future()}])
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_whitespace_only_caption_returns_400_validation(self) -> None:
        # Arrange
        response = self._submit(
            self._body(targets=[{"service": "x", "caption": "   ", "due_at": _schedule_future()}])
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_real_submission_creates_posts_and_returns_scheduled(
        self, schedule_buffer: FakeBuffer, schedule_repo: InMemoryRepo
    ) -> None:
        # Act
        response = self._submit(
            self._body(
                targets=[
                    {"service": "tiktok", "caption": "hi tiktok", "due_at": _schedule_future()},
                    {"service": "x", "caption": "hi x", "due_at": _schedule_future()},
                ]
            )
        )

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["dry_run"] is False
        assert body["incomplete"] is False
        assert body["submission_id"] is not None
        assert {r["service"]: r["status"] for r in body["results"]} == {
            "tiktok": "scheduled",
            "x": "scheduled",
        }
        assert schedule_repo.get_submission_by_s3_key(VALID_SCHEDULE_VIDEO_KEY) is not None

    def test_missing_thumbnail_upload_leaves_thumbnail_url_null_not_error(
        self, schedule_repo: InMemoryRepo
    ) -> None:
        # Act
        response = self._submit(self._body(thumbnail_key=f"public/thumbnails/2030/01/{'c' * 32}.jpg"))

        # Assert
        assert response["statusCode"] == 200
        submission = schedule_repo.get_submission_by_s3_key(VALID_SCHEDULE_VIDEO_KEY)
        assert submission is not None
        assert submission.thumbnail_url is None

    def test_uploaded_thumbnail_is_reflected_in_submission(self, schedule_repo: InMemoryRepo) -> None:
        # Act
        response = self._submit(self._body(thumbnail_key=VALID_SCHEDULE_THUMBNAIL_KEY))

        # Assert
        assert response["statusCode"] == 200
        submission = schedule_repo.get_submission_by_s3_key(VALID_SCHEDULE_VIDEO_KEY)
        assert submission is not None
        assert submission.thumbnail_url == f"https://cdn.example.com/{VALID_SCHEDULE_THUMBNAIL_KEY}"

    def test_idempotent_resend_reuses_submission_and_does_not_resend(
        self, schedule_buffer: FakeBuffer
    ) -> None:
        # Arrange
        body = self._body()
        first = self._submit(body)
        assert first["statusCode"] == 200
        first_submission_id = json.loads(first["body"])["submission_id"]

        # Act
        second = self._submit(body)

        # Assert
        assert second["statusCode"] == 200
        parsed = json.loads(second["body"])
        assert parsed["submission_id"] == first_submission_id
        assert parsed["results"][0]["status"] == "scheduled"
        assert len(schedule_buffer.calls) == 1

    def test_incomplete_true_when_deadline_is_imminent(self, schedule_buffer: FakeBuffer) -> None:
        # Arrange: remaining time below api/_deadline's stop margin (15s) ->
        # should_stop() trips before the first target is even attempted.
        body = self._body(
            targets=[
                {"service": "tiktok", "caption": "hi", "due_at": _schedule_future()},
                {"service": "x", "caption": "hi", "due_at": _schedule_future()},
            ]
        )

        # Act
        response = self._submit(body, context=_FakeLambdaContext(1_000))

        # Assert
        assert response["statusCode"] == 200
        parsed = json.loads(response["body"])
        assert parsed["incomplete"] is True
        assert all(r["status"] == "not_attempted" for r in parsed["results"])
        assert schedule_buffer.calls == []

    def test_incomplete_true_with_a_mix_of_sent_and_not_attempted_results(
        self, schedule_buffer: FakeBuffer
    ) -> None:
        # Arrange: the deadline only trips partway through — `incomplete`
        # must be true from a single not_attempted target among otherwise
        # successful ones, not just when every target is not_attempted.
        class _DecreasingLambdaContext:
            def __init__(self, values: list[int]) -> None:
                self._values = iter(values)

            def get_remaining_time_in_millis(self) -> int:
                return next(self._values)

        body = self._body(
            targets=[
                {"service": "tiktok", "caption": "hi", "due_at": _schedule_future()},
                {"service": "x", "caption": "hi", "due_at": _schedule_future()},
            ]
        )

        # Act: plenty of time for the first target, then below the 15s margin.
        response = self._submit(body, context=_DecreasingLambdaContext([60_000, 1_000]))

        # Assert
        assert response["statusCode"] == 200
        parsed = json.loads(response["body"])
        assert parsed["incomplete"] is True
        statuses = {r["service"]: r["status"] for r in parsed["results"]}
        assert statuses == {"tiktok": "scheduled", "x": "not_attempted"}
        assert len(schedule_buffer.calls) == 1

    def test_negative_thumbnail_offset_ms_returns_400_validation(self) -> None:
        # Arrange
        response = self._submit(self._body(thumbnail_offset_ms=-500))

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_zero_duration_video_returns_400_validation(self) -> None:
        # Act
        response = self._submit(
            self._body(video={"duration_sec": 0.0, "width": 1080, "height": 1920, "video_codec": "h264"})
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_negative_duration_video_returns_400_validation(self) -> None:
        # Act
        response = self._submit(
            self._body(video={"duration_sec": -1.0, "width": 1080, "height": 1920, "video_codec": "h264"})
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_zero_width_video_returns_400_validation(self) -> None:
        # Act
        response = self._submit(
            self._body(video={"duration_sec": 10.0, "width": 0, "height": 1920, "video_codec": "h264"})
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"

    def test_zero_height_video_returns_400_validation(self) -> None:
        # Act
        response = self._submit(
            self._body(video={"duration_sec": 10.0, "width": 1080, "height": 0, "video_codec": "h264"})
        )

        # Assert
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "validation"
