"use client";

import { useEffect, useState } from "react";
import { fetchIsAdmin } from "@/lib/posts";

export type AdminStatus = "loading" | "admin" | "not-admin" | "error";

type CheckedResult = { userId: string; status: Exclude<AdminStatus, "loading"> };

/**
 * ログイン中のユーザーが `admin_users` に登録されているかを判定する。
 * 結果は「どの userId に対する結果か」を添えて保持し、現在の userId と一致しないときは
 * （前のユーザーの結果を一瞬でも見せないよう）常に "loading" を返す。
 */
export function useAdminStatus(userId: string | null): AdminStatus {
  const [checked, setChecked] = useState<CheckedResult | null>(null);

  useEffect(() => {
    if (!userId) return;
    let active = true;
    fetchIsAdmin(userId)
      .then((isAdmin) => {
        if (active) setChecked({ userId, status: isAdmin ? "admin" : "not-admin" });
      })
      .catch(() => {
        if (active) setChecked({ userId, status: "error" });
      });
    return () => {
      active = false;
    };
  }, [userId]);

  if (!userId) return "not-admin";
  if (!checked || checked.userId !== userId) return "loading";
  return checked.status;
}
