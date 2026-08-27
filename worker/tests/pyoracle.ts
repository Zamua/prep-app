// The recorded answers of the Python implementation this worker was ported
// from. Every value in `fixtures/pyoracle.json` was produced by running its
// snippet against that implementation; the implementation is gone, so a
// snippet is provenance rather than something that can run again.
//
// The snippet's text is the key. Editing one does not produce a new answer,
// it loses the recorded one, which is why a miss throws instead of falling
// back to anything.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const FIXTURE = new URL("./fixtures/pyoracle.json", import.meta.url).pathname;

let recorded: Record<string, { snippet: string; value: unknown }> | undefined;

export function pythonJson<T>(snippet: string): T {
  recorded ??= JSON.parse(readFileSync(FIXTURE, "utf8"));
  const key = createHash("sha256").update(snippet).digest("hex").slice(0, 16);
  const hit = recorded![key];
  if (!hit) {
    throw new Error(
      `no recorded Python answer for this snippet (${key}). The reference no ` +
        `longer exists, so a new or edited snippet cannot be answered:\n${snippet}`,
    );
  }
  return hit.value as T;
}
