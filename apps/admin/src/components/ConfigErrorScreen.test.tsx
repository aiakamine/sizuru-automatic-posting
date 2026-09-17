import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ConfigErrorScreen } from "./ConfigErrorScreen";

describe("ConfigErrorScreen", () => {
  it("explains that required environment variables are missing", () => {
    render(<ConfigErrorScreen />);
    expect(screen.getByText(/環境変数が設定されていません/)).toBeInTheDocument();
  });

  it("lists the three required variable names so a deployer knows what to fix", () => {
    render(<ConfigErrorScreen />);
    expect(screen.getByText("NEXT_PUBLIC_SUPABASE_URL")).toBeInTheDocument();
    expect(screen.getByText("NEXT_PUBLIC_SUPABASE_ANON_KEY")).toBeInTheDocument();
    expect(screen.getByText("NEXT_PUBLIC_API_URL")).toBeInTheDocument();
  });
});
