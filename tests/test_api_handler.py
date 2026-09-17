"""Tests for sizuru_post.api.handler: API Gateway HTTP API (payload v2) Lambda.

Publisher/Repo are fakes throughout — no real AWS/Supabase/Buffer calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import sizuru_post.api.handler as handler_module
from sizuru_post.adapters.buffer import (
    BufferAmbiguousError,
    BufferAuthError,
    BufferHTTPError,
    BufferProtocolError,
    CreatePostResult,
)
from sizuru_post.adapters.repo import ChannelRow
from sizuru_post.domain.models import Target, Timing, VideoInfo
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


class _FakeAuthSettings:
    supabase_jwt_issuer = JWT_ISSUER
    supabase_jwt_audience = JWT_AUDIENCE


@pytest.fixture(autouse=True)
def patch_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "get_settings", lambda: _FakeAuthSettings())
    monkeypatch.setattr(handler_module, "get_jwk_client", lambda: _FakeJwkClient())


_UNSET = object()


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
    return InMemoryRepo(channels=[_channel("x"), _channel("tiktok")], admin_user_ids={ADMIN_USER_ID})


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


@pytest.fixture(autouse=True)
def patch_publisher(monkeypatch: pytest.MonkeyPatch, publisher: Publisher) -> None:
    monkeypatch.setattr(handler_module, "get_publisher", lambda: publisher)


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
        def _raise_auth_error(**kwargs: Any) -> dict[str, int]:
            raise BufferAuthError("bad key")

        monkeypatch.setattr(publisher, "sync", _raise_auth_error)
        handler_module._publisher = publisher
        reset_calls: list[bool] = []
        monkeypatch.setattr(handler_module, "reset_ssm_cache", lambda: reset_calls.append(True))

        # Act
        handler_module.handler(_event("POST", "POST /sync"))

        # Assert
        assert handler_module._publisher is None
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
