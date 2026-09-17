import { describe, expect, it } from "vitest";
import { describeSyncResult } from "./syncToast";
import type { SyncResult } from "./types";

const base: SyncResult = {
  sent: 1,
  failed: 0,
  canceled: 0,
  unchanged: 2,
  stuck_pending: 0,
};

describe("describeSyncResult", () => {
  it("reports success with counts when everything settled cleanly", () => {
    const result = describeSyncResult(base);
    expect(result.tone).toBe("success");
    expect(result.message).toContain("投稿済み1件");
    expect(result.message).toContain("失敗0件");
    expect(result.message).toContain("キャンセル0件");
  });

  it("reports a warning when errors is non-empty, even if all counts look fine", () => {
    const result = describeSyncResult({ ...base, errors: ["boom"] });
    expect(result.tone).toBe("warning");
    expect(result.message).toContain("1件のエラー");
  });

  it("reports a warning when stuck_pending is greater than 0", () => {
    const result = describeSyncResult({ ...base, stuck_pending: 3 });
    expect(result.tone).toBe("warning");
    expect(result.message).toContain("確認中のまま3件");
  });

  it("reports a warning when truncated is true", () => {
    const result = describeSyncResult({ ...base, truncated: true });
    expect(result.tone).toBe("warning");
    expect(result.message).toContain("一部のみ処理");
  });

  it("combines multiple warning conditions into one message", () => {
    const result = describeSyncResult({ ...base, errors: ["a", "b"], stuck_pending: 1, truncated: true });
    expect(result.tone).toBe("warning");
    expect(result.message).toContain("2件のエラー");
    expect(result.message).toContain("確認中のまま1件");
    expect(result.message).toContain("一部のみ処理");
  });
});
