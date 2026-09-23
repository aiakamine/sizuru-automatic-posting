"""API Gateway HTTP API (payload format 2.0) Lambda handler for the admin
dashboard: edit/cancel/sync a post via the existing Publisher.

Routes (must match infra/sam/template.yaml exactly):
  PATCH  /posts/{post_id}          -> Publisher.edit
  POST   /posts/{post_id}/cancel   -> Publisher.cancel
  POST   /sync                     -> Publisher.sync
  /schedule/*                      -> sizuru_post.api.schedule (separate
                                       auth scheme — see dispatch() below)

Auth: the HTTP API has NO authorizer attached (see infra/sam/README.md —
API Gateway's native JWT authorizer only supports RSA, but Supabase signs
with ES256). This handler verifies the `Authorization: Bearer <token>`
header itself with PyJWT (sizuru_post.api.auth) before doing anything else,
then checks the verified `sub` claim against admin_users via the repo
(defense in depth — the browser also reads `submissions`/`posts` directly
via RLS regardless of this API). `/schedule/*` routes are dispatched to
sizuru_post.api.schedule BEFORE this JWT check — they use a completely
separate x-api-key scheme instead (see that module's docstring); a JWT
never authenticates them, and their API key never authenticates these
admin routes.

CORS is handled entirely by API Gateway's CORS configuration, not here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sizuru_post.adapters.buffer import (
    BufferAmbiguousError,
    BufferAuthError,
    BufferError,
    BufferProtocolError,
)
from sizuru_post.api import _container as container
from sizuru_post.api import schedule as schedule_api
from sizuru_post.api._deadline import make_should_stop
from sizuru_post.api._http import ApiError, get_header
from sizuru_post.api._http import error_response as _error_response
from sizuru_post.api._http import parse_body as _parse_body
from sizuru_post.api._http import response as _response
from sizuru_post.api.auth import AuthError, verify_bearer_token
from sizuru_post.config import Settings
from sizuru_post.domain.models import validate_due_at_is_future
from sizuru_post.publisher import (
    BufferRejectedError,
    PostNotEditableError,
    PostNotFoundError,
    Publisher,
    RequestValidationError,
)
from sizuru_post.serialization import target_result_to_dict

logger = logging.getLogger(__name__)


def _authenticate(event: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Verify the request's Supabase-issued JWT ourselves (see this
    module's docstring for why) and return its claims."""
    if not settings.supabase_jwt_issuer or not settings.supabase_jwt_audience:
        raise RuntimeError("SUPABASE_JWT_ISSUER/SUPABASE_JWT_AUDIENCE が設定されていません")
    header = get_header(event, "authorization")
    try:
        return verify_bearer_token(
            header,
            issuer=settings.supabase_jwt_issuer,
            audience=settings.supabase_jwt_audience,
            jwk_client=container.get_jwk_client(),
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
    error = validate_due_at_is_future(parsed)
    if error is not None:
        raise ApiError(400, "validation", error)
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


def _is_schedule_route(route_key: str) -> bool:
    _, _, path = route_key.partition(" ")
    return path.startswith("/schedule/")


def handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    try:
        if _is_schedule_route(event.get("routeKey", "")):
            # A completely separate auth scheme (x-api-key) — see this
            # module's docstring and api/schedule.py's. Never reaches
            # _authenticate()/_require_admin() below.
            return schedule_api.dispatch(event, context)
        settings = container.get_settings()
        publisher = container.get_publisher()
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
        container.reset()
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
