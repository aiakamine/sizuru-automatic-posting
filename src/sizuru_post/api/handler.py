"""API Gateway HTTP API (payload format 2.0) Lambda handler for the admin
dashboard: edit/cancel/sync a post via the existing Publisher.

Routes (must match infra/sam/template.yaml exactly):
  PATCH  /posts/{post_id}          -> Publisher.edit
  POST   /posts/{post_id}/cancel   -> Publisher.cancel
  POST   /sync                     -> Publisher.sync

Auth: the HTTP API has NO authorizer attached (see infra/sam/README.md —
API Gateway's native JWT authorizer only supports RSA, but Supabase signs
with ES256). This handler verifies the `Authorization: Bearer <token>`
header itself with PyJWT (sizuru_post.api.auth) before doing anything else,
then checks the verified `sub` claim against admin_users via the repo
(defense in depth — the browser also reads `submissions`/`posts` directly
via RLS regardless of this API).

CORS is handled entirely by API Gateway's CORS configuration, not here.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import jwt

from sizuru_post.adapters.buffer import (
    BufferAmbiguousError,
    BufferAuthError,
    BufferError,
    BufferProtocolError,
)
from sizuru_post.api._deadline import make_should_stop
from sizuru_post.api.auth import AuthError, build_jwk_client, verify_bearer_token
from sizuru_post.config import Settings
from sizuru_post.factory import build_publisher_without_uploads, build_settings, reset_ssm_cache
from sizuru_post.publisher import (
    BufferRejectedError,
    PostNotEditableError,
    PostNotFoundError,
    Publisher,
    RequestValidationError,
)
from sizuru_post.serialization import target_result_to_dict

logger = logging.getLogger(__name__)

_settings: Settings | None = None
_publisher: Publisher | None = None
_jwk_client: jwt.PyJWKClient | None = None


def get_settings() -> Settings:
    """Build Settings lazily, once per container."""
    global _settings
    if _settings is None:
        _settings = build_settings()
    return _settings


def get_publisher() -> Publisher:
    """Build the Publisher lazily, once per container (Lambda execution
    environment reuse avoids rebuilding it on every warm invocation)."""
    global _publisher
    if _publisher is None:
        _publisher = build_publisher_without_uploads(get_settings())
    return _publisher


def get_jwk_client() -> jwt.PyJWKClient:
    """Build the JWKS resolver lazily, once per container — it caches keys
    in-process for its own lifespan (see api/auth.build_jwk_client), so
    reusing one instance across invocations avoids refetching the JWKS on
    every request."""
    global _jwk_client
    if _jwk_client is None:
        settings = get_settings()
        if not settings.supabase_jwt_issuer:
            raise RuntimeError("SUPABASE_JWT_ISSUER が設定されていません")
        _jwk_client = build_jwk_client(settings.supabase_jwt_issuer)
    return _jwk_client


def reset_publisher() -> None:
    """Drop every per-container cache (Settings, Publisher, JWKS client)
    and the SSM secret cache, so the *next* invocation rebuilds everything
    from a fresh read (the API key may have been rotated). Does NOT retry
    the request that triggered this — see the BufferAuthError handling in
    `handler()`."""
    global _settings, _publisher, _jwk_client
    _settings = None
    _publisher = None
    _jwk_client = None
    reset_ssm_cache()


class ApiError(Exception):
    """Carries the HTTP status/error code/message for a deliberate,
    expected failure (auth, validation, bad request)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _error_response(status_code: int, code: str, message: str) -> dict[str, Any]:
    return _response(status_code, {"error": {"code": code, "message": message}})


def _get_authorization_header(event: dict[str, Any]) -> str | None:
    # HTTP API payload v2 always lowercases header names, but be defensive
    # (proxying, hand-built test events, ...) rather than assume it.
    headers = event.get("headers") or {}
    for key, value in headers.items():
        if key.lower() == "authorization":
            return str(value)
    return None


def _authenticate(event: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Verify the request's Supabase-issued JWT ourselves (see this
    module's docstring for why) and return its claims."""
    if not settings.supabase_jwt_issuer or not settings.supabase_jwt_audience:
        raise RuntimeError("SUPABASE_JWT_ISSUER/SUPABASE_JWT_AUDIENCE が設定されていません")
    header = _get_authorization_header(event)
    try:
        return verify_bearer_token(
            header,
            issuer=settings.supabase_jwt_issuer,
            audience=settings.supabase_jwt_audience,
            jwk_client=get_jwk_client(),
        )
    except AuthError as exc:
        raise ApiError(401, "unauthorized", "認証情報が無効です") from exc


def _require_admin(claims: dict[str, Any], publisher: Publisher) -> str:
    user_id = claims.get("sub")
    if not user_id:
        raise ApiError(401, "unauthorized", "認証情報がありません")
    if not publisher._repo.is_admin(user_id):  # noqa: SLF001 - same package, intentional port access
        raise ApiError(403, "forbidden", "管理者権限がありません")
    return str(user_id)


def _parse_post_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ApiError(404, "not_found", "投稿が見つかりません") from exc


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if raw is None:
        return {}
    if event.get("isBase64Encoded"):
        # API Gateway HTTP API (payload v2) base64-encodes the body in some
        # cases (e.g. certain proxy/CDN paths or non-UTF8-safe content
        # negotiation) and sets isBase64Encoded=True to say so.
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ApiError(400, "bad_request", "リクエストボディがbase64として不正です") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ApiError(400, "bad_request", "リクエストボディがJSONとして不正です") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "bad_request", "リクエストボディはオブジェクトである必要があります")
    return data


def _parse_caption(body: dict[str, Any]) -> str | None:
    caption = body.get("caption")
    if caption is not None and not isinstance(caption, str):
        raise ApiError(400, "validation", "caption は文字列で指定してください")
    return caption


def _parse_due_at(body: dict[str, Any]) -> datetime | None:
    raw = body.get("due_at")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ApiError(400, "validation", "due_at は文字列（ISO8601）で指定してください")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(400, "validation", "due_at の形式が不正です（ISO8601で指定してください）") from exc
    if parsed.tzinfo is None:
        raise ApiError(400, "validation", "due_at にはタイムゾーンを含めてください")
    return parsed


_DB_WRITE_FAILED_MESSAGE = "Bufferには反映済みですが記録に失敗しました。更新ボタンで同期してください"


def _handle_edit(publisher: Publisher, post_id: str, event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    caption = _parse_caption(body)
    due_at = _parse_due_at(body)
    post_uuid = _parse_post_id(post_id)

    result = publisher.edit(post_uuid, caption=caption, due_at=due_at)
    if result.status == "db_write_failed":
        return _error_response(502, "db_write_failed", _DB_WRITE_FAILED_MESSAGE)
    return _response(200, {"result": target_result_to_dict(result)})


def _handle_cancel(publisher: Publisher, post_id: str) -> dict[str, Any]:
    post_uuid = _parse_post_id(post_id)
    result = publisher.cancel(post_uuid)
    if result.status == "db_write_failed":
        return _error_response(502, "db_write_failed", _DB_WRITE_FAILED_MESSAGE)
    return _response(200, {"result": target_result_to_dict(result)})


def _handle_sync(publisher: Publisher, context: object) -> dict[str, Any]:
    # The manual /sync endpoint (the dashboard's "sync now" button) must
    # reconcile every scheduled/draft post regardless of due_at, unlike the
    # EventBridge scheduler's frequent full=False sync (see sync_handler.py).
    counts = publisher.sync(full=True, should_stop=make_should_stop(context))
    return _response(200, {"sync": counts})


def _dispatch(event: dict[str, Any], publisher: Publisher, context: object) -> dict[str, Any]:
    route_key = event.get("routeKey", "")
    path_params = event.get("pathParameters") or {}

    if route_key == "PATCH /posts/{post_id}":
        return _handle_edit(publisher, path_params.get("post_id", ""), event)
    if route_key == "POST /posts/{post_id}/cancel":
        return _handle_cancel(publisher, path_params.get("post_id", ""))
    if route_key == "POST /sync":
        return _handle_sync(publisher, context)

    raise ApiError(404, "not_found", "不明なエンドポイントです")


def handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    try:
        settings = get_settings()
        publisher = get_publisher()
        claims = _authenticate(event, settings)
        _require_admin(claims, publisher)
        return _dispatch(event, publisher, context)
    except ApiError as exc:
        return _error_response(exc.status_code, exc.code, exc.message)
    except PostNotFoundError as exc:
        return _error_response(404, "not_found", str(exc))
    except (PostNotEditableError, BufferRejectedError) as exc:
        return _error_response(400, "invalid_state", str(exc))
    except RequestValidationError as exc:
        return _error_response(400, "validation", str(exc))
    except (BufferAmbiguousError, BufferProtocolError) as exc:
        # A malformed/undecodable "success" response after a mutation was
        # dispatched is just as ambiguous as BufferAmbiguousError.
        return _error_response(409, "ambiguous", str(exc))
    except BufferAuthError as exc:
        # The API key itself was rejected — it may have been rotated away
        # in SSM. Reset the cache so the *next* invocation re-reads it
        # fresh; this request itself is not retried.
        logger.error("Buffer認証エラーが発生しました: %s", exc)
        reset_publisher()
        return _error_response(502, "upstream", "Bufferとの認証でエラーが発生しました")
    except BufferError:
        # Buffer reachable but erroring (5xx, malformed response, rate
        # limit, ...) — not the caller's fault.
        logger.exception("Buffer error in API handler")
        return _error_response(502, "upstream", "Bufferとの通信でエラーが発生しました")
    except Exception:  # noqa: BLE001 - must never leak internals to the client
        # Anything else (an unexpected ValueError, a pydantic/config error,
        # a repo/model validation failure, ...) is our own bug, not the
        # caller's — never echo its details back to the client.
        logger.exception("Unhandled error in API handler")
        return _error_response(500, "internal", "サーバー内部でエラーが発生しました")
