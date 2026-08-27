// The cell side of the migration's schedule oracle: `domain/fsrs` driven
// over stdin, so `prep.migrate.verify` can compare it against py-fsrs on
// the same rows. Bundled for node by prep/migrate/fsrs_oracle.py, the way
// scripts/build.mjs bundles render-fixtures.
//
// stdin:  {"now": iso, "verdicts": ["right","wrong"], "cards": [{key, stability,
//          difficulty, fsrs_state, last_review, retention}]}
// stdout: {"results": {key: {verdict: {stability, difficulty, fsrs_state,
//          last_review, next_due, interval_seconds, step_bucket} | {error}}}}
//
// Fuzz is off: a fuzzed interval is a random draw, and the two sides are
// being compared, not sampled.
import { scheduleReview } from "../domain/fsrs/index.ts";
import { isoUtc, parseIso } from "../domain/py.ts";

function run(card, verdict, now) {
  const state = {
    stability: card.stability ?? null,
    difficulty: card.difficulty ?? null,
    fsrsState: card.fsrs_state,
    lastReview: card.last_review == null ? null : parseIso(card.last_review),
  };
  try {
    const r = scheduleReview(state, verdict, now, { desiredRetention: card.retention ?? null, fuzz: false });
    return {
      stability: r.state.stability,
      difficulty: r.state.difficulty,
      fsrs_state: r.state.fsrsState,
      last_review: isoUtc(r.state.lastReview),
      next_due: isoUtc(r.nextDue),
      interval_seconds: r.intervalSeconds,
      step_bucket: r.stepBucket,
    };
  } catch (e) {
    return { error: e?.constructor?.name ?? "Error" };
  }
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

const job = JSON.parse(await readStdin());
const now = parseIso(job.now);
const results = {};
for (const card of job.cards) {
  const per = {};
  for (const verdict of job.verdicts) per[verdict] = run(card, verdict, now);
  results[card.key] = per;
}
process.stdout.write(JSON.stringify({ results }));
