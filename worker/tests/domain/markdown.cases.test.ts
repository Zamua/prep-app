import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { markdownHTML } from "../../domain/markdown";

// The shared case list this renderer and its browser twin
// (static/js/study/markdown.js) both answer: an expectation changed on one
// side without the other is a card that renders differently online and
// offline.
const CASES = new URL("../fixtures/markdown/cases.json", import.meta.url).pathname;
const corpus = JSON.parse(readFileSync(CASES, "utf8")) as {
  cases: { id: string; input: string; expected: string }[];
};

describe("markdown cases", () => {
  it("is the full corpus", () => {
    expect(corpus.cases.length).toBeGreaterThanOrEqual(67);
    expect(new Set(corpus.cases.map((c) => c.id)).size).toBe(corpus.cases.length);
  });

  for (const c of corpus.cases) {
    it(c.id, () => {
      expect(markdownHTML(c.input)).toBe(c.expected);
    });
  }

  it("renders nothing for null, undefined and empty", () => {
    expect(markdownHTML(null)).toBe("");
    expect(markdownHTML(undefined)).toBe("");
    expect(markdownHTML("")).toBe("");
  });
});
