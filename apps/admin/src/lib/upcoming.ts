import { isEditableStatus } from "./postEditRules";
import type { PostWithRelations, SnsService, SubmissionRow } from "./types";

export interface UpcomingGroup {
  submission: SubmissionRow;
  /** グループ内で最も早い予約日時（ISO 文字列, due_at のまま）。 */
  nextDueAt: string;
  /** 次回投稿（`nextDueAt` を持つ投稿）の id。クリックで PostDrawer を開く対象。 */
  nextPostId: string;
  /** そのグループに含まれる編集可能な投稿の媒体一覧（投稿順）。 */
  services: SnsService[];
}

/**
 * 「これから投稿される動画」帯用に、予約済み・下書きの投稿を動画（submission）単位でまとめる。
 * 投稿済み・失敗・キャンセル済みの投稿は対象外。
 */
export function groupUpcomingBySubmission(posts: PostWithRelations[]): UpcomingGroup[] {
  const editablePosts = posts.filter(
    (post): post is PostWithRelations & { due_at: string } =>
      post.due_at !== null && isEditableStatus(post.status),
  );

  const groups = new Map<string, { submission: SubmissionRow; posts: (PostWithRelations & { due_at: string })[] }>();
  for (const post of editablePosts) {
    const existing = groups.get(post.submission_id);
    if (existing) {
      existing.posts.push(post);
    } else {
      groups.set(post.submission_id, { submission: post.submissions, posts: [post] });
    }
  }

  return Array.from(groups.values())
    .map(({ submission, posts: groupPosts }) => {
      const sorted = [...groupPosts].sort((a, b) => a.due_at.localeCompare(b.due_at));
      return {
        submission,
        nextDueAt: sorted[0]!.due_at,
        nextPostId: sorted[0]!.id,
        services: sorted.map((post) => post.channels.service),
      };
    })
    .sort((a, b) => a.nextDueAt.localeCompare(b.nextDueAt));
}
