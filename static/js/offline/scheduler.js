// scheduler.js: the local offline ladder (docs/OFFLINE.md section
// 5). Pure functions with the table as a constant, mirroring the
// shape discipline of prep/domain/srs.py; no I/O, no store access.
//
// Offline devices cannot run FSRS (the scheduler needs the full
// per-card float state and the upstream library), so offline
// re-surfacing uses the ladder prep used before FSRS. It only ever
// decides what to show while offline: at sync the server recomputes
// truth from the review log through FSRS, and the snapshot refresh
// discards every local interval. Parity with the Python-side
// reference is pinned through tests/offline/fixtures/
// ladder_cases.json (Python side: tests/offline/
// test_parity_fixtures.py, which also pins the table to what
// prep/domain/srs.py still exports).

// step:      0     1    2    3    4     5
// interval:  10m   1d   3d   7d   14d   30d
export const TERMINAL_STEP = 5;
export const LADDER_MINUTES = [10, 1440, 4320, 10080, 20160, 43200];

// Ladder transition: right climbs one rung (clamped at the top),
// wrong resets to the bottom rung. `step` is the card's current rung
// (local_step when set, else the snapshot's step bucket);
// out-of-range or missing input clamps into [0, TERMINAL_STEP] so a
// malformed value can never index off the table. Any verdict other
// than "right" resets: right and wrong are the only verdicts prep
// emits, and failing toward more reviews is the harmless direction.
export function transition(step, verdict) {
  let current = Number(step);
  if (!Number.isFinite(current)) current = 0;
  current = Math.trunc(current);
  current = Math.max(0, Math.min(TERMINAL_STEP, current));
  const next = verdict === "right" ? Math.min(current + 1, TERMINAL_STEP) : 0;
  return {step: next, next_due_minutes: LADDER_MINUTES[next]};
}

// Is a card due at `now`? Arguments are ISO-8601 instants with
// explicit offsets (or epoch milliseconds for `now`). A missing or
// unparseable next_due counts as due: surfacing a card early costs
// one extra review, hiding it forever loses study (the acceptable
// drift direction, docs/OFFLINE.md section 5).
export function due(now, nextDue) {
  if (nextDue === null || nextDue === undefined || nextDue === "") return true;
  const dueAt = Date.parse(nextDue);
  if (Number.isNaN(dueAt)) return true;
  const nowAt = typeof now === "number" ? now : Date.parse(now);
  if (Number.isNaN(nowAt)) return false;
  return dueAt <= nowAt;
}

// The write-site timestamp helper: `now` plus `minutes`, always in
// the uniform-offset UTC shape of Date.prototype.toISOString().
// Every timestamp the offline app writes must be in that shape so
// the lexicographic reviewed_at ordering in sync.js flushOutbox
// stays chronological; computing local_next_due through this helper
// keeps the ladder's writes on that contract by construction.
export function nextDueIso(now, minutes) {
  const base = typeof now === "number" ? now : Date.parse(now);
  return new Date(base + minutes * 60000).toISOString();
}
