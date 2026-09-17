/** `submissions.duration_sec` を `m:ss` 表記にする。null はサムネの尺バッジを出さないために null のまま返す。 */
export function formatDurationSec(durationSec: number | null): string | null {
  if (durationSec === null) return null;
  const totalSeconds = Math.floor(durationSec);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
