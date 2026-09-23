"""Minimal urllib-based HTTP client for the sns-schedule skill's API calls.

Kept separate from schedule.py so tests can inject a fake `transport`
callable instead of touching the network. NEVER log or print `api_key`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TIMEOUT_SEC = 40  # longer than the backend Lambda's ~29s deadline (see _deadline.py)


@dataclass
class TransportResponse:
    status: int
    body: bytes


Transport = Callable[[urllib.request.Request], TransportResponse]


class ApiError(Exception):
    """Raised for any non-2xx response or transport failure. `message` is
    always a human-readable Japanese string safe to show the user (it never
    contains the API key); `code`/`status` carry the raw server values when
    available so callers can branch on them (e.g. retry on `incomplete`)."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def default_transport(request: urllib.request.Request) -> TransportResponse:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
            return TransportResponse(status=resp.status, body=resp.read())
    except urllib.error.HTTPError as exc:
        return TransportResponse(status=exc.code, body=exc.read())
    except TimeoutError as exc:
        # Raised directly (not wrapped in URLError) when the timeout hits
        # while reading the response body rather than while connecting.
        raise ApiError(f"APIへの接続がタイムアウトしました（{TIMEOUT_SEC}秒）") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ApiError(f"APIへの接続がタイムアウトしました（{TIMEOUT_SEC}秒）") from exc
        raise ApiError(f"APIに接続できません: {exc.reason}") from exc


def request_json(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    transport: Transport = default_transport,
) -> dict[str, Any]:
    """Call `{base_url}{path}` with the given method/body and the
    `x-api-key` header, and return the parsed JSON body on 2xx. Raises
    ApiError (with a readable Japanese message) on any other status or on a
    transport failure."""
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    response = transport(request)
    return _parse_response(response)


def _parse_response(response: TransportResponse) -> dict[str, Any]:
    parsed = _try_parse_json(response.body)

    if 200 <= response.status < 300:
        return parsed if isinstance(parsed, dict) else {}

    error = parsed.get("error") if isinstance(parsed, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    raise ApiError(_readable_message(response.status, code, message), code=code, status=response.status)


def _try_parse_json(body: bytes) -> Any:
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _readable_message(status: int, code: str | None, message: str | None) -> str:
    if status == 401 or code == "unauthorized":
        return "APIキーが正しくありません（unauthorized）。.envのSIZURU_API_KEYを確認してください"
    if status == 503 or code == "scheduler_disabled":
        return "サーバー側にAPIキーが設定されていません（scheduler_disabled）。管理者に確認してください"
    if code == "upload_missing":
        return f"動画のアップロードが完了していません（upload_missing）: {message or ''}"
    if status == 400 or code == "validation":
        return f"リクエストが不正です（validation）: {message or ''}"
    if message:
        return f"APIエラー（{code or status}）: {message}"
    return f"APIエラー（status={status}）"
