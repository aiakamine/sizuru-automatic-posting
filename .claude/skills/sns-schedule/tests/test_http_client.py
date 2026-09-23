"""Tests for http_client: request building and error-message mapping, with
urllib's transport replaced by a fake so no network call is ever made."""

import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from http_client import (  # noqa: E402
    TIMEOUT_SEC,
    ApiError,
    TransportResponse,
    default_transport,
    request_json,
)


def _fake_transport(status: int, body: bytes):
    captured: dict = {}

    def transport(request: urllib.request.Request) -> TransportResponse:
        captured["request"] = request
        return TransportResponse(status=status, body=body)

    return transport, captured


class TestRequestJsonSuccess:
    def test_returns_parsed_body_on_200(self) -> None:
        # Arrange
        transport, captured = _fake_transport(200, b'{"posts": []}')

        # Act
        result = request_json(
            "https://api.example.com", "secret-key", "GET", "/schedule/posts", transport=transport
        )

        # Assert
        assert result == {"posts": []}

    def test_sends_x_api_key_header_and_never_puts_key_in_url(self) -> None:
        # Arrange
        transport, captured = _fake_transport(200, b"{}")

        # Act
        request_json("https://api.example.com", "secret-key", "GET", "/schedule/posts", transport=transport)

        # Assert
        request = captured["request"]
        assert request.get_header("X-api-key") == "secret-key"
        assert "secret-key" not in request.full_url

    def test_sends_json_body_for_post(self) -> None:
        # Arrange
        transport, captured = _fake_transport(200, b"{}")

        # Act
        request_json(
            "https://api.example.com",
            "key",
            "POST",
            "/schedule/submissions",
            body={"video_key": "public/videos/2026/09/abc.mp4"},
            transport=transport,
        )

        # Assert
        request = captured["request"]
        assert request.method == "POST"
        assert b"video_key" in request.data

    def test_strips_trailing_slash_from_base_url(self) -> None:
        # Arrange
        transport, captured = _fake_transport(200, b"{}")

        # Act
        request_json("https://api.example.com/", "key", "GET", "/schedule/posts", transport=transport)

        # Assert
        assert captured["request"].full_url == "https://api.example.com/schedule/posts"

    def test_empty_body_is_treated_as_empty_dict(self) -> None:
        # Arrange
        transport, _ = _fake_transport(200, b"")

        # Act
        result = request_json("https://api.example.com", "key", "GET", "/schedule/posts", transport=transport)

        # Assert
        assert result == {}

    def test_204_no_content_returns_empty_dict(self) -> None:
        # Arrange: a successful DELETE-style response with no body at all
        transport, _ = _fake_transport(204, b"")

        # Act
        result = request_json(
            "https://api.example.com", "key", "DELETE", "/schedule/posts/1", transport=transport
        )

        # Assert
        assert result == {}

    def test_success_body_that_is_a_json_array_falls_back_to_empty_dict(self) -> None:
        # Arrange: a 2xx body that parses to something other than a dict
        # must not be returned as-is (callers assume a dict)
        transport, _ = _fake_transport(200, b"[1, 2, 3]")

        # Act
        result = request_json("https://api.example.com", "key", "GET", "/schedule/posts", transport=transport)

        # Assert
        assert result == {}


class TestRequestJsonErrors:
    def test_401_maps_to_unauthorized_message(self) -> None:
        # Arrange
        transport, _ = _fake_transport(401, b'{"error": {"code": "unauthorized", "message": "bad key"}}')

        # Act / Assert
        with pytest.raises(ApiError) as excinfo:
            request_json("https://api.example.com", "key", "GET", "/schedule/posts", transport=transport)
        assert excinfo.value.code == "unauthorized"
        assert "APIキー" in str(excinfo.value)

    def test_503_maps_to_scheduler_disabled_message(self) -> None:
        # Arrange
        transport, _ = _fake_transport(
            503, b'{"error": {"code": "scheduler_disabled", "message": "no key configured"}}'
        )

        # Act / Assert
        with pytest.raises(ApiError) as excinfo:
            request_json("https://api.example.com", "key", "GET", "/schedule/posts", transport=transport)
        assert excinfo.value.status == 503
        assert "設定されていません" in str(excinfo.value)

    def test_400_validation_message_includes_server_message(self) -> None:
        # Arrange
        transport, _ = _fake_transport(
            400, b'{"error": {"code": "validation", "message": "due_at must be in the future"}}'
        )

        # Act / Assert
        with pytest.raises(ApiError) as excinfo:
            request_json("https://api.example.com", "key", "POST", "/schedule/submissions", transport=transport)
        assert "due_at must be in the future" in str(excinfo.value)

    def test_upload_missing_code_produces_readable_message(self) -> None:
        # Arrange
        transport, _ = _fake_transport(
            400, b'{"error": {"code": "upload_missing", "message": "video not found"}}'
        )

        # Act / Assert
        with pytest.raises(ApiError) as excinfo:
            request_json("https://api.example.com", "key", "POST", "/schedule/submissions", transport=transport)
        assert excinfo.value.code == "upload_missing"

    def test_error_body_that_is_not_json_still_raises_readable_error(self) -> None:
        # Arrange: a 500 from e.g. an ALB health check page, not our API
        transport, _ = _fake_transport(500, b"<html>Internal Server Error</html>")

        # Act / Assert
        with pytest.raises(ApiError) as excinfo:
            request_json("https://api.example.com", "key", "GET", "/schedule/posts", transport=transport)
        assert excinfo.value.status == 500

    def test_non_json_error_body_produces_the_generic_status_only_message(self) -> None:
        # Arrange: no parseable "error" object -- code/message both end up
        # None, so the message must fall back to the generic status-only form
        transport, _ = _fake_transport(500, b"<html>Internal Server Error</html>")

        # Act / Assert
        with pytest.raises(ApiError) as excinfo:
            request_json("https://api.example.com", "key", "GET", "/schedule/posts", transport=transport)
        assert str(excinfo.value) == "APIエラー（status=500）"
        assert excinfo.value.code is None

    def test_never_includes_api_key_in_the_raised_message(self) -> None:
        # Arrange
        transport, _ = _fake_transport(401, b'{"error": {"code": "unauthorized", "message": "bad"}}')

        # Act / Assert
        with pytest.raises(ApiError) as excinfo:
            request_json("https://api.example.com", "top-secret-key", "GET", "/schedule/posts", transport=transport)
        assert "top-secret-key" not in str(excinfo.value)


class TestClientTimeout:
    def test_timeout_is_40_seconds(self) -> None:
        # Arrange / Act / Assert: the client should wait longer than the
        # backend's ~29s Lambda deadline before giving up.
        assert TIMEOUT_SEC == 40


class TestDefaultTransportTimeoutHandling:
    def test_raw_timeout_error_from_reading_the_response_is_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: simulate a timeout raised while reading the response body
        # (not wrapped in URLError by urllib in every code path)
        class _FakeUrlopenCtx:
            status = 200

            def __enter__(self) -> "_FakeUrlopenCtx":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def read(self) -> bytes:
                raise TimeoutError("timed out")

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeUrlopenCtx())
        request = urllib.request.Request("https://api.example.com/schedule/posts")

        # Act / Assert
        with pytest.raises(ApiError, match="タイムアウト"):
            default_transport(request)

    def test_urlerror_wrapping_a_timeout_is_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: simulate the connect-phase case, where urllib wraps the
        # underlying TimeoutError in a URLError
        def _raise(*a: object, **k: object) -> None:
            raise urllib.error.URLError(TimeoutError("timed out"))

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        request = urllib.request.Request("https://api.example.com/schedule/posts")

        # Act / Assert
        with pytest.raises(ApiError, match="タイムアウト"):
            default_transport(request)
