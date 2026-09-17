import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PostCard } from "./PostCard";
import type { PostWithRelations } from "@/lib/types";

function buildPost(overrides: Partial<PostWithRelations> = {}): PostWithRelations {
  return {
    id: "p1",
    submission_id: "s1",
    channel_id: "c1",
    caption: "起業1年目、300万円溶かしました。",
    mode: "customScheduled",
    due_at: "2026-09-16T07:00:00+09:00",
    is_draft: false,
    buffer_post_id: "b1",
    status: "scheduled",
    external_link: null,
    sent_at: null,
    error_message: null,
    retry_count: 0,
    created_at: "2026-09-15T00:00:00+09:00",
    updated_at: "2026-09-15T00:00:00+09:00",
    submissions: {
      id: "s1",
      title: "起業1年目で失敗した話",
      video_url: "https://example.com/v.mp4",
      thumbnail_url: "https://example.com/thumb.jpg",
      duration_sec: 58,
      approved_at: "2026-09-13T21:04:00+09:00",
      created_at: "2026-09-13T21:00:00+09:00",
    },
    channels: {
      id: "c1",
      service: "tiktok",
      buffer_channel_id: "buf-1",
      name: "sizuru_clips",
      display_name: "@sizuru_clips",
      is_disconnected: false,
      is_queue_paused: false,
    },
    ...overrides,
  };
}

describe("PostCard", () => {
  it("shows the JST time, platform, title, caption preview, and status", () => {
    render(<PostCard post={buildPost()} isSelected={false} onSelect={vi.fn()} />);

    expect(screen.getByText("07:00")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "TikTok" })).toBeInTheDocument();
    expect(screen.getByText("起業1年目で失敗した話")).toBeInTheDocument();
    expect(screen.getByText("起業1年目、300万円溶かしました。")).toBeInTheDocument();
    expect(screen.getByText("予約済み")).toBeInTheDocument();
  });

  it("calls onSelect with the post id when clicked", async () => {
    const onSelect = vi.fn();
    render(<PostCard post={buildPost()} isSelected={false} onSelect={onSelect} />);

    await userEvent.click(screen.getByRole("button"));

    expect(onSelect).toHaveBeenCalledWith("p1");
  });

  it("marks the button as pressed when selected", () => {
    render(<PostCard post={buildPost()} isSelected={true} onSelect={vi.fn()} />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "true");
  });

  it("renders a neutral pill for a pending post instead of crashing on an unmapped status", () => {
    render(<PostCard post={buildPost({ status: "pending" })} isSelected={false} onSelect={vi.fn()} />);
    expect(screen.getByText("確認中")).toBeInTheDocument();
  });

  it("falls back to a placeholder title when the submission has no title", () => {
    render(
      <PostCard
        post={buildPost({ submissions: { ...buildPost().submissions, title: null } })}
        isSelected={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("(タイトル未設定)")).toBeInTheDocument();
  });

  it("renders a fallback thumbnail (no <img>) when the submission has no thumbnail_url", () => {
    const post = buildPost({ submissions: { ...buildPost().submissions, thumbnail_url: null } });
    render(<PostCard post={post} isSelected={false} onSelect={vi.fn()} />);
    // the platform mark is also `role="img"`, so scope to the thumbnail's own accessible name.
    expect(screen.queryByRole("img", { name: post.submissions.title ?? "" })).not.toBeInTheDocument();
  });

  it("shows --:-- as the time placeholder when due_at is null (queue post)", () => {
    render(<PostCard post={buildPost({ due_at: null })} isSelected={false} onSelect={vi.fn()} />);
    expect(screen.getByText("--:--")).toBeInTheDocument();
  });
});
