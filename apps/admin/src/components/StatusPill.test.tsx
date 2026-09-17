import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusPill } from "./StatusPill";

describe("StatusPill", () => {
  it.each([
    ["pending", "確認中"],
    ["scheduled", "予約済み"],
    ["sent", "投稿済み"],
    ["draft", "下書き"],
    ["failed", "エラー"],
    ["canceled", "キャンセル済み"],
  ] as const)("renders the Japanese label for status=%s", (status, label) => {
    render(<StatusPill status={status} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("exposes the status as a data attribute for styling", () => {
    render(<StatusPill status="failed" />);
    expect(screen.getByText("エラー")).toHaveAttribute("data-status", "failed");
  });
});
