import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useNow } from "./useNow";

describe("useNow", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-16T06:00:00+09:00"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns the current time on first render", () => {
    const { result } = renderHook(() => useNow(30_000));
    expect(result.current.toISOString()).toBe(new Date("2026-09-16T06:00:00+09:00").toISOString());
  });

  it("updates every intervalMs", () => {
    const { result } = renderHook(() => useNow(30_000));

    act(() => {
      vi.advanceTimersByTime(30_000);
    });

    expect(result.current.toISOString()).toBe(new Date("2026-09-16T06:00:30+09:00").toISOString());
  });

  it("refreshes immediately on window focus", () => {
    const { result } = renderHook(() => useNow(30_000));

    act(() => {
      vi.setSystemTime(new Date("2026-09-16T06:05:00+09:00"));
      window.dispatchEvent(new Event("focus"));
    });

    expect(result.current.toISOString()).toBe(new Date("2026-09-16T06:05:00+09:00").toISOString());
  });

  it("refreshes immediately when the tab becomes visible again", () => {
    const { result } = renderHook(() => useNow(30_000));

    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    act(() => {
      vi.setSystemTime(new Date("2026-09-16T06:07:00+09:00"));
      document.dispatchEvent(new Event("visibilitychange"));
    });

    expect(result.current.toISOString()).toBe(new Date("2026-09-16T06:07:00+09:00").toISOString());
  });

  it("cleans up its listeners and interval on unmount", () => {
    const addWindowSpy = vi.spyOn(window, "addEventListener");
    const removeWindowSpy = vi.spyOn(window, "removeEventListener");
    const { unmount } = renderHook(() => useNow(30_000));

    expect(addWindowSpy).toHaveBeenCalledWith("focus", expect.any(Function));
    unmount();
    expect(removeWindowSpy).toHaveBeenCalledWith("focus", expect.any(Function));
  });
});
