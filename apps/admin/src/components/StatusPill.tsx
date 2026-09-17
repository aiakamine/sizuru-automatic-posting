import type { PostStatus } from "@/lib/types";
import styles from "./StatusPill.module.css";

const LABELS: Record<PostStatus, string> = {
  pending: "確認中",
  scheduled: "予約済み",
  sent: "投稿済み",
  draft: "下書き",
  failed: "エラー",
  canceled: "キャンセル済み",
};

export function StatusPill({ status }: { status: PostStatus }) {
  return (
    <span className={styles.pill} data-status={status}>
      {LABELS[status]}
    </span>
  );
}
