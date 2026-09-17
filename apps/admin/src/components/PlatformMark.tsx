import type { SnsService } from "@/lib/types";
import styles from "./PlatformMark.module.css";

const LABELS: Record<SnsService, string> = {
  tiktok: "TikTok",
  instagram: "Instagram",
  x: "X",
};

const ICONS: Record<SnsService, React.ReactNode> = {
  tiktok: (
    <svg viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
      <path d="M16.5 3c.3 2.2 1.7 3.8 4 4v3.1c-1.5 0-2.9-.4-4-1.2v6.3A6 6 0 1 1 10.5 9v3.2a2.9 2.9 0 1 0 2.9 2.9V3h3.1z" />
    </svg>
  ),
  instagram: (
    <svg viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth={2.2} aria-hidden="true">
      <rect x="3.5" y="3.5" width="17" height="17" rx="5" />
      <circle cx="12" cy="12" r="4" />
      <circle cx="17.3" cy="6.7" r="0.6" fill="#fff" />
    </svg>
  ),
  x: (
    <svg viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
      <path d="M17.8 3h3.1l-6.8 7.8 8 10.2h-6.3l-4.9-6.4L5.3 21H2.2l7.3-8.3L1.8 3h6.4l4.4 5.9L17.8 3zm-1.1 16.2h1.7L7.4 4.7H5.6l11.1 14.5z" />
    </svg>
  ),
};

export function PlatformMark({
  service,
  size = "md",
}: {
  service: SnsService;
  size?: "md" | "sm";
}) {
  return (
    <span
      className={styles.mark}
      role="img"
      aria-label={LABELS[service]}
      data-service={service}
      data-size={size}
    >
      {ICONS[service]}
    </span>
  );
}
