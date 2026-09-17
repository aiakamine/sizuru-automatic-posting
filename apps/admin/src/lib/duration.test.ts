import { describe, expect, it } from "vitest";
import { formatDurationSec } from "./duration";

describe("formatDurationSec", () => {
  it("formats seconds as m:ss", () => {
    expect(formatDurationSec(58)).toBe("0:58");
    expect(formatDurationSec(72)).toBe("1:12");
  });

  it("pads seconds under 10", () => {
    expect(formatDurationSec(65)).toBe("1:05");
  });

  it("returns null for null input", () => {
    expect(formatDurationSec(null)).toBeNull();
  });

  it("floors fractional seconds", () => {
    expect(formatDurationSec(58.9)).toBe("0:58");
  });
});
