import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { UpcomingStrip } from "./UpcomingStrip";
import type { PostWithRelations } from "@/lib/types";

function buildPost(overrides: Partial<PostWithRelations>): PostWithRelations {
  const submission = overrides.submissions ?? {
    id: overrides.submission_id ?? "s1",
    title: "サンプル動画",
    video_url: "https://example.com/v.mp4",
    thumbnail_url: null,
    duration_sec: 42,
    approved_at: "2026-09-14T22:10:00+09:00",
    created_at: "2026-09-14T22:00:00+09:00",
  };
  return {
    id: overrides.id ?? "p1",
    submission_id: submission.id,
    channel_id: "c1",
    caption: "投稿文",
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
    submissions: submission,
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

describe("UpcomingStrip", () => {
  it("groups posts belonging to the same submission into one card", () => {
    const submission = {
      id: "s1",
      title: "採用で見るたった1つのこと",
      video_url: "https://example.com/v.mp4",
      thumbnail_url: null,
      duration_sec: 42,
      approved_at: "2026-09-14T22:10:00+09:00",
      created_at: "2026-09-14T22:00:00+09:00",
    };
    const posts = [
      buildPost({ id: "p1", submissions: submission, due_at: "2026-09-15T12:00:00+09:00" }),
      buildPost({
        id: "p2",
        submissions: submission,
        due_at: "2026-09-15T18:00:00+09:00",
        channels: { id: "c2", service: "instagram", buffer_channel_id: "b2", name: "n2", display_name: null, is_disconnected: false, is_queue_paused: false },
      }),
    ];

    render(<UpcomingStrip posts={posts} onSelectPost={vi.fn()} />);

    expect(screen.getAllByText("採用で見るたった1つのこと")).toHaveLength(1);
    expect(screen.getByRole("img", { name: "TikTok" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Instagram" })).toBeInTheDocument();
  });

  it("shows the earliest scheduled time per group and calls onSelectPost with that post's id", async () => {
    const posts = [buildPost({ id: "p1", due_at: "2026-09-16T07:00:00+09:00" })];
    const onSelectPost = vi.fn();

    render(<UpcomingStrip posts={posts} onSelectPost={onSelectPost} />);

    expect(screen.getByText("次回 9/16 07:00")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button"));
    expect(onSelectPost).toHaveBeenCalledWith("p1");
  });

  it("shows an empty message when there is nothing upcoming", () => {
    render(<UpcomingStrip posts={[]} onSelectPost={vi.fn()} />);
    expect(screen.getByText("予約済み・下書きの投稿はありません")).toBeInTheDocument();
  });

  it("renders every group without truncation, even well past 50 (no client-side limit is implemented)", () => {
    const posts = Array.from({ length: 60 }, (_, i) =>
      buildPost({
        id: `p${i}`,
        submissions: {
          id: `s${i}`,
          title: `動画${i}`,
          video_url: "https://example.com/v.mp4",
          thumbnail_url: null,
          duration_sec: 30,
          approved_at: "2026-09-14T22:10:00+09:00",
          created_at: "2026-09-14T22:00:00+09:00",
        },
        submission_id: `s${i}`,
        due_at: `2026-09-${16 + (i % 10)}T07:00:00+09:00`,
      }),
    );

    render(<UpcomingStrip posts={posts} onSelectPost={vi.fn()} />);

    expect(screen.getAllByRole("button")).toHaveLength(60);
  });
});
