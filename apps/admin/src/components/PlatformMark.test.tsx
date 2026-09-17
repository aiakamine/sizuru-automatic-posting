import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PlatformMark } from "./PlatformMark";

describe("PlatformMark", () => {
  it.each([
    ["tiktok", "TikTok"],
    ["instagram", "Instagram"],
    ["x", "X"],
  ] as const)("labels the icon for service=%s with an accessible name", (service, label) => {
    render(<PlatformMark service={service} />);
    expect(screen.getByRole("img", { name: label })).toBeInTheDocument();
  });

  it("applies the small-size data attribute when size='sm'", () => {
    render(<PlatformMark service="x" size="sm" />);
    expect(screen.getByRole("img", { name: "X" })).toHaveAttribute("data-size", "sm");
  });
});
