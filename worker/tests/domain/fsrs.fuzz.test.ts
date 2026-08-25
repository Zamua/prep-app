import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { FsrsState, fuzzedIntervalDays, scheduleReview, type CardSRSState, type Verdict } from '../../domain/fsrs';
import { parseIso } from '../../domain/py';

// Fuzz on: S, D and state never change, and the whole-day interval of a
// Review result lands inside py-fsrs's draw range. The range formula here
// is an independent copy, not an import.

interface StateRow {
  stability: number | null;
  difficulty: number | null;
  fsrs_state: number;
  last_review: string | null;
}
interface Review {
  input: StateRow;
  verdict: Verdict;
  now: string;
  output?: StateRow & { next_due: string; interval_seconds: number };
}
interface Corpus {
  cases: { id: string; retention: number; reviews: Review[] }[];
}

const CORPUS = new URL('../../../tests/fixtures/parity/fsrs/corpus.json', import.meta.url).pathname;
const corpus: Corpus = JSON.parse(readFileSync(CORPUS, 'utf8'));
const DAY = 86400;

function lcg(seed: number): () => number {
  let x = seed >>> 0;
  return () => {
    x = (Math.imul(x, 1664525) + 1013904223) >>> 0;
    return x / 4294967296;
  };
}

// py-fsrs `_get_fuzz_range`, transcribed separately. The draw is
// `random() * (max - min + 1) + min` rounded, so max + 1 is reachable.
function range(days: number): [number, number] {
  let delta = 1;
  for (const [start, end, f] of [
    [2.5, 7, 0.15],
    [7, 20, 0.1],
    [20, Infinity, 0.05],
  ] as const) {
    delta += f * Math.max(Math.min(days, end) - start, 0);
  }
  let min = Math.max(2, Math.round(days - delta));
  const max = Math.min(Math.round(days + delta), 36500);
  min = Math.min(min, max);
  return [min, max];
}

function toState(row: StateRow): CardSRSState {
  return {
    stability: row.stability,
    difficulty: row.difficulty,
    fsrsState: row.fsrs_state as CardSRSState['fsrsState'],
    lastReview: row.last_review === null ? null : parseIso(row.last_review),
  };
}

const rows = corpus.cases.flatMap((c) => c.reviews.filter((r) => r.output).map((r) => ({ c, r })));

describe('fuzz on against the corpus', () => {
  it('Review results with an unfuzzed interval of at least 3 days land in range', () => {
    const random = lcg(20260314);
    let checked = 0;
    let offMax = 0;
    for (const { c, r } of rows) {
      const out = r.output!;
      if (out.fsrs_state !== FsrsState.Review || out.interval_seconds < 3 * DAY) continue;
      const now = parseIso(r.now);
      const off = scheduleReview(toState(r.input), r.verdict, now, { desiredRetention: c.retention, fuzz: false });
      const on = scheduleReview(toState(r.input), r.verdict, now, { desiredRetention: c.retention, fuzz: { random } });
      expect(on.state).toEqual(off.state);
      expect(on.stepBucket).toBe(off.stepBucket);
      const days = on.intervalSeconds / DAY;
      expect(Number.isInteger(days)).toBe(true);
      expect(on.nextDue.getTime()).toBe(now.getTime() + on.intervalSeconds * 1000);
      const [min, max] = range(out.interval_seconds / DAY);
      expect(days).toBeGreaterThanOrEqual(min);
      expect(days).toBeLessThanOrEqual(max + 1);
      if (days === max + 1) offMax++;
      checked++;
    }
    expect(checked).toBeGreaterThan(500);
    console.log(`fsrs fuzz: ${checked} Review transitions fuzzed, ${offMax} landed on max + 1`);
  });

  it('under 2.5 days, and every non-Review result, is identical to fuzz off', () => {
    let checked = 0;
    for (const { c, r } of rows) {
      const out = r.output!;
      if (out.fsrs_state === FsrsState.Review && out.interval_seconds >= 3 * DAY) continue;
      const now = parseIso(r.now);
      const off = scheduleReview(toState(r.input), r.verdict, now, { desiredRetention: c.retention, fuzz: false });
      const on = scheduleReview(toState(r.input), r.verdict, now, { desiredRetention: c.retention, fuzz: { random: () => 0.999999 } });
      expect(on).toEqual(off);
      checked++;
    }
    expect(checked).toBeGreaterThan(500);
  });
});

describe('fuzzedIntervalDays', () => {
  it('random 0 gives min; a draw near 1 gives max + 1, as the reference rounds', () => {
    expect(range(30)).toEqual([27, 33]);
    expect(fuzzedIntervalDays(30, () => 0, 36500)).toBe(27);
    expect(fuzzedIntervalDays(30, () => 0.5, 36500)).toBe(30);
    expect(fuzzedIntervalDays(30, () => 0.9, 36500)).toBe(33);
    expect(fuzzedIntervalDays(30, () => 0.999999, 36500)).toBe(34);
    expect(fuzzedIntervalDays(3, () => 0, 36500)).toBe(2);
    expect(fuzzedIntervalDays(3, () => 0.999999, 36500)).toBe(5);
  });
  it('below 2.5 days is unchanged and the maximum interval caps the draw', () => {
    expect(fuzzedIntervalDays(2, () => 0.999999, 36500)).toBe(2);
    expect(fuzzedIntervalDays(1, () => 0, 36500)).toBe(1);
    expect(fuzzedIntervalDays(36500, () => 0.999999, 36500)).toBe(36500);
  });
  it('64 draws on a 30-day interval spread over at least 3 values', () => {
    const random = lcg(7);
    const seen = new Set<number>();
    for (let i = 0; i < 64; i++) seen.add(fuzzedIntervalDays(30, random, 36500));
    expect(seen.size).toBeGreaterThanOrEqual(3);
    for (const d of seen) expect(d >= 27 && d <= 34).toBe(true);
  });
});
