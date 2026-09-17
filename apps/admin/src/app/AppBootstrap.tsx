"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { setUnauthorizedHandler } from "@/lib/api";
import { signOut } from "@/lib/auth";

/**
 * 画面には何も描画しない、アプリ起動時の副作用専用コンポーネント。
 * API から 401 が返ったときにログアウトしてログイン画面へ戻す処理を、
 * ここで一度だけ登録する（`src/lib/api.ts` 自体は副作用を知らない）。
 */
export function AppBootstrap() {
  const router = useRouter();

  useEffect(() => {
    setUnauthorizedHandler(() => {
      void signOut().finally(() => router.replace("/login/"));
    });
  }, [router]);

  return null;
}
