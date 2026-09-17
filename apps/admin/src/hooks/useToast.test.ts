import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useToast } from "./useToast";

describe("useToast", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts with no message", () => {
    const { result } = renderHook(() => useToast());
    expect(result.current.message).toBeNull();
  });

  it("defaults tone to success when calling show() with just a message", () => {
    const { result } = renderHook(() => useToast());
    act(() => result.current.show("保存しました"));
    expect(result.current.message).toBe("保存しました");
    expect(result.current.tone).toBe("success");
  });

  it("accepts an explicit tone", () => {
    const { result } = renderHook(() => useToast());
    act(() => result.current.show("要確認です", "warning"));
    expect(result.current.tone).toBe("warning");
  });

  it("clears the message after the duration elapses", () => {
    const { result } = renderHook(() => useToast(2400));
    act(() => result.current.show("保存しました"));
    act(() => vi.advanceTimersByTime(2400));
    expect(result.current.message).toBeNull();
  });
});
