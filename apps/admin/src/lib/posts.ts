import { getSupabaseClient } from "./supabase";
import type { PostWithRelations } from "./types";

/**
 * `posts` から画面に必要な列だけを明示的に選択する（`select("*")` は使わない）。
 * DB スキーマ（`supabase/migrations/20260915000000_init_posting.sql`）とこの列挙、
 * および `src/lib/types.ts` の `PostWithRelations` を常に一致させる。
 */
const POST_SELECT = `
  id, submission_id, channel_id, caption, mode, due_at, is_draft, buffer_post_id,
  status, external_link, sent_at, error_message, retry_count, created_at, updated_at,
  submissions:submission_id ( id, title, video_url, thumbnail_url, duration_sec, approved_at, created_at ),
  channels:channel_id ( id, service, buffer_channel_id, name, display_name, is_disconnected, is_queue_paused )
`;

/** 「これから投稿される動画」帯の最大表示件数。無限に伸びないよう上限を設ける。 */
export const UPCOMING_POSTS_LIMIT = 50;

/** 週カレンダー表示用: `[weekStart, weekEnd)` に投稿予定日時を持つ投稿。 */
export async function fetchWeekPosts(weekStart: Date, weekEnd: Date): Promise<PostWithRelations[]> {
  const { data, error } = await getSupabaseClient()
    .from("posts")
    .select(POST_SELECT)
    .gte("due_at", weekStart.toISOString())
    .lt("due_at", weekEnd.toISOString())
    // due_at が同じ投稿（同一動画の複数媒体等）でも並び順が実行のたびに変わらないよう id を第2キーにする
    .order("due_at", { ascending: true })
    .order("id", { ascending: true });
  if (error) throw error;
  return (data ?? []) as unknown as PostWithRelations[];
}

/** 「これから投稿される動画」帯用: 予約済み・下書きで、今後の日時のもの。 */
export async function fetchUpcomingPosts(now: Date): Promise<PostWithRelations[]> {
  const { data, error } = await getSupabaseClient()
    .from("posts")
    .select(POST_SELECT)
    .in("status", ["scheduled", "draft"])
    .gte("due_at", now.toISOString())
    .order("due_at", { ascending: true })
    .order("id", { ascending: true })
    .limit(UPCOMING_POSTS_LIMIT);
  if (error) throw error;
  return (data ?? []) as unknown as PostWithRelations[];
}

/** ログイン中のユーザーが `admin_users` に登録されているか。 */
export async function fetchIsAdmin(userId: string): Promise<boolean> {
  const { data, error } = await getSupabaseClient()
    .from("admin_users")
    .select("user_id")
    .eq("user_id", userId)
    .maybeSingle();
  if (error) throw error;
  return data !== null;
}
