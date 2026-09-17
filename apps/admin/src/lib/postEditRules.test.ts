import { describe, expect, it } from "vitest";
import { evaluateSave, isCancelable, isEditableStatus, isWithinLockoutWindow } from "./postEditRules";

const NOW = new Date("2026-09-16T06:00:00+09:00");

describe("isEditableStatus", () => {
  it("is true for scheduled and draft", () => {
    expect(isEditableStatus("scheduled")).toBe(true);
    expect(isEditableStatus("draft")).toBe(true);
  });

  it("is false for sent, failed, canceled, pending", () => {
    expect(isEditableStatus("sent")).toBe(false);
    expect(isEditableStatus("failed")).toBe(false);
    expect(isEditableStatus("canceled")).toBe(false);
    expect(isEditableStatus("pending")).toBe(false);
  });
});

describe("isWithinLockoutWindow", () => {
  it("is false when due_at is null", () => {
    expect(isWithinLockoutWindow(null, NOW)).toBe(false);
  });

  it("is false when due_at is more than 5 minutes in the future", () => {
    const dueAt = new Date(NOW.getTime() + 6 * 60 * 1000);
    expect(isWithinLockoutWindow(dueAt, NOW)).toBe(false);
  });

  it("is true when due_at is exactly 5 minutes in the future", () => {
    const dueAt = new Date(NOW.getTime() + 5 * 60 * 1000);
    expect(isWithinLockoutWindow(dueAt, NOW)).toBe(true);
  });

  it("is true when due_at is in the past (send window already open)", () => {
    const dueAt = new Date(NOW.getTime() - 60 * 1000);
    expect(isWithinLockoutWindow(dueAt, NOW)).toBe(true);
  });

  it("fails safe (treats as locked) when due_at is an invalid Date (NaN timestamp)", () => {
    expect(isWithinLockoutWindow(new Date("not-a-date"), NOW)).toBe(true);
  });
});

describe("evaluateSave", () => {
  const base = {
    status: "scheduled" as const,
    service: "tiktok" as const,
    captionChanged: false,
    dueAtChanged: false,
    originalDueAt: new Date(NOW.getTime() + 60 * 60 * 1000),
    nextDueAt: new Date(NOW.getTime() + 60 * 60 * 1000),
    now: NOW,
  };

  it("blocks with not_editable for a sent post", () => {
    const result = evaluateSave({
      ...base,
      status: "sent",
      captionChanged: true,
      nextCaption: "変更後",
    });
    expect(result).toEqual({ canSave: false, reasons: ["not_editable"] });
  });

  it("blocks with unchanged when neither caption nor due_at was touched", () => {
    const result = evaluateSave({
      ...base,
      nextCaption: "元の投稿文",
    });
    expect(result).toEqual({ canSave: false, reasons: ["unchanged"] });
  });

  it("does not report unchanged when only the caption changed (due_at field untouched)", () => {
    const result = evaluateSave({
      ...base,
      captionChanged: true,
      nextCaption: "変更後",
    });
    expect(result).toEqual({ canSave: true, reasons: [] });
  });

  it("does not report unchanged when only the due_at field was touched (even to the same value)", () => {
    const result = evaluateSave({
      ...base,
      dueAtChanged: true,
      nextCaption: "元の投稿文",
    });
    expect(result).toEqual({ canSave: true, reasons: [] });
  });

  it("blocks with over_limit when the caption exceeds the service limit", () => {
    const result = evaluateSave({
      ...base,
      captionChanged: true,
      nextCaption: "あ".repeat(2201),
    });
    expect(result.canSave).toBe(false);
    expect(result.reasons).toContain("over_limit");
  });

  it("blocks with past_time when due_at was touched and the new value is not in the future", () => {
    const result = evaluateSave({
      ...base,
      dueAtChanged: true,
      nextCaption: "元の投稿文",
      nextDueAt: new Date(NOW.getTime() - 1000),
    });
    expect(result.canSave).toBe(false);
    expect(result.reasons).toContain("past_time");
  });

  it("does not check past_time when due_at was not touched, even if nextDueAt happens to be stale/past", () => {
    // due_at フィールドを一切触っていない場合、その値を送信しないので過去日時チェックは無関係
    const result = evaluateSave({
      ...base,
      captionChanged: true,
      nextCaption: "変更後",
      nextDueAt: new Date(NOW.getTime() - 1000),
    });
    expect(result.reasons).not.toContain("past_time");
  });

  it("blocks with near_due_at when now is within 5 minutes of the original due_at, regardless of which field changed", () => {
    const result = evaluateSave({
      ...base,
      originalDueAt: new Date(NOW.getTime() + 2 * 60 * 1000),
      captionChanged: true,
      nextCaption: "変更後",
    });
    expect(result.canSave).toBe(false);
    expect(result.reasons).toContain("near_due_at");
  });

  it("blocks with invalid_date instead of silently allowing when due_at was touched and nextDueAt is NaN", () => {
    const result = evaluateSave({
      ...base,
      dueAtChanged: true,
      nextCaption: "元の投稿文",
      nextDueAt: new Date("not-a-date"),
    });
    expect(result).toEqual({ canSave: false, reasons: ["invalid_date"] });
  });

  it("blocks with invalid_date when originalDueAt is NaN", () => {
    const result = evaluateSave({
      ...base,
      originalDueAt: new Date("not-a-date"),
      captionChanged: true,
      nextCaption: "変更後",
    });
    expect(result).toEqual({ canSave: false, reasons: ["invalid_date"] });
  });

  it("allows saving when the caption changed, is within limit, and nothing is locked out", () => {
    const result = evaluateSave({
      ...base,
      captionChanged: true,
      nextCaption: "変更後の投稿文",
    });
    expect(result).toEqual({ canSave: true, reasons: [] });
  });

  it("can report multiple reasons at once", () => {
    const result = evaluateSave({
      ...base,
      captionChanged: true,
      dueAtChanged: true,
      nextCaption: "あ".repeat(2201),
      nextDueAt: new Date(NOW.getTime() - 1000),
    });
    expect(result.canSave).toBe(false);
    expect(result.reasons).toEqual(expect.arrayContaining(["over_limit", "past_time"]));
  });

  it("allows a due_at-less (queue) post to be saved: null originalDueAt is never in the lockout window", () => {
    const result = evaluateSave({
      ...base,
      originalDueAt: null,
      dueAtChanged: true,
      nextCaption: "元の投稿文",
      nextDueAt: new Date(NOW.getTime() + 60 * 60 * 1000),
    });
    expect(result).toEqual({ canSave: true, reasons: [] });
  });

  it("does not block on invalid_date for a queue post whose originalDueAt is null (not NaN)", () => {
    const result = evaluateSave({
      ...base,
      originalDueAt: null,
      captionChanged: true,
      nextCaption: "変更後",
    });
    expect(result.reasons).not.toContain("invalid_date");
  });

  it("allows a whitespace-only caption (no trimming/minimum-length rule is enforced)", () => {
    const result = evaluateSave({
      ...base,
      captionChanged: true,
      nextCaption: "   ",
    });
    expect(result).toEqual({ canSave: true, reasons: [] });
  });

  describe("caption limit boundary per service", () => {
    it.each([
      { service: "tiktok" as const, atLimit: "あ".repeat(2200), overLimit: "あ".repeat(2201) },
      { service: "instagram" as const, atLimit: "あ".repeat(2200), overLimit: "あ".repeat(2201) },
      // X weights full-width chars as 2; 140 * 2 = 280 (the X limit), 141 * 2 = 282 (over).
      { service: "x" as const, atLimit: "あ".repeat(140), overLimit: "あ".repeat(141) },
    ])("$service: does not block at exactly the limit, blocks with over_limit one character past it", ({ service, atLimit, overLimit }) => {
      const atLimitResult = evaluateSave({
        ...base,
        service,
        captionChanged: true,
        nextCaption: atLimit,
      });
      expect(atLimitResult.reasons).not.toContain("over_limit");

      const overLimitResult = evaluateSave({
        ...base,
        service,
        captionChanged: true,
        nextCaption: overLimit,
      });
      expect(overLimitResult.reasons).toContain("over_limit");
    });
  });
});

describe("isCancelable", () => {
  it("is true for a scheduled post well before due_at", () => {
    const dueAt = new Date(NOW.getTime() + 60 * 60 * 1000);
    expect(isCancelable("scheduled", dueAt, NOW)).toBe(true);
  });

  it("is false within the 5 minute lockout window", () => {
    const dueAt = new Date(NOW.getTime() + 60 * 1000);
    expect(isCancelable("scheduled", dueAt, NOW)).toBe(false);
  });

  it("is false for a non-editable status", () => {
    const dueAt = new Date(NOW.getTime() + 60 * 60 * 1000);
    expect(isCancelable("sent", dueAt, NOW)).toBe(false);
  });

  it("is true for a queue (due_at null) scheduled post, since a null due_at is never in the lockout window", () => {
    expect(isCancelable("scheduled", null, NOW)).toBe(true);
  });
});
