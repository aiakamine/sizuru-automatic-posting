"use client";

import { useMemo, useState } from "react";
import { PostDrawer } from "@/components/PostDrawer";
import { StatusPill } from "@/components/StatusPill";
import { Toast } from "@/components/Toast";
import { UpcomingStrip } from "@/components/UpcomingStrip";
import { WeekBoard } from "@/components/WeekBoard";
import { useNow } from "@/hooks/useNow";
import { useSession } from "@/hooks/useSession";
import { useToast } from "@/hooks/useToast";
import { useUpcomingPosts } from "@/hooks/useUpcomingPosts";
import { useWeekPosts } from "@/hooks/useWeekPosts";
import { syncPosts } from "@/lib/api";
import { describeSyncResult } from "@/lib/syncToast";
import type { PostStatus } from "@/lib/types";
import { formatJstWeekRangeLabel, jstWeekStart } from "@/lib/week";
import styles from "./page.module.css";

const LEGEND_STATUSES: PostStatus[] = ["scheduled", "sent", "draft", "failed", "canceled"];
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

export default function CalendarPage() {
  const session = useSession();
  const accessToken = session.status === "signed-in" ? session.session.accessToken : null;

  // 30秒ごと・フォーカス/可視化復帰時に更新される「今」。5分ロックアウト等の判定に使う
  // （実際の送信直前には PostDrawer 側でさらに新鮮な Date() で再検証する）。
  const now = useNow();
  const [weekStart, setWeekStart] = useState(() => jstWeekStart(new Date()));
  const [selectedPostId, setSelectedPostId] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const { message: toastMessage, tone: toastTone, show: showToast } = useToast();

  const week = useWeekPosts(weekStart);
  const upcoming = useUpcomingPosts(now);

  const selectedPost = useMemo(() => {
    if (!selectedPostId) return null;
    return (
      week.posts.find((post) => post.id === selectedPostId) ??
      upcoming.posts.find((post) => post.id === selectedPostId) ??
      null
    );
  }, [selectedPostId, week.posts, upcoming.posts]);

  function refetchAll() {
    week.refetch();
    upcoming.refetch();
  }

  async function handleSync() {
    if (!accessToken) return;
    setSyncing(true);
    try {
      const { sync } = await syncPosts(accessToken);
      const { tone, message } = describeSyncResult(sync);
      showToast(message, tone);
      refetchAll();
    } catch {
      showToast("同期に失敗しました。時間をおいて再度お試しください。", "warning");
    } finally {
      setSyncing(false);
    }
  }

  if (!accessToken) {
    return <div className={styles.state}>読み込み中...</div>;
  }

  return (
    <>
      <div className={styles.topbar}>
        <div>
          <h1>週カレンダー</h1>
          <p>各日にどの動画が出るかをサムネイルで表示。カードを選ぶと右側で日時・投稿文を編集できます。</p>
        </div>
        <button type="button" className={styles.syncButton} onClick={handleSync} disabled={syncing}>
          {syncing ? "更新中..." : "更新"}
        </button>
      </div>

      <div className={styles.upcomingCard}>
        <div className={styles.upcomingHeader}>
          <h2>これから投稿される動画</h2>
          <span style={{ fontSize: 12, color: "var(--muted)" }}>予約済み・下書きを含む</span>
        </div>
        {upcoming.loading ? (
          <p className={styles.state}>読み込み中...</p>
        ) : upcoming.error ? (
          <p className={styles.state} data-tone="err">
            {upcoming.error}
          </p>
        ) : (
          <UpcomingStrip posts={upcoming.posts} onSelectPost={setSelectedPostId} />
        )}
      </div>

      <div className={styles.weekbar}>
        <button
          type="button"
          className={styles.navButton}
          aria-label="前の週"
          onClick={() => setWeekStart(new Date(weekStart.getTime() - WEEK_MS))}
        >
          ‹
        </button>
        <b className="num">{formatJstWeekRangeLabel(weekStart)}</b>
        <button
          type="button"
          className={styles.navButton}
          aria-label="次の週"
          onClick={() => setWeekStart(new Date(weekStart.getTime() + WEEK_MS))}
        >
          ›
        </button>
        <button type="button" className={styles.navButton} onClick={() => setWeekStart(jstWeekStart(new Date()))}>
          今日
        </button>
        <div className={styles.legend}>
          {LEGEND_STATUSES.map((status) => (
            <StatusPill key={status} status={status} />
          ))}
        </div>
      </div>

      {week.loading ? (
        <p className={styles.state}>読み込み中...</p>
      ) : week.error ? (
        <p className={styles.state} data-tone="err">
          {week.error}
        </p>
      ) : (
        <div className={styles.boardArea} data-has-drawer={selectedPost !== null}>
          <WeekBoard
            weekStart={weekStart}
            posts={week.posts}
            selectedPostId={selectedPostId}
            onSelectPost={setSelectedPostId}
            today={now}
          />
          {selectedPost && (
            // post.id を key にして、別の投稿へ切り替えたときに編集中の入力や
            // キャンセル確認ダイアログが前の投稿の状態のまま残らないようにする。
            <PostDrawer
              key={selectedPost.id}
              post={selectedPost}
              accessToken={accessToken}
              now={now}
              onClose={() => setSelectedPostId(null)}
              onSaved={() => {
                showToast("変更を保存しました");
                refetchAll();
              }}
              onCanceled={() => {
                showToast("予約をキャンセルしました");
                setSelectedPostId(null);
                refetchAll();
              }}
              onRequireResync={refetchAll}
            />
          )}
        </div>
      )}

      {toastMessage && <Toast message={toastMessage} tone={toastTone} />}
    </>
  );
}
