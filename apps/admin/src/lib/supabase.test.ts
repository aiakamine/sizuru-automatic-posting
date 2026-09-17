import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const createClientMock = vi.fn(() => ({ marker: "fake-client" }));

vi.mock("@supabase/supabase-js", () => ({
  createClient: createClientMock,
}));

// このモジュールはクライアントをモジュールスコープでキャッシュするため、
// 各テストで独立させるためにテストごとに再インポートする。
async function importFreshSupabaseModule() {
  vi.resetModules();
  return import("./supabase");
}

describe("getSupabaseClient", () => {
  beforeEach(() => {
    createClientMock.mockClear();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("reads NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY literally and creates a client", async () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "https://project.supabase.co");
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "anon-key");

    const { getSupabaseClient } = await importFreshSupabaseModule();
    getSupabaseClient();

    expect(createClientMock).toHaveBeenCalledWith("https://project.supabase.co", "anon-key");
  });

  it("throws a clear error instead of silently creating an invalid client when env vars are missing", async () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", undefined);
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", undefined);

    const { getSupabaseClient } = await importFreshSupabaseModule();

    expect(() => getSupabaseClient()).toThrow(/NEXT_PUBLIC_SUPABASE_URL/);
    expect(createClientMock).not.toHaveBeenCalled();
  });

  it("caches the client across calls instead of creating a new one every time", async () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "https://project.supabase.co");
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "anon-key");

    const { getSupabaseClient } = await importFreshSupabaseModule();
    const first = getSupabaseClient();
    const second = getSupabaseClient();

    expect(first).toBe(second);
    expect(createClientMock).toHaveBeenCalledTimes(1);
  });
});
