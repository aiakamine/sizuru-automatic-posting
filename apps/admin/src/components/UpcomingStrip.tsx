import { groupUpcomingBySubmission } from "@/lib/upcoming";
import { formatJstMonthDay, formatJstTime } from "@/lib/week";
import type { PostWithRelations } from "@/lib/types";
import { PlatformMark } from "./PlatformMark";
import { VideoThumbnail } from "./VideoThumbnail";
import styles from "./UpcomingStrip.module.css";

/** 「これから投稿される動画」帯。予約済み・下書きを動画単位でまとめて表示する。 */
export function UpcomingStrip({
  posts,
  onSelectPost,
}: {
  posts: PostWithRelations[];
  onSelectPost: (postId: string) => void;
}) {
  const groups = groupUpcomingBySubmission(posts);

  if (groups.length === 0) {
    return <p className={styles.empty}>予約済み・下書きの投稿はありません</p>;
  }

  return (
    <div className={styles.strip}>
      {groups.map((group) => {
        const nextDueAt = new Date(group.nextDueAt);
        return (
          <button
            key={group.submission.id}
            type="button"
            className={styles.card}
            onClick={() => onSelectPost(group.nextPostId)}
          >
            <VideoThumbnail
              thumbnailUrl={group.submission.thumbnail_url}
              durationSec={group.submission.duration_sec}
              alt={group.submission.title ?? "動画"}
              size="lg"
            />
            <b>{group.submission.title ?? "(タイトル未設定)"}</b>
            <small className="num">
              次回 {formatJstMonthDay(nextDueAt)} {formatJstTime(nextDueAt)}
            </small>
            <span className={styles.services}>
              {group.services.map((service, index) => (
                <PlatformMark key={`${service}-${index}`} service={service} size="sm" />
              ))}
            </span>
          </button>
        );
      })}
    </div>
  );
}
