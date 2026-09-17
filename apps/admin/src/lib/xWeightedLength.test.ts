import { describe, expect, it } from "vitest";
import { captionLength, captionLimit, xWeightedLength } from "./xWeightedLength";

describe("xWeightedLength", () => {
  it("counts half-width ASCII characters as 1", () => {
    expect(xWeightedLength("hello")).toBe(5);
  });

  it("counts full-width (Japanese) characters as 2", () => {
    expect(xWeightedLength("こんにちは")).toBe(10);
  });

  it("counts a mix of half-width and full-width characters", () => {
    // "起業1年目" = 起,業,1,年,目 -> 2+2+1+2+2 = 9
    expect(xWeightedLength("起業1年目")).toBe(9);
  });

  it("counts any URL as exactly 23 regardless of its length", () => {
    expect(xWeightedLength("https://example.com")).toBe(23);
    expect(xWeightedLength("https://example.com/very/long/path?with=query&params=1")).toBe(23);
  });

  it("counts a URL plus surrounding text", () => {
    // "見て https://example.com ね" -> 見(2)+て(2)+space(1)+URL(23)+space(1)+ね(2) = 31
    expect(xWeightedLength("見て https://example.com ね")).toBe(31);
  });

  it("returns 0 for an empty string", () => {
    expect(xWeightedLength("")).toBe(0);
  });

  it("counts a ZWJ emoji sequence (family emoji) as a single weighted unit of 2, like Twitter/X itself", () => {
    const family = "\u{1F468}‍\u{1F469}‍\u{1F467}‍\u{1F466}";
    expect(xWeightedLength(family)).toBe(2);
  });

  it("counts a decomposed accented character the same as its precomposed form (NFC-equivalent)", () => {
    const decomposed = "é"; // "e" + combining acute accent
    const precomposed = "é";
    expect(xWeightedLength(decomposed)).toBe(xWeightedLength(precomposed));
  });

  it("counts a string that is only a URL as exactly 23", () => {
    expect(xWeightedLength("https://example.com/only-url")).toBe(23);
  });

  it("counts each URL in a string with multiple URLs as 23 apiece", () => {
    // "a"(1) + " "(1) + URL(23) + " "(1) + "b"(1) + " "(1) + URL(23) = 51
    expect(xWeightedLength("a https://example.com/one b https://example.com/two")).toBe(51);
  });

  it("counts newlines as a single half-width character each", () => {
    expect(xWeightedLength("a\nb")).toBe(3);
    expect(xWeightedLength("a\n\nb")).toBe(4);
  });

  it("stays fast for very long input (10k characters)", () => {
    // これはベンチマークではなく、正規表現の破滅的バックトラッキング等による
    // 指数的な劣化が起きていないことを確認するサニティチェック。
    // CI/共有マシンの負荷でも安定して通るよう、閾値は十分に余裕を持たせている。
    const longText = "あ".repeat(10_000);
    const start = performance.now();
    xWeightedLength(longText);
    const elapsed = performance.now() - start;
    expect(elapsed).toBeLessThan(1000);
  });
});

describe("captionLength", () => {
  it("uses weighted length for X", () => {
    expect(captionLength("x", "こんにちは")).toBe(10);
  });

  it("uses plain code-point length for tiktok", () => {
    expect(captionLength("tiktok", "こんにちは")).toBe(5);
  });

  it("uses plain code-point length for instagram", () => {
    expect(captionLength("instagram", "hello")).toBe(5);
  });

  it("normalizes to NFC before counting code points for tiktok/instagram (decomposed accents collapse to 1)", () => {
    const decomposed = "é"; // 2 code points before normalization
    expect(captionLength("tiktok", decomposed)).toBe(1);
    expect(captionLength("instagram", decomposed)).toBe(1);
  });

  it("does not collapse a ZWJ emoji sequence for tiktok/instagram (counted per code point, unlike X)", () => {
    const family = "\u{1F468}‍\u{1F469}‍\u{1F467}‍\u{1F466}";
    // 4 emoji + 3 ZWJ joiners = 7 code points
    expect(captionLength("tiktok", family)).toBe(7);
  });
});

describe("captionLimit", () => {
  it("returns 280 for x", () => {
    expect(captionLimit("x")).toBe(280);
  });

  it("returns 2200 for tiktok and instagram", () => {
    expect(captionLimit("tiktok")).toBe(2200);
    expect(captionLimit("instagram")).toBe(2200);
  });
});
