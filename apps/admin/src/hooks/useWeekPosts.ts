"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchWeekPosts } from "@/lib/posts";
import type { PostWithRelations } from "@/lib/types";

export interface UseWeekPostsResult {
  posts: PostWithRelations[];
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

/** `weekStart` を含む週（7日間）の投稿を取得する。`weekStart` が変わるたびに再取得する。 */
export function useWeekPosts(weekStart: Date): UseWeekPostsResult {
  const [posts, setPosts] = useState<PostWithRelations[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const weekStartMs = weekStart.getTime();

  useEffect(() => {
    let active = true;
    // weekStart（または手動再取得）が変わるたびに読み込み中表示へ戻す。
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 依存値が変わった瞬間に前回データを隠すための意図的なリセット
    setLoading(true);
    setError(null);
    const weekEnd = new Date(weekStartMs + 7 * 24 * 60 * 60 * 1000);
    fetchWeekPosts(new Date(weekStartMs), weekEnd)
      .then((result) => {
        if (active) setPosts(result);
      })
      .catch(() => {
        if (active) setError("投稿の取得に失敗しました。時間をおいて再度お試しください。");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [weekStartMs, reloadToken]);

  const refetch = useCallback(() => setReloadToken((token) => token + 1), []);

  return { posts, loading, error, refetch };
}
