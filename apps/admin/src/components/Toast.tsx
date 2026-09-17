import styles from "./Toast.module.css";

export function Toast({
  message,
  tone = "success",
}: {
  message: string;
  tone?: "success" | "warning";
}) {
  return (
    <div className={styles.toast} data-tone={tone} role="status" aria-live="polite">
      {message}
    </div>
  );
}
