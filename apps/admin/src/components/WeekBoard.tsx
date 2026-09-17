import { formatJstIsoDate, isSameJstDay, jstWeekDays } from "@/lib/week";
import type { PostWithRelations } from "@/lib/types";
import { PostCard } from "./PostCard";
import styles from "./WeekBoard.module.css";

/** `jstWeekDays` は月曜始まりで返すため、曜日ラベルもこの並びに固定する。 */
const WEEKDAY_LABELS_MON_START = ["月", "火", "水", "木", "金", "土", "日"];

export function WeekBoard({
  weekStart,
  posts,
  selectedPostId,
  onSelectPost,
  today,
}: {
  weekStart: Date;
  posts: PostWithRelations[];
  selectedPostId: string | null;
  onSelectPost: (postId: string) => void;
  today: Date;
}) {
  const days = jstWeekDays(weekStart);

  return (
    <div className={styles.week}>
      {days.map((day, index) => {
        const postsForDay = posts
          .filter((post) => post.due_at !== null && isSameJstDay(new Date(post.due_at), day))
          .sort((a, b) => (a.due_at ?? "").localeCompare(b.due_at ?? ""));
        const isToday = isSameJstDay(day, today);

        return (
          <div
            key={formatJstIsoDate(day)}
            className={styles.day}
            data-testid={`week-day-${formatJstIsoDate(day)}`}
            data-today={isToday}
          >
            <div className={styles.dayHeader}>
              <b className="num">{Number(formatJstIsoDate(day).split("-")[2])}</b>
              <small>{WEEKDAY_LABELS_MON_START[index]}</small>
            </div>
            <div className={styles.dayBody}>
              {postsForDay.length === 0 ? (
                <span className={styles.empty}>予定なし</span>
              ) : (
                postsForDay.map((post) => (
                  <PostCard
                    key={post.id}
                    post={post}
                    isSelected={selectedPostId === post.id}
                    onSelect={onSelectPost}
                  />
                ))
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
