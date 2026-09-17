import { describe, expect, it } from "vitest";
import { groupUpcomingBySubmission } from "./upcoming";
import type { PostWithRelations, SubmissionRow } from "./types";

function buildSubmission(overrides: Partial<SubmissionRow>): SubmissionRow {
  return {
    id: "s1",
    title: "サンプル動画",
    video_url: "https://example.com/v.mp4",
    thumbnail_url: null,
    duration_sec: 42,
    approved_at: "2026-09-14T22:10:00+09:00",
    created_at: "2026-09-14T22:00:00+09:00",
    ...overrides,
  };
}

function buildPost(overrides: Partial<PostWithRelations>): PostWithRelations {
  const submission = overrides.submissions ?? buildSubmission({ id: overrides.submission_id ?? "s1" });
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

describe("groupUpcomingBySubmission", () => {
  it("groups multiple posts of the same submission into a single entry", () => {
    const submission = buildSubmission({ id: "s1", title: "採用で見るたった1つのこと" });
    const posts = [
      buildPost({
        id: "p1",
        submissions: submission,
        channels: { id: "c1", service: "tiktok", buffer_channel_id: "b1", name: "n1", display_name: null, is_disconnected: false, is_queue_paused: false },
        due_at: "2026-09-15T12:00:00+09:00",
      }),
      buildPost({
        id: "p2",
        submissions: submission,
        channels: { id: "c2", service: "instagram", buffer_channel_id: "b2", name: "n2", display_name: null, is_disconnected: false, is_queue_paused: false },
        due_at: "2026-09-15T18:00:00+09:00",
      }),
    ];

    const groups = groupUpcomingBySubmission(posts);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.submission.id).toBe("s1");
    expect(groups[0]?.services).toEqual(["tiktok", "instagram"]);
  });

  it("uses the earliest due_at within the group as nextDueAt", () => {
    const submission = buildSubmission({ id: "s1" });
    const posts = [
      buildPost({ id: "p1", submissions: submission, due_at: "2026-09-18T07:00:00+09:00" }),
      buildPost({ id: "p2", submissions: submission, due_at: "2026-09-15T12:00:00+09:00" }),
    ];

    const groups = groupUpcomingBySubmission(posts);

    expect(groups[0]?.nextDueAt).toBe("2026-09-15T12:00:00+09:00");
    expect(groups[0]?.nextPostId).toBe("p2");
  });

  it("sorts groups by their nextDueAt ascending", () => {
    const later = buildSubmission({ id: "s-later" });
    const sooner = buildSubmission({ id: "s-sooner" });
    const posts = [
      buildPost({ id: "p1", submissions: later, submission_id: "s-later", due_at: "2026-09-20T07:00:00+09:00" }),
      buildPost({ id: "p2", submissions: sooner, submission_id: "s-sooner", due_at: "2026-09-15T07:00:00+09:00" }),
    ];

    const groups = groupUpcomingBySubmission(posts);

    expect(groups.map((g) => g.submission.id)).toEqual(["s-sooner", "s-later"]);
  });

  it("excludes posts that are not editable (sent, failed, canceled)", () => {
    const submission = buildSubmission({ id: "s1" });
    const posts = [
      buildPost({ id: "p1", submissions: submission, status: "sent", due_at: "2026-09-14T07:00:00+09:00" }),
      buildPost({ id: "p2", submissions: submission, status: "scheduled", due_at: "2026-09-16T07:00:00+09:00" }),
    ];

    const groups = groupUpcomingBySubmission(posts);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.nextDueAt).toBe("2026-09-16T07:00:00+09:00");
  });

  it("omits a submission entirely when it has no editable posts", () => {
    const submission = buildSubmission({ id: "s1" });
    const posts = [buildPost({ id: "p1", submissions: submission, status: "sent" })];

    expect(groupUpcomingBySubmission(posts)).toEqual([]);
  });

  it("ignores posts with a null due_at", () => {
    const submission = buildSubmission({ id: "s1" });
    const posts = [buildPost({ id: "p1", submissions: submission, due_at: null, status: "scheduled" })];

    expect(groupUpcomingBySubmission(posts)).toEqual([]);
  });
});
