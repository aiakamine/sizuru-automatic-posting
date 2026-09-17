"use client";

import { useState } from "react";
import { signInWithMagicLink } from "@/lib/auth";
import styles from "./page.module.css";

type LoginStatus = "idle" | "submitting" | "sent" | "error";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<LoginStatus>("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setStatus("submitting");
    setErrorMessage(null);
    const redirectTo = `${window.location.origin}/calendar/`;
    const { error } = await signInWithMagicLink(email, redirectTo);
    if (error) {
      setStatus("error");
      setErrorMessage(error);
      return;
    }
    setStatus("sent");
  }

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <i className={styles.brandMark} aria-hidden="true">
            S
          </i>
          Sizuru 管理画面
        </div>

        {status === "sent" ? (
          <p className={styles.note}>
            ログイン用のリンクをメールで送信しました。メール内のリンクを開いてログインしてください。
          </p>
        ) : (
          <form className={styles.field} onSubmit={handleSubmit}>
            <h1 className={styles.title}>メールアドレスでログイン</h1>
            <label htmlFor="email">メールアドレス</label>
            <input
              id="email"
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
            {status === "error" && errorMessage && (
              <p className={styles.note} data-tone="err">
                {errorMessage}
              </p>
            )}
            <button type="submit" className={styles.submit} disabled={status === "submitting" || !email}>
              {status === "submitting" ? "送信中..." : "ログインリンクを送信"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
