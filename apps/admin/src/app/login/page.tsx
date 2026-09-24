"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { signInWithMagicLink, signInWithPassword } from "@/lib/auth";
import styles from "./page.module.css";

type Mode = "password" | "magic-link";
type LoginStatus = "idle" | "submitting" | "sent" | "error";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("password");
  const [id, setId] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<LoginStatus>("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  function switchMode(next: Mode) {
    setMode(next);
    setStatus("idle");
    setErrorMessage(null);
  }

  async function handlePasswordSubmit(event: React.FormEvent) {
    event.preventDefault();
    setStatus("submitting");
    setErrorMessage(null);
    const { error } = await signInWithPassword(id, password);
    if (error) {
      setStatus("error");
      setErrorMessage(error);
      return;
    }
    router.replace("/calendar/");
  }

  async function handleMagicLinkSubmit(event: React.FormEvent) {
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

        {mode === "password" && (
          <form className={styles.field} onSubmit={handlePasswordSubmit}>
            <h1 className={styles.title}>ID とパスワードでログイン</h1>
            <label htmlFor="login-id">ID</label>
            <input
              id="login-id"
              type="text"
              required
              autoComplete="username"
              value={id}
              onChange={(event) => setId(event.target.value)}
            />
            <label htmlFor="login-password">パスワード</label>
            <input
              id="login-password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            {status === "error" && errorMessage && (
              <p className={styles.note} data-tone="err">
                {errorMessage}
              </p>
            )}
            <button
              type="submit"
              className={styles.submit}
              disabled={status === "submitting" || !id.trim() || !password.trim()}
            >
              {status === "submitting" ? "送信中..." : "ログイン"}
            </button>
            <button type="button" className={styles.linkButton} onClick={() => switchMode("magic-link")}>
              メールのリンクでログイン
            </button>
          </form>
        )}

        {mode === "magic-link" &&
          (status === "sent" ? (
            <p className={styles.note}>
              ログイン用のリンクをメールで送信しました。メール内のリンクを開いてログインしてください。
            </p>
          ) : (
            <form className={styles.field} onSubmit={handleMagicLinkSubmit}>
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
              <button type="button" className={styles.linkButton} onClick={() => switchMode("password")}>
                ID とパスワードでログイン
              </button>
            </form>
          ))}
      </div>
    </div>
  );
}
