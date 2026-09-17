"use client";

import { useEffect, useState } from "react";
import { subscribeToSession, type SessionInfo } from "@/lib/auth";

export type SessionState =
  | { status: "loading" }
  | { status: "signed-out" }
  | { status: "signed-in"; session: SessionInfo };

/**
 * ログイン状態（読み込み中／未ログイン／ログイン済み）を購読する。
 * 初回ロードと認証イベントの競合・購読解除後の後始末は `@/lib/auth` の `subscribeToSession` が担う。
 */
export function useSession(): SessionState {
  const [state, setState] = useState<SessionState>({ status: "loading" });

  useEffect(() => {
    return subscribeToSession((session) => {
      setState(session ? { status: "signed-in", session } : { status: "signed-out" });
    });
  }, []);

  return state;
}
