import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { captionLength } from "./xWeightedLength";
import type { SnsService } from "./types";

interface CaptionLengthCase {
  description: string;
  service: SnsService;
  text: string;
  expected_length: number;
  limit: number;
}

const dirname = path.dirname(fileURLToPath(import.meta.url));
// apps/admin/src/lib -> リポジトリルートの tests/fixtures/caption_length_cases.json
const fixturePath = path.resolve(dirname, "../../../../tests/fixtures/caption_length_cases.json");
const cases: CaptionLengthCase[] = JSON.parse(readFileSync(fixturePath, "utf-8"));

describe("captionLength against tests/fixtures/caption_length_cases.json", () => {
  it("has at least one case per service so this fixture stays meaningful", () => {
    const services = new Set(cases.map((c) => c.service));
    expect(services).toEqual(new Set<SnsService>(["tiktok", "instagram", "x"]));
  });

  it.each(cases)("$description", ({ service, text, expected_length }) => {
    expect(captionLength(service, text)).toBe(expected_length);
  });
});
