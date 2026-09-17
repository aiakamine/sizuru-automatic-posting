import { formatDurationSec } from "@/lib/duration";
import styles from "./VideoThumbnail.module.css";

export function VideoThumbnail({
  thumbnailUrl,
  durationSec,
  alt,
  size = "sm",
}: {
  thumbnailUrl: string | null;
  durationSec: number | null;
  alt: string;
  size?: "sm" | "lg";
}) {
  const duration = formatDurationSec(durationSec);
  return (
    <span className={styles.thumb} data-size={size} aria-label={thumbnailUrl ? undefined : alt}>
      {/* eslint-disable-next-line @next/next/no-img-element -- サムネはサイズ多様・静的書き出しのため next/image を使わない */}
      {thumbnailUrl ? <img className={styles.image} src={thumbnailUrl} alt={alt} loading="lazy" /> : null}
      {duration ? <span className={styles.duration}>{duration}</span> : null}
    </span>
  );
}
