/**
 * 日本時間（JST, UTC+9, DST なし）を基準にした週境界・日時フォーマット。
 *
 * ホストのブラウザ／実行環境のタイムゾーンに依存しないよう、`Date` のローカルゲッター
 * （`getHours` 等）は一切使わず、UTC ゲッターとオフセットのシフトだけで計算する。
 */

const JST_OFFSET_MS = 9 * 60 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;

interface JstParts {
  year: number;
  month: number; // 1-12
  day: number;
  hour: number;
  minute: number;
  second: number;
  /** 0 = 日曜 ... 6 = 土曜（JST の暦日ベース） */
  weekday: number;
}

/** 実時刻の instant を JST の暦日・時刻フィールドへ分解する。 */
function toJstParts(instant: Date): JstParts {
  const shifted = new Date(instant.getTime() + JST_OFFSET_MS);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
    hour: shifted.getUTCHours(),
    minute: shifted.getUTCMinutes(),
    second: shifted.getUTCSeconds(),
    weekday: shifted.getUTCDay(),
  };
}

/** JST の暦日・時刻フィールドから実時刻の instant を組み立てる。 */
function fromJstParts(parts: {
  year: number;
  month: number;
  day: number;
  hour?: number;
  minute?: number;
  second?: number;
}): Date {
  const shiftedMs = Date.UTC(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour ?? 0,
    parts.minute ?? 0,
    parts.second ?? 0,
  );
  return new Date(shiftedMs - JST_OFFSET_MS);
}

const pad2 = (n: number): string => String(n).padStart(2, "0");

/** `instant` を含む週（月曜始まり・JST）の月曜 00:00 JST を返す。 */
export function jstWeekStart(instant: Date): Date {
  const parts = toJstParts(instant);
  const daysSinceMonday = (parts.weekday + 6) % 7; // 月=0, 火=1, ..., 日=6
  const monday = fromJstParts({ year: parts.year, month: parts.month, day: parts.day - daysSinceMonday });
  return monday;
}

/** 月曜 00:00 JST の `weekStart` から、その週7日分の 00:00 JST instant を返す。 */
export function jstWeekDays(weekStart: Date): Date[] {
  return Array.from({ length: 7 }, (_, i) => new Date(weekStart.getTime() + i * DAY_MS));
}

/** 2つの instant が同じ JST の暦日かどうか。 */
export function isSameJstDay(a: Date, b: Date): boolean {
  const pa = toJstParts(a);
  const pb = toJstParts(b);
  return pa.year === pb.year && pa.month === pb.month && pa.day === pb.day;
}

/** `YYYY-MM-DD` 形式（JST）。日カラムのキーなどに使う。 */
export function formatJstIsoDate(instant: Date): string {
  const p = toJstParts(instant);
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}`;
}

/** `HH:mm` 形式（JST）。 */
export function formatJstTime(instant: Date): string {
  const p = toJstParts(instant);
  return `${pad2(p.hour)}:${pad2(p.minute)}`;
}

/** `M/D` 形式（JST）。 */
export function formatJstMonthDay(instant: Date): string {
  const p = toJstParts(instant);
  return `${p.month}/${p.day}`;
}

/** 週の範囲ラベル `YYYY/M/D – M/D`（月曜〜日曜, JST）。 */
export function formatJstWeekRangeLabel(weekStart: Date): string {
  const days = jstWeekDays(weekStart);
  const first = toJstParts(days[0] as Date);
  const last = toJstParts(days[6] as Date);
  return `${first.year}/${first.month}/${first.day} – ${last.month}/${last.day}`;
}

/** `<input type="datetime-local">` 用の値（JST の壁時計表記）。 */
export function toDatetimeLocalValue(instant: Date): string {
  const p = toJstParts(instant);
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}T${pad2(p.hour)}:${pad2(p.minute)}`;
}

/**
 * `<input type="datetime-local">` の値（JST の壁時計表記として解釈）を instant に変換する。
 *
 * `Date.UTC` は "2026-02-30" のような存在しない日付を（3月2日などへ）静かに繰り上げてしまうため、
 * 一度組み立てた instant を再度 `toJstParts` で分解し、入力の各フィールドと一致するか
 * 突き合わせて検証する（ラウンドトリップ検証）。一致しなければ実在しない日時として拒否する。
 */
export function fromDatetimeLocalValue(value: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) {
    throw new Error(`invalid datetime-local value: ${value}`);
  }
  const [, year, month, day, hour, minute] = match;
  const parts = {
    year: Number(year),
    month: Number(month),
    day: Number(day),
    hour: Number(hour),
    minute: Number(minute),
  };
  const instant = fromJstParts(parts);
  const roundTrip = toJstParts(instant);
  const isValid =
    roundTrip.year === parts.year &&
    roundTrip.month === parts.month &&
    roundTrip.day === parts.day &&
    roundTrip.hour === parts.hour &&
    roundTrip.minute === parts.minute;
  if (!isValid) {
    throw new Error(`invalid datetime-local value (nonexistent date/time): ${value}`);
  }
  return instant;
}

/** API へ送る ISO8601 文字列（`+09:00` オフセット付き）。 */
export function toDueAtIsoString(instant: Date): string {
  const p = toJstParts(instant);
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}T${pad2(p.hour)}:${pad2(p.minute)}:${pad2(p.second)}+09:00`;
}
