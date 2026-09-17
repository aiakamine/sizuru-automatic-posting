"use client";

import { useEffect, useState } from "react";

const DEFAULT_INTERVAL_MS = 30_000;

/**
 * 現在時刻を一定間隔で更新して返す。「5分前ロック」「過去日時」判定のように
 * 時間経過そのものが状態を変える UI（PostDrawer・WeekBoard の today ハイライト等）を
 * 開いたままにしていても、ある程度新鮮な `now` を保つ。
 * タブがバックグラウンドの間はタイマーが遅延しうるため、フォーカス復帰・可視化のタイミングでも即座に更新する。
 */
export function useNow(intervalMs: number = DEFAULT_INTERVAL_MS): Date {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const refresh = () => setNow(new Date());
    const interval = setInterval(refresh, intervalMs);

    function handleVisibilityChange() {
      if (document.visibilityState === "visible") refresh();
    }

    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      clearInterval(interval);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [intervalMs]);

  return now;
}
