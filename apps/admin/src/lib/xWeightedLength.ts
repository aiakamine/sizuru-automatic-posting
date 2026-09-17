// `twitter-text` は ESM ビルドでも default export のみを持つ（named export が無い）ため、
// default から取り出す（`import { parseTweet }` は Turbopack の本番ビルドで解決に失敗する）。
import twitterText from "twitter-text";
import type { SnsService } from "./types";

const { parseTweet } = twitterText;

/** 媒体ごとの投稿文字数上限。 */
const CAPTION_LIMITS: Record<SnsService, number> = {
  tiktok: 2200,
  instagram: 2200,
  x: 280,
};

/**
 * X（旧 Twitter）の重み付け文字数。
 * 自前実装ではなく公式の `twitter-text`（本家 Twitter/X と同じ仕様）に委譲する。
 * URL は長さに関わらず一律 23、結合絵文字（家族の絵文字等の ZWJ シーケンス）や
 * 合成前後の異なるアクセント文字表現も本家と同じ重みになる。
 * `twitter-text` 自体も内部で正規化するが、契約を明示するためここでも NFC 正規化してから渡す
 * （他の媒体と同じく「まず NFC 正規化」を統一のルールにする）。
 * Python 側の実装と `tests/fixtures/caption_length_cases.json` で一致を検証する。
 */
export function xWeightedLength(text: string): number {
  return parseTweet(text.normalize("NFC")).weightedLength;
}

/** 媒体ごとの投稿文字数（NFC 正規化してから、X のみ重み付け・それ以外はコードポイント数）。 */
export function captionLength(service: SnsService, text: string): number {
  if (service === "x") return xWeightedLength(text);
  return [...text.normalize("NFC")].length;
}

/** 媒体ごとの投稿文字数上限。 */
export function captionLimit(service: SnsService): number {
  return CAPTION_LIMITS[service];
}
