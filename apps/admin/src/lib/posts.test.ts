import { beforeEach, describe, expect, it, vi } from "vitest";

const fromMock = vi.fn();

vi.mock("./supabase", () => ({
  getSupabaseClient: () => ({ from: fromMock }),
}));

// `./supabase` をモックしてから読み込む
const { fetchWeekPosts, fetchUpcomingPosts, fetchIsAdmin, UPCOMING_POSTS_LIMIT } = await import("./posts");

interface QueryBuilderMock {
  select: ReturnType<typeof vi.fn>;
  gte: ReturnType<typeof vi.fn>;
  lt: ReturnType<typeof vi.fn>;
  in: ReturnType<typeof vi.fn>;
  eq: ReturnType<typeof vi.fn>;
  order: ReturnType<typeof vi.fn>;
  limit: ReturnType<typeof vi.fn>;
  maybeSingle: ReturnType<typeof vi.fn>;
  then: (resolve: (value: { data: unknown; error: unknown }) => void) => void;
}

/**
 * Supabase の PostgREST クエリビルダーのチェーン呼び出しを模した最小限のモック。
 * 実物同様、ビルダー自身が thenable（`.then` を持つ）なので、途中のチェーンの長さに関わらず
 * `await` した時点の状態で解決する。
 */
function createQueryBuilder(terminalResult: { data: unknown; error: unknown }): QueryBuilderMock {
  const builder = {} as QueryBuilderMock;
  const chain = (): QueryBuilderMock => builder;
  builder.select = vi.fn(chain);
  builder.gte = vi.fn(chain);
  builder.lt = vi.fn(chain);
  builder.in = vi.fn(chain);
  builder.eq = vi.fn(chain);
  builder.order = vi.fn(chain);
  builder.limit = vi.fn(chain);
  builder.maybeSingle = vi.fn(() => Promise.resolve(terminalResult));
  builder.then = (resolve) => resolve(terminalResult);
  return builder;
}

describe("fetchWeekPosts", () => {
  beforeEach(() => {
    fromMock.mockReset();
  });

  it("queries posts for the given week range, embedding submissions and channels", async () => {
    const builder = createQueryBuilder({ data: [{ id: "p1" }], error: null });
    fromMock.mockReturnValue(builder);

    const weekStart = new Date("2026-09-14T00:00:00+09:00");
    const weekEnd = new Date("2026-09-21T00:00:00+09:00");
    const result = await fetchWeekPosts(weekStart, weekEnd);

    expect(fromMock).toHaveBeenCalledWith("posts");
    expect(builder.select).toHaveBeenCalledTimes(1);
    const selectArg = builder.select.mock.calls[0]?.[0] as string;
    expect(selectArg).toContain("submissions:submission_id");
    expect(selectArg).toContain("channels:channel_id");
    expect(builder.gte).toHaveBeenCalledWith("due_at", weekStart.toISOString());
    expect(builder.lt).toHaveBeenCalledWith("due_at", weekEnd.toISOString());
    expect(result).toEqual([{ id: "p1" }]);
  });

  it("orders deterministically by due_at then id (stable sort for equal due_at)", async () => {
    const builder = createQueryBuilder({ data: [], error: null });
    fromMock.mockReturnValue(builder);

    await fetchWeekPosts(new Date("2026-09-14T00:00:00+09:00"), new Date("2026-09-21T00:00:00+09:00"));

    expect(builder.order).toHaveBeenNthCalledWith(1, "due_at", { ascending: true });
    expect(builder.order).toHaveBeenNthCalledWith(2, "id", { ascending: true });
  });

  it("throws when Supabase returns an error", async () => {
    const builder = createQueryBuilder({ data: null, error: new Error("boom") });
    fromMock.mockReturnValue(builder);

    await expect(
      fetchWeekPosts(new Date("2026-09-14T00:00:00+09:00"), new Date("2026-09-21T00:00:00+09:00")),
    ).rejects.toThrow("boom");
  });
});

describe("fetchUpcomingPosts", () => {
  beforeEach(() => {
    fromMock.mockReset();
  });

  it("queries scheduled and draft posts due from now onward", async () => {
    const builder = createQueryBuilder({ data: [{ id: "p2" }], error: null });
    fromMock.mockReturnValue(builder);

    const now = new Date("2026-09-16T06:00:00+09:00");
    const result = await fetchUpcomingPosts(now);

    expect(fromMock).toHaveBeenCalledWith("posts");
    expect(builder.in).toHaveBeenCalledWith("status", ["scheduled", "draft"]);
    expect(builder.gte).toHaveBeenCalledWith("due_at", now.toISOString());
    expect(result).toEqual([{ id: "p2" }]);
  });

  it("orders deterministically by due_at then id, and caps the result with an explicit limit", async () => {
    const builder = createQueryBuilder({ data: [], error: null });
    fromMock.mockReturnValue(builder);

    await fetchUpcomingPosts(new Date("2026-09-16T06:00:00+09:00"));

    expect(builder.order).toHaveBeenNthCalledWith(1, "due_at", { ascending: true });
    expect(builder.order).toHaveBeenNthCalledWith(2, "id", { ascending: true });
    expect(builder.limit).toHaveBeenCalledWith(UPCOMING_POSTS_LIMIT);
  });
});

describe("fetchIsAdmin", () => {
  beforeEach(() => {
    fromMock.mockReset();
  });

  it("returns true when the user has a row in admin_users", async () => {
    const builder = createQueryBuilder({ data: { user_id: "u1" }, error: null });
    fromMock.mockReturnValue(builder);

    await expect(fetchIsAdmin("u1")).resolves.toBe(true);
    expect(fromMock).toHaveBeenCalledWith("admin_users");
  });

  it("returns false when the user has no row in admin_users", async () => {
    const builder = createQueryBuilder({ data: null, error: null });
    fromMock.mockReturnValue(builder);

    await expect(fetchIsAdmin("u2")).resolves.toBe(false);
  });
});
