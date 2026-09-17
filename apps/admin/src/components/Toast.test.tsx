import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Toast } from "./Toast";

describe("Toast", () => {
  it("renders the message", () => {
    render(<Toast message="保存しました" />);
    expect(screen.getByText("保存しました")).toBeInTheDocument();
  });

  it("defaults to the neutral (success) tone", () => {
    render(<Toast message="保存しました" />);
    expect(screen.getByRole("status")).toHaveAttribute("data-tone", "success");
  });

  it("exposes a warning tone for messages that need attention", () => {
    render(<Toast message="要確認です" tone="warning" />);
    expect(screen.getByRole("status")).toHaveAttribute("data-tone", "warning");
  });
});
