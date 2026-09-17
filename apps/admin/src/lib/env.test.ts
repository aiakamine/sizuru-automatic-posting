import { afterEach, describe, expect, it, vi } from "vitest";
import { getApiUrl, getAppEnv, getSupabaseAnonKey, getSupabaseUrl } from "./env";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("getSupabaseUrl / getSupabaseAnonKey / getApiUrl", () => {
  it("reads each NEXT_PUBLIC_* env var by its literal name", () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "https://project.supabase.co");
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "anon-key");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");

    expect(getSupabaseUrl()).toBe("https://project.supabase.co");
    expect(getSupabaseAnonKey()).toBe("anon-key");
    expect(getApiUrl()).toBe("https://api.example.com");
  });

  it("returns undefined when the variable is not set", () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", undefined);
    expect(getSupabaseUrl()).toBeUndefined();
  });
});

describe("getAppEnv", () => {
  it("returns null when any of the three required variables is missing", () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "https://project.supabase.co");
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "anon-key");
    vi.stubEnv("NEXT_PUBLIC_API_URL", undefined);

    expect(getAppEnv()).toBeNull();
  });

  it("returns the combined config when all three are present", () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "https://project.supabase.co");
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "anon-key");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");

    expect(getAppEnv()).toEqual({
      supabaseUrl: "https://project.supabase.co",
      supabaseAnonKey: "anon-key",
      apiUrl: "https://api.example.com",
    });
  });
});
