import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { markdownHTML } from "../../domain/markdown";

// Every corpus case byte-equal to the server filter's output.
const REPO = new URL("../../..", import.meta.url).pathname;
const corpus = JSON.parse(readFileSync(`${REPO}tests/fixtures/parity/markdown/corpus.json`, "utf8")) as {
  cases: { id: string; input: string; expected: string }[];
};

describe("markdown corpus", () => {
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
