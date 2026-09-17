"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Sidebar } from "@/components/Sidebar";
import { useAdminStatus } from "@/hooks/useAdminStatus";
import { useSession } from "@/hooks/useSession";
import { signOut } from "@/lib/auth";
import styles from "./layout.module.css";

/**
 * `/calendar/` 配下の共通ガード。
 * 未ログインならログイン画面へ、ログイン済みだが `admin_users` にいなければ
 * 権限エラー画面を表示する。管理者と確認できた場合のみサイドバー付きの本体を描画する。
 */
export default function CalendarLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const session = useSession();
  const userId = session.status === "signed-in" ? session.session.userId : null;
  const adminStatus = useAdminStatus(userId);

  useEffect(() => {
    if (session.status === "signed-out") {
      router.replace("/login/");
    }
  }, [session, router]);

  async function handleLogout() {
    await signOut();
    router.replace("/login/");
  }

  if (session.status !== "signed-in" || adminStatus === "loading") {
    return <div className={styles.centered}>読み込み中...</div>;
  }

  if (adminStatus === "error") {
    return (
      <div className={styles.centered}>
        <div className={styles.forbidden}>
          <p>権限の確認中にエラーが発生しました。時間をおいて再度お試しください。</p>
          <button type="button" className={styles.logoutButton} onClick={handleLogout}>
            ログアウト
          </button>
        </div>
      </div>
    );
  }

  if (adminStatus === "not-admin") {
    return (
      <div className={styles.centered}>
        <div className={styles.forbidden}>
          <p>このアカウントには管理画面の権限がありません。</p>
          <button type="button" className={styles.logoutButton} onClick={handleLogout}>
            ログアウト
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.app}>
      <Sidebar onLogout={handleLogout} />
      <main className={styles.main}>{children}</main>
    </div>
  );
}
