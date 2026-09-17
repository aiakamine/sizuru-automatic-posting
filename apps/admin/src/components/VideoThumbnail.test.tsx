import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VideoThumbnail } from "./VideoThumbnail";

describe("VideoThumbnail", () => {
  it("renders an image when thumbnailUrl is provided", () => {
    render(<VideoThumbnail thumbnailUrl="https://example.com/thumb.jpg" durationSec={72} alt="サンプル動画" />);
    const img = screen.getByRole("img", { name: "サンプル動画" });
    expect(img).toHaveAttribute("src", "https://example.com/thumb.jpg");
  });

  it("shows a duration badge formatted as m:ss", () => {
    render(<VideoThumbnail thumbnailUrl="https://example.com/thumb.jpg" durationSec={72} alt="サンプル動画" />);
    expect(screen.getByText("1:12")).toBeInTheDocument();
  });

  it("omits the duration badge when durationSec is null", () => {
    render(<VideoThumbnail thumbnailUrl="https://example.com/thumb.jpg" durationSec={null} alt="サンプル動画" />);
    expect(screen.queryByText(/^\d+:\d{2}$/)).not.toBeInTheDocument();
  });

  it("renders a fallback placeholder (no <img>) when thumbnailUrl is null", () => {
    render(<VideoThumbnail thumbnailUrl={null} durationSec={30} alt="サンプル動画" />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByLabelText("サンプル動画")).toBeInTheDocument();
  });
});
