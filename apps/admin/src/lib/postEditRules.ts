import { captionLength, captionLimit } from "./xWeightedLength";
import type { PostStatus, SnsService } from "./types";

/** 編集・キャンセルが可能な状態（予約済み・下書きのみ）。 */
export function isEditableStatus(status: PostStatus): boolean {
  return status === "scheduled" || status === "draft";
}

/** Buffer が送信中の可能性がある時間帯（予約時刻の5分前以降）。 */
export const EDIT_LOCKOUT_MS = 5 * 60 * 1000;

/**
 * `dueAt` の5分前以降（＝送信中の可能性がある）かどうか。
 * `dueAt` が不正な Date（NaN タイムスタンプ）の場合、`NaN <= x` は常に false になり
 * 「ロックされていない」と誤判定してしまうため、フェイルセーフに「ロックされている」を返す。
 */
export function isWithinLockoutWindow(dueAt: Date | null, now: Date): boolean {
  if (!dueAt) return false;
  if (Number.isNaN(dueAt.getTime())) return true;
  return dueAt.getTime() - now.getTime() <= EDIT_LOCKOUT_MS;
}

export type SaveBlockReason =
  | "not_editable"
  | "invalid_date"
  | "unchanged"
  | "over_limit"
  | "past_time"
  | "near_due_at";

export interface EvaluateSaveInput {
  status: PostStatus;
  service: SnsService;
  /** 投稿文フィールドが実際に編集されたか（`caption !== post.caption` ではなく呼び出し側の意図を渡す）。 */
  captionChanged: boolean;
  /**
   * 日時フィールドが実際に触られたか。
   * `<input type="datetime-local">` は秒以下を保持できないため、DB の元の値と単純に
   * タイムスタンプ比較すると「触っていないのに秒が丸められて別の値に見える」誤検出が起きる。
   * そのため「変更されたか」は比較ではなく、呼び出し側が明示的に渡す。
   */
  dueAtChanged: boolean;
  nextCaption: string;
  originalDueAt: Date | null;
  nextDueAt: Date;
  now: Date;
}

export interface EvaluateSaveResult {
  canSave: boolean;
  reasons: SaveBlockReason[];
}

/** PostDrawer の保存可否と、その理由（複数ありうる）を判定する純粋関数。 */
export function evaluateSave(input: EvaluateSaveInput): EvaluateSaveResult {
  if (!isEditableStatus(input.status)) {
    return { canSave: false, reasons: ["not_editable"] };
  }
  const hasInvalidDate =
    (input.dueAtChanged && Number.isNaN(input.nextDueAt.getTime())) ||
    (input.originalDueAt !== null && Number.isNaN(input.originalDueAt.getTime()));
  if (hasInvalidDate) {
    return { canSave: false, reasons: ["invalid_date"] };
  }

  const reasons: SaveBlockReason[] = [];
  // 送信予定時刻の5分前以降は、どのフィールドを編集していても一律ブロックする
  // （Buffer が送信中の可能性があるため）。
  if (isWithinLockoutWindow(input.originalDueAt, input.now)) {
    reasons.push("near_due_at");
  }
  // 日時フィールドを触っていないなら、その値を送信しないので過去日時チェックは対象外。
  if (input.dueAtChanged && input.nextDueAt.getTime() <= input.now.getTime()) {
    reasons.push("past_time");
  }
  if (captionLength(input.service, input.nextCaption) > captionLimit(input.service)) {
    reasons.push("over_limit");
  }
  if (!input.captionChanged && !input.dueAtChanged) {
    reasons.push("unchanged");
  }
  return { canSave: reasons.length === 0, reasons };
}

/** 予約キャンセルボタンを押せるかどうか。 */
export function isCancelable(status: PostStatus, dueAt: Date | null, now: Date): boolean {
  return isEditableStatus(status) && !isWithinLockoutWindow(dueAt, now);
}
