import styles from "./ConfigErrorScreen.module.css";

/**
 * ビルド時に必要な環境変数（NEXT_PUBLIC_*）が1つでも欠けているときに全画面表示する。
 * `output: "export"` はビルド時の値がそのまま静的 HTML に焼き込まれるため、
 * デプロイ担当者が気づけるよう、実行時エラーではなくこの専用画面で明示する。
 */
export function ConfigErrorScreen() {
  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <h1 className={styles.title}>設定エラー</h1>
        <p>このビルドには必要な環境変数が設定されていません。以下をビルド環境に設定してから、再度ビルド・デプロイしてください。</p>
        <ul className={styles.list}>
          <li>NEXT_PUBLIC_SUPABASE_URL</li>
          <li>NEXT_PUBLIC_SUPABASE_ANON_KEY</li>
          <li>NEXT_PUBLIC_API_URL</li>
        </ul>
      </div>
    </div>
  );
}
