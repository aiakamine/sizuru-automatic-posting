import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WeekBoard } from "./WeekBoard";
import type { PostWithRelations } from "@/lib/types";

function buildPost(overrides: Partial<PostWithRelations>): PostWithRelations {
  return {
    id: overrides.id ?? "p1",
    submission_id: "s1",
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
    submissions: {
      id: "s1",
      title: "サンプル動画",
      video_url: "https://example.com/v.mp4",
      thumbnail_url: null,
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

const WEEK_START = new Date("2026-09-14T00:00:00+09:00"); // Monday

describe("WeekBoard", () => {
  it("places each post under its own JST day column", () => {
    const monday = buildPost({ id: "mon", due_at: "2026-09-14T09:00:00+09:00", caption: "月曜の投稿" });
    const wednesday = buildPost({ id: "wed", due_at: "2026-09-16T07:00:00+09:00", caption: "水曜の投稿" });
    const sunday = buildPost({ id: "sun", due_at: "2026-09-20T23:30:00+09:00", caption: "日曜の投稿" });

    render(
      <WeekBoard
        weekStart={WEEK_START}
        posts={[monday, wednesday, sunday]}
        selectedPostId={null}
        onSelectPost={vi.fn()}
        today={new Date("2026-09-15T12:00:00+09:00")}
      />,
    );

    const mondayColumn = screen.getByTestId("week-day-2026-09-14");
    const wednesdayColumn = screen.getByTestId("week-day-2026-09-16");
    const sundayColumn = screen.getByTestId("week-day-2026-09-20");

    expect(within(mondayColumn).getByText("月曜の投稿")).toBeInTheDocument();
    expect(within(wednesdayColumn).getByText("水曜の投稿")).toBeInTheDocument();
    expect(within(sundayColumn).getByText("日曜の投稿")).toBeInTheDocument();

    // 別の日カラムには表示されない
    expect(within(mondayColumn).queryByText("水曜の投稿")).not.toBeInTheDocument();
  });

  it("renders all 7 days of the week even when empty", () => {
    render(
      <WeekBoard
        weekStart={WEEK_START}
        posts={[]}
        selectedPostId={null}
        onSelectPost={vi.fn()}
        today={new Date("2026-09-15T12:00:00+09:00")}
      />,
    );

    for (const date of ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"]) {
      expect(screen.getByTestId(`week-day-${date}`)).toBeInTheDocument();
    }
  });

  it("does not render a post with a null due_at (queue post) in any day column", () => {
    const queued = buildPost({ id: "queued", due_at: null, caption: "キュー投稿" });
    render(
      <WeekBoard
        weekStart={WEEK_START}
        posts={[queued]}
        selectedPostId={null}
        onSelectPost={vi.fn()}
        today={new Date("2026-09-15T12:00:00+09:00")}
      />,
    );

    expect(screen.queryByText("キュー投稿")).not.toBeInTheDocument();
  });

  it("does not render a post whose due_at falls outside the displayed week", () => {
    const nextWeekPost = buildPost({ id: "next-week", due_at: "2026-09-21T09:00:00+09:00", caption: "来週の投稿" });
    render(
      <WeekBoard
        weekStart={WEEK_START}
        posts={[nextWeekPost]}
        selectedPostId={null}
        onSelectPost={vi.fn()}
        today={new Date("2026-09-15T12:00:00+09:00")}
      />,
    );

    expect(screen.queryByText("来週の投稿")).not.toBeInTheDocument();
  });

  it("keeps posts at the same due_at in their original (stable-sort) order within a day column", () => {
    const first = buildPost({ id: "first", due_at: "2026-09-16T07:00:00+09:00", caption: "先に並ぶ投稿" });
    const second = buildPost({ id: "second", due_at: "2026-09-16T07:00:00+09:00", caption: "後に並ぶ投稿" });

    render(
      <WeekBoard
        weekStart={WEEK_START}
        posts={[first, second]}
        selectedPostId={null}
        onSelectPost={vi.fn()}
        today={new Date("2026-09-15T12:00:00+09:00")}
      />,
    );

    const dayColumn = screen.getByTestId("week-day-2026-09-16");
    const captions = within(dayColumn)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(captions[0]).toContain("先に並ぶ投稿");
    expect(captions[1]).toContain("後に並ぶ投稿");
  });

  it("marks today's column", () => {
    render(
      <WeekBoard
        weekStart={WEEK_START}
        posts={[]}
        selectedPostId={null}
        onSelectPost={vi.fn()}
        today={new Date("2026-09-16T12:00:00+09:00")}
      />,
    );

    expect(screen.getByTestId("week-day-2026-09-16")).toHaveAttribute("data-today", "true");
    expect(screen.getByTestId("week-day-2026-09-14")).toHaveAttribute("data-today", "false");
  });
});
