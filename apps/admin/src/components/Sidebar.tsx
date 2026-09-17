"use client";

import styles from "./Sidebar.module.css";

/** 左サイドバー。カレンダーのみ（承認済みスコープ: パターン B + ログインのみ）とログアウト。 */
export function Sidebar({ onLogout }: { onLogout: () => void }) {
  return (
    <aside className={styles.side} aria-label="メニュー">
      <div className={styles.brand}>
        <i className={styles.brandMark} aria-hidden="true">
          S
        </i>
        Sizuru
      </div>
      <button type="button" className={styles.nav} aria-current="page">
        カレンダー
      </button>
      <span className={styles.spacer} />
      <button type="button" className={styles.logout} onClick={onLogout}>
        ログアウト
      </button>
    </aside>
  );
}
