"""Shared HTTP envelope helpers for the API Gateway Lambda handlers.

Both api/handler.py (the admin dashboard's JWT-authenticated routes) and
api/schedule.py (the scheduling skill's x-api-key-authenticated
/schedule/* routes) speak the same wire format — a uniform JSON body and
the same `{"error": {"code", "message"}}` shape on failure — so it lives
here once instead of being duplicated per module.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any


class ApiError(Exception):
    """Carries the HTTP status/error code/message for a deliberate,
    expected failure (auth, validation, bad request)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def get_header(event: dict[str, Any], name: str) -> str | None:
    """Case-insensitive header lookup. HTTP API payload v2 always
    lowercases header names, but this stays defensive (proxying,
    hand-built events, ...) rather than assume it — used for both
    `Authorization` (api/handler.py) and `x-api-key` (api/schedule.py)."""
    headers = event.get("headers") or {}
    lower_name = name.lower()
    for key, value in headers.items():
        if key.lower() == lower_name:
            return str(value)
    return None


def response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def error_response(status_code: int, code: str, message: str) -> dict[str, Any]:
    return response(status_code, {"error": {"code": code, "message": message}})


def parse_body(event: dict[str, Any]) -> dict[str, Any]:
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
