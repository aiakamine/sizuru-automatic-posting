"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchUpcomingPosts } from "@/lib/posts";
import type { PostWithRelations } from "@/lib/types";

export interface UseUpcomingPostsResult {
  posts: PostWithRelations[];
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

/** 「これから投稿される動画」帯用に、予約済み・下書きで今後の日時のものを取得する。 */
export function useUpcomingPosts(now: Date): UseUpcomingPostsResult {
  const [posts, setPosts] = useState<PostWithRelations[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let active = true;
    // 手動再取得のたびに読み込み中表示へ戻す。
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 依存値が変わった瞬間に前回データを隠すための意図的なリセット
    setLoading(true);
    setError(null);
    fetchUpcomingPosts(now)
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `now` は呼び出し側で意図的に固定・更新される基準時刻
  }, [reloadToken]);

  const refetch = useCallback(() => setReloadToken((token) => token + 1), []);

  return { posts, loading, error, refetch };
}
