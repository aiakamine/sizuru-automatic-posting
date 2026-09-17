/**
 * DB / API の型定義（契約層）。
 *
 * DB 側は `supabase/migrations/20260915000000_init_posting.sql` を正とする。
 * オフライン環境のため Supabase CLI の型自動生成が使えず、このファイルは手書きで
 * スキーマを厳密にミラーする。マイグレーションを変更したら必ずここも合わせて変更する。
 */

export type SnsService = "tiktok" | "instagram" | "x";

export type ShareMode = "shareNow" | "addToQueue" | "customScheduled";

/**
 * "pending" は Buffer へ未送信の内部状態（`posts_unsettled_idx` 参照。sync 完了前の
 * ごく短い間だけ存在しうる）。週カレンダー・PostDrawer には「確認中」として表示し、
 * 編集・キャンセルは不可（`isEditableStatus` が false を返す）。
 */
export type PostStatus =
  | "pending"
  | "draft"
  | "scheduled"
  | "sent"
  | "failed"
  | "canceled";

export interface ChannelRow {
  id: string;
  service: SnsService;
  buffer_channel_id: string;
  name: string;
  display_name: string | null;
  is_disconnected: boolean;
  is_queue_paused: boolean;
}

export interface SubmissionRow {
  id: string;
  title: string | null;
  video_url: string;
  thumbnail_url: string | null;
  duration_sec: number | null;
  approved_at: string;
  created_at: string;
}

export interface PostRow {
  id: string;
  submission_id: string;
  channel_id: string;
  caption: string;
  mode: ShareMode;
  due_at: string | null;
  is_draft: boolean;
  buffer_post_id: string | null;
  status: PostStatus;
  external_link: string | null;
  sent_at: string | null;
  error_message: string | null;
  retry_count: number;
  created_at: string;
  updated_at: string;
}

/** `posts` を `submissions` / `channels` と embed して取得した1件。カレンダー表示の基本単位。 */
export interface PostWithRelations extends PostRow {
  submissions: SubmissionRow;
  channels: ChannelRow;
}

export interface AdminUserRow {
  user_id: string;
  email: string;
  display_name: string | null;
}

// ---------------------------------------------------------------------------
// API 契約（Lambda `PATCH /posts/{post_id}` などのレスポンス）
// ---------------------------------------------------------------------------

export interface PostIssue {
  service: SnsService;
  level: "warning" | "error";
  message: string;
}

export interface PostActionResult {
  service: SnsService;
  status: PostStatus;
  post_id: string;
  buffer_post_id: string | null;
  due_at: string | null;
  error: string | null;
  issues: PostIssue[];
}

export interface SyncResult {
  sent: number;
  failed: number;
  canceled: number;
  unchanged: number;
  stuck_pending: number;
  errors?: string[];
  /** 対象件数が多すぎて今回の同期で全件処理しきれなかった場合に true。 */
  truncated?: boolean;
}

export type ApiErrorCode =
  | "validation"
  | "invalid_state"
  | "bad_request"
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "ambiguous"
  | "upstream"
  /** Buffer への反映自体は成功したが、こちらの DB への記録に失敗した（502）。 */
  | "db_write_failed"
  /** 未分類のサーバーエラー（500）。 */
  | "internal";

export interface ApiErrorBody {
  code: ApiErrorCode | string;
  message: string;
}
