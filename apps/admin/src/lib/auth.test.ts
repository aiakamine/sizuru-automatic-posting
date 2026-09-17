import { beforeEach, describe, expect, it, vi } from "vitest";

const getSessionMock = vi.fn();
const getClaimsMock = vi.fn();
const onAuthStateChangeMock = vi.fn();
const signInWithOtpMock = vi.fn();
const signOutMock = vi.fn();
const unsubscribeMock = vi.fn();

vi.mock("./supabase", () => ({
  getSupabaseClient: () => ({
    auth: {
      getSession: getSessionMock,
      getClaims: getClaimsMock,
      onAuthStateChange: onAuthStateChangeMock,
      signInWithOtp: signInWithOtpMock,
      signOut: signOutMock,
    },
  }),
}));

const { getCurrentSession, subscribeToSession, signInWithMagicLink, signOut } = await import("./auth");

/** テストから任意のタイミングで解決/拒否できる Promise を作る。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("getCurrentSession", () => {
  beforeEach(() => {
    getSessionMock.mockReset();
    getClaimsMock.mockReset();
  });

  it("returns null when there is no active session", async () => {
    getSessionMock.mockResolvedValue({ data: { session: null }, error: null });

    await expect(getCurrentSession()).resolves.toBeNull();
    expect(getClaimsMock).not.toHaveBeenCalled();
  });

  it("verifies the captured access token explicitly via getClaims(token), not the implicit current session", async () => {
    getSessionMock.mockResolvedValue({
      data: { session: { access_token: "token-abc" } },
      error: null,
    });
    getClaimsMock.mockResolvedValue({
      data: { claims: { sub: "user-1", email: "kiyoto_hata@levela.co.jp" } },
      error: null,
    });

    await expect(getCurrentSession()).resolves.toEqual({
      accessToken: "token-abc",
      userId: "user-1",
      email: "kiyoto_hata@levela.co.jp",
    });
    expect(getClaimsMock).toHaveBeenCalledWith("token-abc");
  });

  it("returns null when getClaims() fails", async () => {
    getSessionMock.mockResolvedValue({
      data: { session: { access_token: "token-abc" } },
      error: null,
    });
    getClaimsMock.mockResolvedValue({ data: null, error: new Error("bad jwt") });

    await expect(getCurrentSession()).resolves.toBeNull();
  });

  it("resolves to null instead of rejecting when getSession() itself rejects", async () => {
    getSessionMock.mockRejectedValue(new Error("network down"));

    await expect(getCurrentSession()).resolves.toBeNull();
  });

  it("resolves to null instead of rejecting when getClaims() itself rejects", async () => {
    getSessionMock.mockResolvedValue({
      data: { session: { access_token: "token-abc" } },
      error: null,
    });
    getClaimsMock.mockRejectedValue(new Error("network down"));

    await expect(getCurrentSession()).resolves.toBeNull();
  });
});

describe("subscribeToSession", () => {
  let authEventHandler: (event: string, session: unknown) => void = () => {};

  beforeEach(() => {
    getSessionMock.mockReset();
    getClaimsMock.mockReset();
    onAuthStateChangeMock.mockReset();
    unsubscribeMock.mockReset();
    onAuthStateChangeMock.mockImplementation((cb: typeof authEventHandler) => {
      authEventHandler = cb;
      return { data: { subscription: { unsubscribe: unsubscribeMock } } };
    });
    // デフォルトでは初回ロードは「未ログイン」として即解決しておく（各テストで上書き可）。
    getSessionMock.mockResolvedValue({ data: { session: null }, error: null });
  });

  it("verifies each auth event's session using its own captured access token via getClaims(token)", async () => {
    getClaimsMock.mockResolvedValue({
      data: { claims: { sub: "user-1", email: "a@example.com" } },
      error: null,
    });
    const callback = vi.fn();
    subscribeToSession(callback);

    authEventHandler("SIGNED_IN", { access_token: "token-xyz" });
    await vi.waitFor(() => expect(getClaimsMock).toHaveBeenCalledWith("token-xyz"));
  });

  it("notifies null immediately when an auth event has no session (sign-out)", () => {
    const callback = vi.fn();
    subscribeToSession(callback);

    authEventHandler("SIGNED_OUT", null);

    expect(callback).toHaveBeenCalledWith(null);
  });

  it("drops a stale result when a newer auth event has already superseded it (generation guard)", async () => {
    const first = deferred<{ data: { claims: Record<string, unknown> } | null; error: unknown }>();
    const second = deferred<{ data: { claims: Record<string, unknown> } | null; error: unknown }>();
    getClaimsMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    const callback = vi.fn();
    subscribeToSession(callback);

    authEventHandler("SIGNED_IN", { access_token: "token-old" });
    authEventHandler("SIGNED_IN", { access_token: "token-new" });

    // 新しい方（token-new）を先に解決させる
    second.resolve({ data: { claims: { sub: "user-new", email: null } }, error: null });
    await vi.waitFor(() => expect(callback).toHaveBeenCalledWith(expect.objectContaining({ userId: "user-new" })));

    callback.mockClear();
    // 古い方（token-old）が遅れて解決しても、もう反映されない
    first.resolve({ data: { claims: { sub: "user-old", email: null } }, error: null });
    await Promise.resolve();
    await Promise.resolve();
    expect(callback).not.toHaveBeenCalled();
  });

  it("does not call back with a stale initial load result after a newer auth event already fired", async () => {
    const initial = deferred<{ data: { session: { access_token: string } | null }; error: unknown }>();
    getSessionMock.mockReturnValueOnce(initial.promise);
    getClaimsMock.mockResolvedValue({
      data: { claims: { sub: "user-newer", email: null } },
      error: null,
    });

    const callback = vi.fn();
    subscribeToSession(callback);

    // 初回ロードが解決する前に、購読イベントで新しいセッションが届く
    authEventHandler("SIGNED_IN", { access_token: "token-newer" });
    await vi.waitFor(() => expect(callback).toHaveBeenCalledWith(expect.objectContaining({ userId: "user-newer" })));

    callback.mockClear();
    // 初回ロードが遅れて解決しても、新しいイベントの結果を上書きしない
    initial.resolve({ data: { session: { access_token: "token-stale" } }, error: null });
    await Promise.resolve();
    await Promise.resolve();
    expect(callback).not.toHaveBeenCalled();
  });

  it("ignores results that resolve after unsubscribe", async () => {
    const pending = deferred<{ data: { claims: Record<string, unknown> } | null; error: unknown }>();
    getClaimsMock.mockReturnValueOnce(pending.promise);

    const callback = vi.fn();
    const unsubscribe = subscribeToSession(callback);

    authEventHandler("SIGNED_IN", { access_token: "token-abc" });
    unsubscribe();
    expect(unsubscribeMock).toHaveBeenCalledTimes(1);

    pending.resolve({ data: { claims: { sub: "user-1", email: null } }, error: null });
    await Promise.resolve();
    await Promise.resolve();
    expect(callback).not.toHaveBeenCalled();
  });

  it("notifies null instead of throwing when getClaims() rejects for an auth event", async () => {
    getClaimsMock.mockRejectedValueOnce(new Error("network down"));
    const callback = vi.fn();
    subscribeToSession(callback);

    authEventHandler("SIGNED_IN", { access_token: "token-abc" });
    await vi.waitFor(() => expect(callback).toHaveBeenCalledWith(null));
  });
});

describe("signInWithMagicLink", () => {
  beforeEach(() => {
    signInWithOtpMock.mockReset();
  });

  it("sends a magic link with the given redirect and returns no error on success", async () => {
    signInWithOtpMock.mockResolvedValue({ error: null });

    const result = await signInWithMagicLink("a@example.com", "https://app.example.com/calendar/");

    expect(signInWithOtpMock).toHaveBeenCalledWith({
      email: "a@example.com",
      options: { emailRedirectTo: "https://app.example.com/calendar/" },
    });
    expect(result).toEqual({ error: null });
  });

  it("returns the error message on failure", async () => {
    signInWithOtpMock.mockResolvedValue({ error: { message: "rate limited" } });

    const result = await signInWithMagicLink("a@example.com", "https://app.example.com/calendar/");

    expect(result).toEqual({ error: "rate limited" });
  });

  it("returns a generic error message instead of throwing when signInWithOtp itself rejects", async () => {
    signInWithOtpMock.mockRejectedValue(new Error("network down"));

    const result = await signInWithMagicLink("a@example.com", "https://app.example.com/calendar/");

    expect(result.error).toBeTruthy();
  });
});

describe("signOut", () => {
  it("calls supabase auth signOut", async () => {
    signOutMock.mockResolvedValue({ error: null });
    await signOut();
    expect(signOutMock).toHaveBeenCalledTimes(1);
  });
});
