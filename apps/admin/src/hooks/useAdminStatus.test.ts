import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchIsAdminMock = vi.fn();

vi.mock("@/lib/posts", () => ({
  fetchIsAdmin: fetchIsAdminMock,
}));

const { useAdminStatus } = await import("./useAdminStatus");

describe("useAdminStatus", () => {
  beforeEach(() => {
    fetchIsAdminMock.mockReset();
  });

  it("returns not-admin immediately when userId is null, without calling fetchIsAdmin", () => {
    const { result } = renderHook(() => useAdminStatus(null));
    expect(result.current).toBe("not-admin");
    expect(fetchIsAdminMock).not.toHaveBeenCalled();
  });

  it("returns loading while the check for the current userId is in flight", async () => {
    let resolve!: (value: boolean) => void;
    fetchIsAdminMock.mockReturnValue(new Promise<boolean>((res) => (resolve = res)));

    const { result } = renderHook(() => useAdminStatus("user-1"));
    expect(result.current).toBe("loading");

    resolve(true);
    await waitFor(() => expect(result.current).toBe("admin"));
  });

  it("returns admin/not-admin once resolved for the matching userId", async () => {
    fetchIsAdminMock.mockResolvedValue(false);
    const { result } = renderHook(() => useAdminStatus("user-1"));
    await waitFor(() => expect(result.current).toBe("not-admin"));
  });

  it("returns loading (not the previous user's stale result) as soon as userId changes", async () => {
    fetchIsAdminMock.mockResolvedValueOnce(true);
    const { result, rerender } = renderHook(({ userId }) => useAdminStatus(userId), {
      initialProps: { userId: "user-1" },
    });
    await waitFor(() => expect(result.current).toBe("admin"));

    let resolveNext!: (value: boolean) => void;
    fetchIsAdminMock.mockReturnValueOnce(new Promise<boolean>((res) => (resolveNext = res)));
    act(() => rerender({ userId: "user-2" }));

    // 新しい userId 用の結果がまだ届いていない間は、前のユーザーの "admin" を出し続けない
    expect(result.current).toBe("loading");

    resolveNext(false);
    await waitFor(() => expect(result.current).toBe("not-admin"));
  });

  it("returns error when fetchIsAdmin rejects", async () => {
    fetchIsAdminMock.mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useAdminStatus("user-1"));
    await waitFor(() => expect(result.current).toBe("error"));
  });

  it("falls back to not-admin immediately when signed out (userId -> null) while the check is still in flight", async () => {
    let resolve!: (value: boolean) => void;
    fetchIsAdminMock.mockReturnValue(new Promise<boolean>((res) => (resolve = res)));

    const { result, rerender } = renderHook(({ userId }) => useAdminStatus(userId), {
      initialProps: { userId: "user-1" as string | null },
    });
    expect(result.current).toBe("loading");

    act(() => rerender({ userId: null }));
    expect(result.current).toBe("not-admin");

    // the stale in-flight check for user-1 resolving later must not flip the status back
    resolve(true);
    await Promise.resolve();
    await Promise.resolve();
    expect(result.current).toBe("not-admin");
  });
});
