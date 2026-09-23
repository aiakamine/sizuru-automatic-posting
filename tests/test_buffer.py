"""Tests for sizuru_post.adapters.buffer: GraphQL variable building + HTTP client."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from sizuru_post.adapters.buffer import (
    BUFFER_GRAPHQL_URL,
    BufferAmbiguousError,
    BufferAuthError,
    BufferClient,
    BufferGraphQLError,
    BufferHTTPError,
    BufferProtocolError,
    BufferRateLimitError,
    BufferTransportError,
    build_create_post_variables,
)
from sizuru_post.domain.models import PublishRequest, Target, Timing


def _request(**overrides: object) -> PublishRequest:
    defaults: dict[str, object] = {
        "video_path": "video.mp4",
        "timing": Timing(mode="shareNow"),
        "targets": [Target(service="x", caption="hello")],
    }
    defaults.update(overrides)
    return PublishRequest(**defaults)  # type: ignore[arg-type]


class TestBuildCreatePostVariables:
    def test_basic_fields(self) -> None:
        # Arrange
        request = _request(timing=Timing(mode="addToQueue"))
        target = Target(service="x", caption="hello world")

        # Act
        variables = build_create_post_variables(request, target, "chan-1", "https://cdn/video.mp4")

        # Assert
        assert variables["text"] == "hello world"
        assert variables["channelId"] == "chan-1"
        assert variables["schedulingType"] == "automatic"
        assert variables["mode"] == "addToQueue"
        assert variables["assets"] == [{"video": {"url": "https://cdn/video.mp4"}}]
        # CreatePostInput.needsApproval is non-null (Boolean!) — must always be sent.
        assert variables["needsApproval"] is False
        assert "dueAt" not in variables
        assert "saveToDraft" not in variables
        assert "metadata" not in variables

    def test_custom_scheduled_sets_due_at_iso8601_utc(self) -> None:
        # Arrange
        due_at = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
        request = _request(timing=Timing(mode="customScheduled", due_at=due_at))
        target = Target(service="x", caption="hi")

        # Act
        variables = build_create_post_variables(request, target, "chan-1", "https://cdn/video.mp4")

        # Assert
        assert variables["dueAt"] == "2030-01-01T12:00:00+00:00"

    def test_draft_sets_save_to_draft(self) -> None:
        # Arrange
        request = _request(draft=True)
        target = Target(service="x", caption="hi")

        # Act
        variables = build_create_post_variables(request, target, "chan-1", "https://cdn/video.mp4")

        # Assert
        assert variables["saveToDraft"] is True

    def test_thumbnail_offset(self) -> None:
        # Arrange
        request = _request(thumbnail_offset_ms=1500)
        target = Target(service="x", caption="hi")

        # Act
        variables = build_create_post_variables(request, target, "chan-1", "https://cdn/video.mp4")

        # Assert
        assert variables["assets"][0]["video"]["metadata"] == {"thumbnailOffset": 1500}

    def test_instagram_metadata(self) -> None:
        # Arrange
        request = _request(targets=[Target(service="instagram", caption="hi", share_to_feed=False)])
        target = request.targets[0]

        # Act
        variables = build_create_post_variables(request, target, "chan-1", "https://cdn/video.mp4")

        # Assert
        assert variables["metadata"]["instagram"] == {"type": "reel", "shouldShareToFeed": False}

    def test_tiktok_metadata_only_when_title_given(self) -> None:
        # Arrange
        request = _request(targets=[Target(service="tiktok", caption="hi")])
        target = request.targets[0]

        # Act
        variables_no_title = build_create_post_variables(request, target, "chan-1", "https://cdn/video.mp4")

        # Assert
        assert "metadata" not in variables_no_title

        # Arrange
        target_with_title = Target(service="tiktok", caption="hi", tiktok_title="My Title")

        # Act
        variables_with_title = build_create_post_variables(
            request, target_with_title, "chan-1", "https://cdn/video.mp4"
        )

        # Assert
        assert variables_with_title["metadata"]["tiktok"] == {"title": "My Title"}

    def test_x_has_no_service_metadata(self) -> None:
        # Arrange
        request = _request()
        target = request.targets[0]

        # Act
        variables = build_create_post_variables(request, target, "chan-1", "https://cdn/video.mp4")

        # Assert
        assert "metadata" not in variables

    def test_target_timing_override_wins_over_request_timing(self) -> None:
        # Arrange: request defaults to addToQueue, but this target opts into a
        # specific schedule via its own timing override
        due_at = datetime(2030, 6, 1, 9, 0, 0, tzinfo=UTC)
        request = _request(timing=Timing(mode="addToQueue"))
        target = Target(service="x", caption="hi", timing=Timing(mode="customScheduled", due_at=due_at))

        # Act
        variables = build_create_post_variables(request, target, "chan-1", "https://cdn/video.mp4")

        # Assert
        assert variables["mode"] == "customScheduled"
        assert variables["dueAt"] == "2030-06-01T09:00:00+00:00"


SUCCESS_RESPONSE = {
    "data": {
        "createPost": {
            "__typename": "PostActionSuccess",
            "post": {"id": "post-1", "status": "scheduled", "dueAt": None},
        }
    }
}

MUTATION_ERROR_RESPONSE = {
    "data": {
        "createPost": {
            # "MutationError" is the shared interface; the real __typename is
            # always one of its concrete implementers (NotFoundError,
            # UnauthorizedError, UnexpectedError, RestProxyError,
            # LimitReachedError, InvalidInputError, ...).
            "__typename": "NotFoundError",
            "message": "チャンネルが見つかりません",
        }
    }
}


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> BufferClient:
    return BufferClient(api_key="test-key", max_retries=4, sleep=sleeps.append)


class TestBufferClientTimeout:
    def test_default_timeout_is_30_seconds(self) -> None:
        # Arrange / Act
        client = BufferClient(api_key="test-key")

        # Assert: the CLI relies on this generous default.
        assert client._http.timeout == httpx.Timeout(30.0)

    def test_explicit_timeout_is_used_for_internal_http_client(self) -> None:
        # Arrange / Act: the Lambda factory needs a short timeout so a slow
        # Buffer response can't blow the whole invocation's deadline.
        client = BufferClient(api_key="test-key", timeout=8.0)

        # Assert
        assert client._http.timeout == httpx.Timeout(8.0)

    def test_explicit_http_client_is_not_overridden_by_timeout(self) -> None:
        # Arrange: when a caller supplies their own httpx.Client (as tests
        # do via respx), the timeout param must not construct a second one.
        http = httpx.Client(timeout=1.0)

        # Act
        client = BufferClient(api_key="test-key", http=http, timeout=8.0)

        # Assert
        assert client._http is http


class TestBufferClientCreatePost:
    @respx.mock
    def test_success_returns_ok_result(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        request = _request()

        # Act
        result = client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

        # Assert
        assert result.ok is True
        assert result.post_id == "post-1"
        assert result.status == "scheduled"

    @respx.mock
    def test_success_with_error_status_carries_post_error_message(self, client: BufferClient) -> None:
        # Arrange: Buffer accepted the mutation (ok) but the post itself ended
        # up in its "error" status immediately, with an explanation attached.
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "createPost": {
                            "__typename": "PostActionSuccess",
                            "post": {
                                "id": "post-1",
                                "status": "error",
                                "dueAt": None,
                                "error": {"message": "media rejected by TikTok"},
                            },
                        }
                    }
                },
            )
        )
        request = _request()

        # Act
        result = client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

        # Assert
        assert result.ok is True
        assert result.status == "error"
        assert result.error == "media rejected by TikTok"

    @respx.mock
    def test_mutation_error_returns_not_ok_without_raising(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(200, json=MUTATION_ERROR_RESPONSE))
        request = _request()

        # Act
        result = client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

        # Assert
        assert result.ok is False
        assert result.error == "チャンネルが見つかりません"

    @respx.mock
    def test_unauthorized_error_typename_raises_auth_error(self, client: BufferClient) -> None:
        # Arrange: a GraphQL-level (HTTP 200) authorization failure, distinct
        # from an HTTP 401/403 — both must be treated as the same auth error.
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {"createPost": {"__typename": "UnauthorizedError", "message": "invalid token"}}
                },
            )
        )
        request = _request()

        # Act / Assert
        with pytest.raises(BufferAuthError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

    @respx.mock
    def test_401_raises_auth_error(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(401, json={}))
        request = _request()

        # Act / Assert
        with pytest.raises(BufferAuthError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

    @respx.mock
    def test_top_level_graphql_errors_raise(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(200, json={"errors": [{"message": "bad query"}]})
        )
        request = _request()

        # Act / Assert
        with pytest.raises(BufferGraphQLError, match="bad query"):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

    @respx.mock
    def test_429_then_200_succeeds_and_sleeps(self, client: BufferClient, sleeps: list[float]) -> None:
        # Arrange
        route = respx.post(BUFFER_GRAPHQL_URL)
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0.01"}),
            httpx.Response(200, json=SUCCESS_RESPONSE),
        ]
        request = _request()

        # Act
        result = client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

        # Assert
        assert result.ok is True
        assert sleeps == [0.01]

    @respx.mock
    def test_exhausted_retries_raises_rate_limit_error(self, sleeps: list[float]) -> None:
        # Arrange
        client = BufferClient(api_key="test-key", max_retries=2, sleep=sleeps.append)
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(429))
        request = _request()

        # Act / Assert
        with pytest.raises(BufferRateLimitError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")
        assert len(sleeps) == 2

    @respx.mock
    def test_5xx_raises_ambiguous_without_retry(self, client: BufferClient, sleeps: list[float]) -> None:
        # Arrange: mutations must NOT be blindly retried on 5xx — the server may
        # have already processed the createPost before failing to respond.
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(500))
        request = _request()

        # Act / Assert
        with pytest.raises(BufferAmbiguousError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")
        assert sleeps == []

    @respx.mock
    def test_transport_error_raises_ambiguous_without_retry(
        self, client: BufferClient, sleeps: list[float]
    ) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(side_effect=httpx.ConnectError("boom"))
        request = _request()

        # Act / Assert
        with pytest.raises(BufferAmbiguousError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")
        assert sleeps == []

    @respx.mock
    def test_400_raises_http_error(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(400))
        request = _request()

        # Act / Assert
        with pytest.raises(BufferHTTPError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

    @respx.mock
    def test_unrecognized_typename_raises_protocol_error(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(200, json={"data": {"createPost": {"__typename": "Weird"}}})
        )
        request = _request()

        # Act / Assert
        with pytest.raises(BufferProtocolError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

    @respx.mock
    def test_success_without_post_id_raises_protocol_error(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"createPost": {"__typename": "PostActionSuccess", "post": {}}}},
            )
        )
        request = _request()

        # Act / Assert
        with pytest.raises(BufferProtocolError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")


class TestBufferClientRetrySleepCap:
    """max_retry_sleep_seconds (set for the Lambda-built client — see
    factory.build_publisher_without_uploads) bounds how long a retry will
    ever block, so a slow/rate-limited Buffer can't blow the Lambda's own
    deadline. 429 happens before a mutation is dispatched (see _post's
    comment), so raising instead of sleeping here is always safe — no
    createPost/editPost/deletePost has been sent yet."""

    @respx.mock
    def test_retry_after_exceeding_cap_raises_before_sleeping(self, sleeps: list[float]) -> None:
        # Arrange
        client = BufferClient(
            api_key="test-key", max_retries=4, sleep=sleeps.append, max_retry_sleep_seconds=5.0
        )
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "120"})
        )
        request = _request()

        # Act / Assert
        with pytest.raises(BufferRateLimitError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")
        assert sleeps == []

    @respx.mock
    def test_retry_after_under_cap_still_sleeps_and_retries(self, sleeps: list[float]) -> None:
        # Arrange
        client = BufferClient(
            api_key="test-key", max_retries=4, sleep=sleeps.append, max_retry_sleep_seconds=5.0
        )
        route = respx.post(BUFFER_GRAPHQL_URL)
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json=SUCCESS_RESPONSE),
        ]
        request = _request()

        # Act
        result = client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

        # Assert
        assert result.ok is True
        assert sleeps == [1.0]

    @respx.mock
    def test_no_cap_by_default_preserves_current_behavior(self, sleeps: list[float]) -> None:
        # Arrange: the CLI's client (no cap set) sleeps out the full
        # Retry-After, same as before this cap existed.
        client = BufferClient(api_key="test-key", max_retries=1, sleep=sleeps.append)
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "120"})
        )
        request = _request()

        # Act / Assert
        with pytest.raises(BufferRateLimitError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")
        assert sleeps == [120.0]

    @respx.mock
    def test_retry_after_exactly_at_cap_still_sleeps_and_retries(self, sleeps: list[float]) -> None:
        # Arrange: the guard is `delay > cap`, not `>=` — a delay exactly
        # equal to the cap must still sleep normally, not raise.
        client = BufferClient(
            api_key="test-key", max_retries=4, sleep=sleeps.append, max_retry_sleep_seconds=5.0
        )
        route = respx.post(BUFFER_GRAPHQL_URL)
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(200, json=SUCCESS_RESPONSE),
        ]
        request = _request()

        # Act
        result = client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")

        # Assert
        assert result.ok is True
        assert sleeps == [5.0]

    @respx.mock
    def test_retry_after_just_above_cap_raises_before_sleeping(self, sleeps: list[float]) -> None:
        # Arrange: the smallest possible overage (cap + 0.001s) must still
        # raise rather than sleep — the boundary is a strict `>`.
        client = BufferClient(
            api_key="test-key", max_retries=4, sleep=sleeps.append, max_retry_sleep_seconds=5.0
        )
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "5.001"})
        )
        request = _request()

        # Act / Assert
        with pytest.raises(BufferRateLimitError):
            client.create_post(request, request.targets[0], "chan-1", "https://cdn/video.mp4")
        assert sleeps == []

    @respx.mock
    def test_cap_applies_to_queries_too(self, sleeps: list[float]) -> None:
        # Arrange
        client = BufferClient(
            api_key="test-key", max_retries=4, sleep=sleeps.append, max_retry_sleep_seconds=5.0
        )
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "60"})
        )

        # Act / Assert
        with pytest.raises(BufferRateLimitError):
            client.get_post("post-1")
        assert sleeps == []


class TestBufferClientChannelsAndPost:
    @respx.mock
    def test_get_channels_returns_list(self, client: BufferClient) -> None:
        # Arrange
        channels = [{"id": "c1", "service": "tiktok", "name": "n", "displayName": "d"}]
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(200, json={"data": {"channels": channels}})
        )

        # Act
        result = client.get_channels("org-1")

        # Assert
        assert result == channels

    @respx.mock
    def test_get_channels_maps_twitter_service_to_x(self, client: BufferClient) -> None:
        # Arrange: Buffer's Service enum calls it "twitter"; our domain/DB use "x"
        channels = [
            {"id": "c1", "service": "twitter", "name": "n", "displayName": "d"},
            {"id": "c2", "service": "tiktok", "name": "n2", "displayName": "d2"},
        ]
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(200, json={"data": {"channels": channels}})
        )

        # Act
        result = client.get_channels("org-1")

        # Assert
        assert result[0]["service"] == "x"
        assert result[1]["service"] == "tiktok"

    @respx.mock
    def test_get_post_returns_dict(self, client: BufferClient) -> None:
        # Arrange
        post = {"id": "p1", "status": "sent", "externalLink": "https://x.com/1"}
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(200, json={"data": {"post": post}}))

        # Act
        result = client.get_post("p1")

        # Assert
        assert result == post

    @respx.mock
    def test_get_post_query_requests_text_for_sync_repair(self, client: BufferClient) -> None:
        # Arrange: sync() repairs a locally-stale caption from Buffer's own
        # text, so the query must request it.
        captured: dict[str, object] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            import json as jsonlib

            captured.update(jsonlib.loads(request.content))
            return httpx.Response(200, json={"data": {"post": {"id": "p1", "text": "remote caption"}}})

        respx.post(BUFFER_GRAPHQL_URL).mock(side_effect=_capture)

        # Act
        result = client.get_post("p1")

        # Assert
        assert "text" in str(captured["query"])
        assert result is not None
        assert result["text"] == "remote caption"

    @respx.mock
    def test_get_post_returns_none_for_explicit_null(self, client: BufferClient) -> None:
        # Arrange: GraphQL resolved cleanly, post just doesn't exist
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(200, json={"data": {"post": None}}))

        # Act
        result = client.get_post("p1")

        # Assert
        assert result is None

    @respx.mock
    def test_get_post_missing_field_raises_protocol_error(self, client: BufferClient) -> None:
        # Arrange: "post" key entirely absent is a different, malformed shape
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(200, json={"data": {}}))

        # Act / Assert
        with pytest.raises(BufferProtocolError):
            client.get_post("p1")

    @respx.mock
    def test_query_retries_on_5xx_then_succeeds(self, client: BufferClient, sleeps: list[float]) -> None:
        # Arrange: queries are safe to retry (no side effects)
        route = respx.post(BUFFER_GRAPHQL_URL)
        route.side_effect = [
            httpx.Response(500),
            httpx.Response(200, json={"data": {"post": {"id": "p1"}}}),
        ]

        # Act
        result = client.get_post("p1")

        # Assert
        assert result == {"id": "p1"}
        assert len(sleeps) == 1

    @respx.mock
    def test_query_transport_error_retries_then_raises(self, sleeps: list[float]) -> None:
        # Arrange
        client = BufferClient(api_key="test-key", max_retries=1, sleep=sleeps.append)
        respx.post(BUFFER_GRAPHQL_URL).mock(side_effect=httpx.ConnectError("boom"))

        # Act / Assert
        with pytest.raises(BufferTransportError):
            client.get_post("p1")
        assert len(sleeps) == 1


class TestBufferClientEditPost:
    @respx.mock
    def test_success_returns_ok_result(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "editPost": {
                            "__typename": "PostActionSuccess",
                            "post": {"id": "p1", "status": "scheduled", "dueAt": "2030-01-01T00:00:00+00:00"},
                        }
                    }
                },
            )
        )

        # Act
        result = client.edit_post("p1", text="new caption")

        # Assert
        assert result.ok is True
        assert result.post_id == "p1"
        assert result.due_at == "2030-01-01T00:00:00+00:00"

    @respx.mock
    def test_success_captures_caption_from_response(self, client: BufferClient) -> None:
        # Arrange: edit() must trust Buffer's own response as the source of
        # truth for the post's caption, not just echo back what was sent.
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "editPost": {
                            "__typename": "PostActionSuccess",
                            "post": {
                                "id": "p1",
                                "status": "scheduled",
                                "dueAt": None,
                                "text": "buffer's own caption",
                            },
                        }
                    }
                },
            )
        )

        # Act
        result = client.edit_post("p1", text="requested caption")

        # Assert
        assert result.caption == "buffer's own caption"

    @respx.mock
    def test_sends_only_given_fields(self, client: BufferClient) -> None:
        # Arrange
        captured: dict[str, object] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            import json as jsonlib

            captured.update(jsonlib.loads(request.content)["variables"]["input"])
            return httpx.Response(
                200,
                json={"data": {"editPost": {"__typename": "PostActionSuccess", "post": {"id": "p1"}}}},
            )

        respx.post(BUFFER_GRAPHQL_URL).mock(side_effect=_capture)
        due_at = datetime(2030, 2, 2, tzinfo=UTC)

        # Act
        client.edit_post("p1", due_at=due_at)

        # Assert
        assert captured == {"id": "p1", "dueAt": "2030-02-02T00:00:00+00:00"}

    @respx.mock
    def test_mutation_error_returns_not_ok(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"editPost": {"__typename": "NotFoundError", "message": "not found"}}},
            )
        )

        # Act
        result = client.edit_post("p1", text="x")

        # Assert
        assert result.ok is False
        assert result.error == "not found"

    @respx.mock
    def test_sends_only_caption_omits_due_at(self, client: BufferClient) -> None:
        # Arrange
        captured: dict[str, object] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            import json as jsonlib

            captured.update(jsonlib.loads(request.content)["variables"]["input"])
            return httpx.Response(
                200,
                json={"data": {"editPost": {"__typename": "PostActionSuccess", "post": {"id": "p1"}}}},
            )

        respx.post(BUFFER_GRAPHQL_URL).mock(side_effect=_capture)

        # Act
        client.edit_post("p1", text="only caption")

        # Assert
        assert captured == {"id": "p1", "text": "only caption"}


class TestBufferClientDeletePost:
    @respx.mock
    def test_success_returns_ok_result(self, client: BufferClient) -> None:
        # Arrange: DeletePostPayload = DeletePostSuccess { id } | VoidMutationError { message }
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"deletePost": {"__typename": "DeletePostSuccess", "id": "p1"}}},
            )
        )

        # Act
        result = client.delete_post("p1")

        # Assert
        assert result.ok is True
        assert result.post_id == "p1"

    @respx.mock
    def test_void_mutation_error_returns_not_ok(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"deletePost": {"__typename": "VoidMutationError", "message": "already sent"}}},
            )
        )

        # Act
        result = client.delete_post("p1")

        # Assert
        assert result.ok is False
        assert result.error == "already sent"

    @respx.mock
    def test_unauthorized_error_raises_auth_error(self, client: BufferClient) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {"deletePost": {"__typename": "UnauthorizedError", "message": "invalid token"}}
                },
            )
        )

        # Act / Assert
        with pytest.raises(BufferAuthError):
            client.delete_post("p1")

    @respx.mock
    def test_5xx_raises_ambiguous_without_retry(self, client: BufferClient, sleeps: list[float]) -> None:
        # Arrange
        respx.post(BUFFER_GRAPHQL_URL).mock(return_value=httpx.Response(503))

        # Act / Assert
        with pytest.raises(BufferAmbiguousError):
            client.delete_post("p1")
        assert sleeps == []
