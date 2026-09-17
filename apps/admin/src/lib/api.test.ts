import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, cancelPost, patchPost, setUnauthorizedHandler, syncPosts } from "./api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client", () => {
  const originalEnv = process.env.NEXT_PUBLIC_API_URL;

  beforeEach(() => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.com";
    setUnauthorizedHandler(() => {});
  });

  afterEach(() => {
    process.env.NEXT_PUBLIC_API_URL = originalEnv;
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  describe("patchPost", () => {
    it("sends a PATCH request with the Authorization header, JSON body, and returns the parsed result", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        jsonResponse(200, {
          result: {
            service: "tiktok",
            status: "scheduled",
            post_id: "p1",
            buffer_post_id: "b1",
            due_at: "2026-09-16T07:00:00+09:00",
            error: null,
            issues: [],
          },
        }),
      );
      vi.stubGlobal("fetch", fetchMock);

      const result = await patchPost(
        "p1",
        { caption: "新しい投稿文", due_at: "2026-09-16T07:00:00+09:00" },
        "token-abc",
      );

      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/posts/p1");
      expect(init.method).toBe("PATCH");
      expect(init.body).toBe(
        JSON.stringify({ caption: "新しい投稿文", due_at: "2026-09-16T07:00:00+09:00" }),
      );
      const headers = init.headers as Record<string, string>;
      expect(headers.Authorization).toBe("Bearer token-abc");
      expect(headers["Content-Type"]).toBe("application/json");
      expect(result.result.post_id).toBe("p1");
    });

    it("throws an ApiError with the code and message from the error body on failure", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          jsonResponse(400, { error: { code: "validation", message: "投稿文が長すぎます" } }),
        ),
      );

      await expect(patchPost("p1", { caption: "x" }, "token")).rejects.toMatchObject({
        status: 400,
        code: "validation",
        message: "投稿文が長すぎます",
      });
    });

    it("calls the unauthorized handler and throws on a 401 response", async () => {
      const onUnauthorized = vi.fn();
      setUnauthorizedHandler(onUnauthorized);
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          jsonResponse(401, { error: { code: "unauthorized", message: "セッションが切れました" } }),
        ),
      );

      await expect(patchPost("p1", {}, "token")).rejects.toBeInstanceOf(ApiError);
      expect(onUnauthorized).toHaveBeenCalledTimes(1);
    });

    it("falls back to a generic error when the response body is not JSON", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(new Response("not json", { status: 502 })),
      );

      await expect(patchPost("p1", {}, "token")).rejects.toMatchObject({
        status: 502,
        code: "unknown",
      });
    });

    it("propagates the raw error (not an ApiError) when fetch itself rejects on a network failure", async () => {
      vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

      await expect(patchPost("p1", {}, "token")).rejects.toThrow("Failed to fetch");
      await expect(patchPost("p1", {}, "token")).rejects.not.toBeInstanceOf(ApiError);
    });

    it("does not call the unauthorized handler on a network failure (only on an actual 401 response)", async () => {
      const onUnauthorized = vi.fn();
      setUnauthorizedHandler(onUnauthorized);
      vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

      await expect(patchPost("p1", {}, "token")).rejects.toThrow();
      expect(onUnauthorized).not.toHaveBeenCalled();
    });

    it.each([
      "validation",
      "invalid_state",
      "bad_request",
      "unauthorized",
      "forbidden",
      "not_found",
      "ambiguous",
      "upstream",
      "db_write_failed",
      "internal",
    ] as const)("preserves the %s error code from the response body on the thrown ApiError", async (code) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(jsonResponse(400, { error: { code, message: "エラーです" } })),
      );

      await expect(patchPost("p1", {}, "token")).rejects.toMatchObject({ code });
    });
  });

  describe("cancelPost", () => {
    it("sends a POST request to /posts/{id}/cancel", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        jsonResponse(200, {
          result: {
            service: "tiktok",
            status: "canceled",
            post_id: "p1",
            buffer_post_id: "b1",
            due_at: null,
            error: null,
            issues: [],
          },
        }),
      );
      vi.stubGlobal("fetch", fetchMock);

      await cancelPost("p1", "token-abc");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/posts/p1/cancel");
      expect(init.method).toBe("POST");
    });
  });

  describe("syncPosts", () => {
    it("sends a POST request to /sync and returns the sync summary", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        jsonResponse(200, {
          sync: { sent: 1, failed: 0, canceled: 0, unchanged: 2, stuck_pending: 0 },
        }),
      );
      vi.stubGlobal("fetch", fetchMock);

      const result = await syncPosts("token-abc");

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/sync");
      expect(init.method).toBe("POST");
      expect(result.sync.sent).toBe(1);
    });
  });
});
