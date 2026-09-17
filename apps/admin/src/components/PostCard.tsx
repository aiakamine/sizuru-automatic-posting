import { formatJstTime } from "@/lib/week";
import type { PostWithRelations } from "@/lib/types";
import { PlatformMark } from "./PlatformMark";
import { StatusPill } from "./StatusPill";
import { VideoThumbnail } from "./VideoThumbnail";
import styles from "./PostCard.module.css";

/** 週ボードの1マス（slot）。カレンダー上のカード。クリックで PostDrawer を開く。 */
export function PostCard({
  post,
  isSelected,
  onSelect,
}: {
  post: PostWithRelations;
  isSelected: boolean;
  onSelect: (postId: string) => void;
}) {
  const status = post.status;

  return (
    <button
      type="button"
      className={styles.slot}
      data-status={status}
      aria-pressed={isSelected}
      onClick={() => onSelect(post.id)}
    >
      <span className={styles.bar} data-status={status} aria-hidden="true" />
      <span className={styles.top}>
        {post.due_at ? (
          <span className={`${styles.time} num`}>{formatJstTime(new Date(post.due_at))}</span>
        ) : (
          <span className={`${styles.time} num`}>--:--</span>
        )}
        <PlatformMark service={post.channels.service} size="sm" />
      </span>
      <span className={styles.media}>
        <VideoThumbnail
          thumbnailUrl={post.submissions.thumbnail_url}
          durationSec={post.submissions.duration_sec}
          alt={post.submissions.title ?? "動画"}
        />
        <span>
          <p className={styles.title}>{post.submissions.title ?? "(タイトル未設定)"}</p>
          <p className={styles.caption}>{post.caption}</p>
        </span>
      </span>
      <span className={styles.foot}>
        <StatusPill status={status} />
      </span>
    </button>
  );
}
