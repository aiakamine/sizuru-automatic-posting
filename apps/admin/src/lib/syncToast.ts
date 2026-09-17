import type { SyncResult } from "./types";

export interface SyncToastDescription {
  tone: "success" | "warning";
  message: string;
}

/**
 * 「更新」ボタンで叩いた `POST /sync` の結果をトーストの文言に変換する。
 * `errors` が空でない・`stuck_pending` が0より大きい・`truncated` が true のいずれかなら、
 * 見た目は「成功」でも実際には手当てが必要なので警告として扱う。
 */
export function describeSyncResult(sync: SyncResult): SyncToastDescription {
  const parts = [`投稿済み${sync.sent}件`, `失敗${sync.failed}件`, `キャンセル${sync.canceled}件`];
  const warnings: string[] = [];

  if (sync.errors && sync.errors.length > 0) {
    warnings.push(`${sync.errors.length}件のエラー`);
  }
  if (sync.stuck_pending > 0) {
    warnings.push(`確認中のまま${sync.stuck_pending}件`);
  }
  if (sync.truncated) {
    warnings.push("一部のみ処理（対象が多すぎました）");
  }

  if (warnings.length === 0) {
    return { tone: "success", message: `同期しました（${parts.join("・")}）` };
  }
  return {
    tone: "warning",
    message: `同期は完了しましたが確認が必要です（${parts.join("・")}／${warnings.join("・")}）`,
  };
}
