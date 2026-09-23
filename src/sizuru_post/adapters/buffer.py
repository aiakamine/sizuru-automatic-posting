"""Buffer GraphQL API client and pure request-building helpers.

Schema notes: verified against the live schema via `BufferClient.introspect()`
(also exposed as `sizuru-post introspect`). Key points baked into this
module:
  - `Service` enum calls X/Twitter "twitter"; our domain/DB call it "x".
    `get_channels()` normalizes this at the boundary so nothing downstream
    ever sees "twitter".
  - `CreatePostInput.needsApproval` is non-null (`Boolean!`) — always sent.
  - `createPost`/`editPost` return `PostActionPayload` = `PostActionSuccess`
    | `NotFoundError` | `UnauthorizedError` | `UnexpectedError` |
    `RestProxyError` | `LimitReachedError` | `InvalidInputError`, all of
    which (except PostActionSuccess) implement the `MutationError`
    interface (`{ message }`). `deletePost` returns the smaller
    `DeletePostPayload` = `DeletePostSuccess { id }` | `VoidMutationError`
    (which also implements `MutationError`).
  - Remote `PostStatus` enum: draft, error, needs_approval, scheduled,
    sending, sent (mapped in publisher.py).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from sizuru_post.domain.models import PublishRequest, Target, resolve_timing

BUFFER_GRAPHQL_URL = "https://api.buffer.com/graphql"


class BufferError(Exception):
    """Base class for all Buffer adapter errors."""


class BufferAuthError(BufferError):
    """Raised on HTTP 401/403 — the API key is invalid or lacks access."""


class BufferRateLimitError(BufferError):
    """Raised when retries are exhausted while receiving HTTP 429."""


class BufferTransportError(BufferError):
    """Raised on network failures or exhausted retries against 5xx responses.

    Only raised for queries (channels/post/organizations/introspect), which
    are safe to retry since they have no side effects.
    """


class BufferHTTPError(BufferError):
    """Raised for any non-2xx status we don't otherwise handle (e.g. 400)."""


class BufferAmbiguousError(BufferError):
    """Raised when a mutation (createPost/editPost/deletePost) fails with an
    ambiguous outcome (5xx, transport error, or timeout).

    Mutations are NOT automatically retried on these failures — the server
    may already have processed the request before failing to respond, so a
    blind retry risks creating/editing/deleting the post twice. Callers
    must surface this ambiguity to a human rather than assume failure.
    """


class BufferProtocolError(BufferError):
    """Raised when a response doesn't match the expected shape: an
    unrecognized __typename, a "success" with no post.id, or a query
    response missing an expected field entirely."""


class BufferGraphQLError(BufferError):
    """Raised when the GraphQL response has a top-level `errors` array."""

    def __init__(self, message: str, errors: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.errors = errors


@dataclass(frozen=True)
class CreatePostResult:
    ok: bool
    post_id: str | None = None
    status: str | None = None
    due_at: str | None = None
    error: str | None = None
    # Buffer's own copy of the caption, when the response includes it (only
    # editPost currently requests `text`). edit() uses this as the source of
    # truth instead of blindly echoing back the requested caption.
    caption: str | None = None


# --- GraphQL documents -------------------------------------------------------

CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post {
        id
        status
        dueAt
        error {
          message
        }
      }
    }
    ... on MutationError {
      message
    }
  }
}
"""

EDIT_POST_MUTATION = """
mutation EditPost($input: EditPostInput!) {
  editPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post {
        id
        status
        dueAt
        text
        error {
          message
        }
      }
    }
    ... on MutationError {
      message
    }
  }
}
"""

# DeletePostPayload is a smaller union (DeletePostSuccess | VoidMutationError)
# than PostActionPayload, but VoidMutationError also implements MutationError
# so the same "... on MutationError { message }" fragment works here too.
DELETE_POST_MUTATION = """
mutation DeletePost($input: DeletePostInput!) {
  deletePost(input: $input) {
    __typename
    ... on DeletePostSuccess {
      id
    }
    ... on MutationError {
      message
    }
  }
}
"""

CHANNELS_QUERY = """
query Channels($input: ChannelsInput!) {
  channels(input: $input) {
    id
    name
    displayName
    service
    avatar
    isQueuePaused
    isDisconnected
  }
}
"""

POST_QUERY = """
query Post($input: PostInput!) {
  post(input: $input) {
    id
    status
    sentAt
    externalLink
    dueAt
    text
    error {
      message
    }
  }
}
"""

ORGANIZATIONS_QUERY = """
query Organizations {
  account {
    organizations {
      id
      name
    }
  }
}
"""

_INTROSPECT_TYPE_QUERY = """
query IntrospectType($name: String!) {
  __type(name: $name) {
    name
    kind
    fields {
      name
      type {
        name
        kind
        ofType {
          name
          kind
        }
      }
    }
    inputFields {
      name
      type {
        name
        kind
      }
    }
  }
}
"""

_INTROSPECT_TYPES = ("CreatePostInput", "Post")


# --- pure variable builder ----------------------------------------------------


def build_create_post_variables(
    request: PublishRequest,
    target: Target,
    channel_id: str,
    video_url: str,
) -> dict[str, Any]:
    """Build the `CreatePostInput` variables for one target. Pure, no I/O.

    Uses the target's own timing override if it has one, else the
    request-level default (see domain.models.resolve_timing).
    """
    timing = resolve_timing(request, target)
    variables: dict[str, Any] = {
        "text": target.caption,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": timing.mode,
        # CreatePostInput.needsApproval is Boolean! (non-null) — we don't use
        # Buffer's approval workflow, so this is always false.
        "needsApproval": False,
    }

    if timing.mode == "customScheduled":
        assert timing.due_at is not None  # noqa: S101 - guaranteed by Timing validator
        variables["dueAt"] = timing.due_at.astimezone(UTC).isoformat()

    if request.draft:
        variables["saveToDraft"] = True

    video_asset: dict[str, Any] = {"url": video_url}
    if request.thumbnail_offset_ms is not None:
        video_asset["metadata"] = {"thumbnailOffset": request.thumbnail_offset_ms}
    variables["assets"] = [{"video": video_asset}]

    metadata: dict[str, Any] = {}
    if target.service == "instagram":
        metadata["instagram"] = {"type": "reel", "shouldShareToFeed": target.share_to_feed}
    elif target.service == "tiktok" and target.tiktok_title:
        metadata["tiktok"] = {"title": target.tiktok_title}

    if metadata:
        variables["metadata"] = metadata

    return variables


# --- HTTP client ---------------------------------------------------------------


class BufferClient:
    def __init__(
        self,
        api_key: str,
        http: httpx.Client | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
        max_retry_sleep_seconds: float | None = None,
    ) -> None:
        self._api_key = api_key
        # `timeout` only applies when we build the internal client ourselves;
        # a caller-supplied `http` (e.g. tests wiring respx) keeps its own.
        self._http = http or httpx.Client(timeout=timeout)
        self._max_retries = max_retries
        self._sleep = sleep
        # None (the CLI's default) = unbounded, today's behavior. Set for
        # the Lambda-built client (factory.build_publisher_without_uploads)
        # so a long Retry-After can't blow the invocation's own deadline —
        # see _sleep_or_raise.
        self._max_retry_sleep_seconds = max_retry_sleep_seconds

    def create_post(
        self,
        request: PublishRequest,
        target: Target,
        channel_id: str,
        video_url: str,
    ) -> CreatePostResult:
        variables = build_create_post_variables(request, target, channel_id, video_url)
        data = self._post(CREATE_POST_MUTATION, {"input": variables}, is_mutation=True)
        return self._parse_post_action_result(data.get("createPost") or {})

    def edit_post(
        self, post_id: str, *, text: str | None = None, due_at: datetime | None = None
    ) -> CreatePostResult:
        """Edit a scheduled/draft post's text and/or due date. Only sends given fields."""
        variables: dict[str, Any] = {"id": post_id}
        if text is not None:
            variables["text"] = text
        if due_at is not None:
            variables["dueAt"] = due_at.astimezone(UTC).isoformat()

        data = self._post(EDIT_POST_MUTATION, {"input": variables}, is_mutation=True)
        return self._parse_post_action_result(data.get("editPost") or {})

    def delete_post(self, post_id: str) -> CreatePostResult:
        """Cancel a scheduled/draft post on Buffer's side."""
        data = self._post(DELETE_POST_MUTATION, {"input": {"id": post_id}}, is_mutation=True)
        return self._parse_delete_post_result(data.get("deletePost") or {})

    # Buffer's Service enum names X "twitter"; our domain/DB call it "x".
    _SERVICE_FROM_BUFFER = {"twitter": "x"}

    def get_channels(self, organization_id: str) -> list[dict[str, Any]]:
        data = self._post(CHANNELS_QUERY, {"input": {"organizationId": organization_id}})
        return [self._normalize_channel(c) for c in data.get("channels") or []]

    @classmethod
    def _normalize_channel(cls, channel: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(channel)
        service = normalized.get("service")
        if service in cls._SERVICE_FROM_BUFFER:
            normalized["service"] = cls._SERVICE_FROM_BUFFER[service]
        return normalized

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        """Fetch a post's status. Returns None for an explicit not-found
        (post: null); raises BufferProtocolError if the "post" field is
        missing entirely (a different, malformed shape)."""
        data = self._post(POST_QUERY, {"input": {"id": post_id}})
        if "post" not in data:
            raise BufferProtocolError("Buffer post クエリの応答が不正です（post フィールドがありません）")
        post = data["post"]
        return dict(post) if post is not None else None

    def get_organizations(self) -> list[dict[str, Any]]:
        data = self._post(ORGANIZATIONS_QUERY, {})
        account = data.get("account") or {}
        return list(account.get("organizations") or [])

    def introspect(self) -> dict[str, Any]:
        """Dump `__type` introspection for the types this client depends on."""
        return {name: self._post(_INTROSPECT_TYPE_QUERY, {"name": name}) for name in _INTROSPECT_TYPES}

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _parse_post_action_result(result: dict[str, Any]) -> CreatePostResult:
        """Parse createPost/editPost's PostActionPayload union.

        Success requires __typename == "PostActionSuccess" AND a post.id;
        anything else is either a MutationError-implementing failure (has a
        `message`, e.g. NotFoundError/UnexpectedError/InvalidInputError/...)
        or a malformed response.
        """
        typename = result.get("__typename")
        if typename == "PostActionSuccess":
            post = result.get("post") or {}
            post_id = post.get("id")
            if post_id:
                # Buffer can accept the mutation but the post itself may
                # already be in its "error" status; carry that explanation
                # through so callers mapping status="error" have a message.
                post_error = (post.get("error") or {}).get("message")
                return CreatePostResult(
                    ok=True,
                    post_id=post_id,
                    status=post.get("status"),
                    due_at=post.get("dueAt"),
                    error=post_error,
                    caption=post.get("text"),
                )
            raise BufferProtocolError("Buffer の応答が不正です（PostActionSuccessにpost.idがありません）")

        return BufferClient._parse_mutation_error(typename, result.get("message"))

    @staticmethod
    def _parse_delete_post_result(result: dict[str, Any]) -> CreatePostResult:
        """Parse deletePost's smaller DeletePostPayload union."""
        typename = result.get("__typename")
        if typename == "DeletePostSuccess":
            post_id = result.get("id")
            if post_id:
                return CreatePostResult(ok=True, post_id=post_id)
            raise BufferProtocolError("Buffer の応答が不正です（DeletePostSuccessにidがありません）")

        return BufferClient._parse_mutation_error(typename, result.get("message"))

    @staticmethod
    def _parse_mutation_error(typename: str | None, message: str | None) -> CreatePostResult:
        """Shared handling for the non-success branch of any mutation union.

        A `message` field present means the concrete type implements the
        MutationError interface (regardless of which specific error type it
        is) — GraphQL includes interface-fragment fields for any matching
        concrete type. UnauthorizedError specifically is surfaced as an
        auth error rather than a per-post failure, since it means the API
        key itself lacks access.
        """
        if typename == "UnauthorizedError":
            raise BufferAuthError(message or "Buffer APIキーが無効です。BUFFER_API_KEY を確認してください。")
        if message is not None:
            return CreatePostResult(ok=False, error=message)
        raise BufferProtocolError(
            f"Buffer の応答が不正です（__typename={typename!r}）。手動で投稿の有無を確認してください。"
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _post(self, query: str, variables: dict[str, Any], *, is_mutation: bool = False) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = self._http.post(
                    BUFFER_GRAPHQL_URL,
                    json={"query": query, "variables": variables},
                    headers=self._headers(),
                )
            except httpx.TransportError as exc:
                if is_mutation:
                    raise BufferAmbiguousError(f"Buffer への接続に失敗し、処理結果が不明です: {exc}") from exc
                attempt += 1
                if attempt > self._max_retries:
                    raise BufferTransportError(f"Buffer APIへの接続に失敗しました: {exc}") from exc
                self._sleep_or_raise(self._backoff_seconds(attempt))
                continue

            if response.status_code in (401, 403):
                message = "Buffer APIキーが無効です。BUFFER_API_KEY を確認してください。"
                raise BufferAuthError(message)

            if response.status_code == 429:
                # Rate-limited before execution — not processed, safe to
                # retry (or, if the wait would exceed our cap, to raise
                # instead) even for mutations: nothing has been dispatched
                # to Buffer yet either way.
                attempt += 1
                if attempt > self._max_retries:
                    raise BufferRateLimitError("Buffer APIのレート制限を超えました。")
                self._sleep_or_raise(self._retry_delay(response, attempt))
                continue

            if response.status_code >= 500:
                if is_mutation:
                    raise BufferAmbiguousError(
                        f"Buffer がエラーを返し、処理結果が不明です（HTTP {response.status_code}）"
                    )
                attempt += 1
                if attempt > self._max_retries:
                    raise BufferTransportError(
                        f"Buffer APIがエラーを返しました（HTTP {response.status_code}）"
                    )
                self._sleep_or_raise(self._retry_delay(response, attempt))
                continue

            if response.status_code >= 300:
                raise BufferHTTPError(
                    f"Buffer APIが予期しないエラーを返しました（HTTP {response.status_code}）"
                )

            payload: dict[str, Any] = response.json()
            errors = payload.get("errors")
            if errors:
                raise BufferGraphQLError(errors[0].get("message", "Buffer GraphQLエラー"), errors)
            return dict(payload.get("data") or {})

    def _backoff_seconds(self, attempt: int) -> float:
        return float(2 ** (attempt - 1))

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self._backoff_seconds(attempt)

    def _sleep_or_raise(self, delay: float) -> None:
        """Sleep for `delay` seconds before retrying, unless that would
        exceed `max_retry_sleep_seconds` (unset for the CLI's client —
        unbounded, today's behavior; set to a small cap for the
        Lambda-built client — see factory.build_publisher_without_uploads)
        — in that case raise immediately instead of blocking past the
        Lambda's own deadline.

        Only ever called from paths where no mutation has been dispatched
        to Buffer yet (429 before execution, or a retryable
        transport/5xx failure on a *query*, never a mutation — see the
        is_mutation branches in _post), so raising here instead of
        sleeping never creates a "did it actually happen" ambiguity.
        """
        if self._max_retry_sleep_seconds is not None and delay > self._max_retry_sleep_seconds:
            cap = self._max_retry_sleep_seconds
            raise BufferRateLimitError(
                f"Buffer への再試行待機が上限（{cap:.0f}秒）を超えるため中断しました"
                f"（{delay:.0f}秒の待機が必要）。リクエストはまだBufferに送信されていません。"
            )
        self._sleep(delay)
