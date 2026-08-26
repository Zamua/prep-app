import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import {
  DEFAULT_PARAMETERS,
  FsrsState,
  MAX_DESIRED_RETENTION,
  MIN_DESIRED_RETENTION,
  RelearningStepMissing,
  freshState,
  scheduleReview,
  seedStateFromLadderStep,
  stepForStability,
  type CardSRSState,
  type Verdict,
} from '../../domain/fsrs';
import { isoUtc, parseIso } from '../../domain/py';
import { pythonJson } from '../pyoracle';

// Every corpus transition replayed from its own input, fuzz off. S and D
// within the header tolerance, everything else exact, and the counts match
// the header so a silently skipped row cannot pass.

interface StateRow {
  stability: number | null;
  difficulty: number | null;
  fsrs_state: number;
  last_review: string | null;
}
interface Review {
  input: StateRow;
  verdict: Verdict;
  elapsed: string;
  now: string;
  output?: StateRow & { next_due: string; interval_seconds: number; step_bucket: number };
  error?: { type: string };
}
interface Corpus {
  header: {
    parameters: number[];
    py_fsrs_version: string;
    float_tolerance: number;
    retention_clamp: [number, number];
    transitions: number;
    transitions_raised: number;
    step_bucket_of: Record<string, number>;
    step_thresholds: number[];
  };
  cases: { id: string; start: string; retention: number; reviews: Review[] }[];
}

const CORPUS = new URL('../../../tests/fixtures/parity/fsrs/corpus.json', import.meta.url).pathname;
const corpus: Corpus = JSON.parse(readFileSync(CORPUS, 'utf8'));
const TOL = corpus.header.float_tolerance;

function toState(row: StateRow): CardSRSState {
  return {
    stability: row.stability,
    difficulty: row.difficulty,
    fsrsState: row.fsrs_state as CardSRSState['fsrsState'],
    lastReview: row.last_review === null ? null : parseIso(row.last_review),
  };
}

let transitions = 0;
let raised = 0;
let maxS = 0;
let maxD = 0;
let exactS = 0;
let exactD = 0;

describe('the corpus header describes this port', () => {
  it('py-fsrs 6.3.2 default parameters, 1e-9, clamp [0.7, 0.97]', () => {
    expect(corpus.header.py_fsrs_version).toBe('6.3.2');
    expect(corpus.header.parameters).toEqual([...DEFAULT_PARAMETERS]);
    expect(TOL).toBe(1e-9);
    expect(corpus.header.retention_clamp).toEqual([MIN_DESIRED_RETENTION, MAX_DESIRED_RETENTION]);
  });
  it('step_bucket_of pins stepForStability at the thresholds', () => {
    for (const [s, bucket] of Object.entries(corpus.header.step_bucket_of)) expect(stepForStability(Number(s))).toBe(bucket);
    expect(corpus.header.step_thresholds).toEqual([1, 3, 7, 14, 30]);
    expect(stepForStability(null)).toBe(0);
    expect(stepForStability(0.999)).toBe(0);
    expect(stepForStability(29.999)).toBe(4);
  });
});

describe('every corpus sequence replays', () => {
  for (const c of corpus.cases) {
    it(`${c.id} (${c.start}, retention ${c.retention})`, () => {
      for (const r of c.reviews) {
        const state = toState(r.input);
        const now = parseIso(r.now);
        const run = () => scheduleReview(state, r.verdict, now, { desiredRetention: c.retention, fuzz: false });
        if (r.error) {
          expect(r.error.type).toBe('AssertionError');
          expect(run).toThrow(RelearningStepMissing);
          raised++;
          continue;
        }
        const out = r.output!;
        const got = run();
        const dS = Math.abs(got.state.stability! - out.stability!);
        const dD = Math.abs(got.state.difficulty! - out.difficulty!);
        maxS = Math.max(maxS, dS);
        maxD = Math.max(maxD, dD);
        if (dS === 0) exactS++;
        if (dD === 0) exactD++;
        expect(dS, `stability ${r.now}`).toBeLessThanOrEqual(TOL);
        expect(dD, `difficulty ${r.now}`).toBeLessThanOrEqual(TOL);
        expect(got.state.fsrsState).toBe(out.fsrs_state);
        expect(isoUtc(got.state.lastReview!)).toBe(out.last_review);
        expect(isoUtc(got.nextDue)).toBe(out.next_due);
        expect(got.intervalSeconds).toBe(out.interval_seconds);
        expect(got.stepBucket).toBe(out.step_bucket);
        transitions++;
      }
    });
  }

  it('counted every transition and every refusal in the header', () => {
    expect(transitions).toBe(corpus.header.transitions);
    expect(raised).toBe(corpus.header.transitions_raised);
    expect(transitions).toBe(5640);
    expect(raised).toBe(2117);
    console.log(
      `fsrs parity: ${transitions} transitions, max |dS| = ${maxS}, max |dD| = ${maxD}, ` +
        `bit-exact S ${exactS}/${transitions}, D ${exactD}/${transitions}`,
    );
  });
});

describe('the surface around the scheduler', () => {
  const now = parseIso('2026-03-14T15:00:00+00:00');
  it('freshState is a never-studied Learning card', () => {
    expect(freshState()).toEqual({ stability: null, difficulty: null, fsrsState: FsrsState.Learning, lastReview: null });
  });
  it('seedStateFromLadderStep follows the ladder table', () => {
    expect(seedStateFromLadderStep(0, now)).toEqual(freshState());
    expect(seedStateFromLadderStep(3, now)).toEqual({ stability: 7, difficulty: 5, fsrsState: FsrsState.Review, lastReview: now });
    expect(seedStateFromLadderStep(9, now).stability).toBe(30);
  });
  it('null retention is 0.9 and out-of-range retention clamps', () => {
    const s = seedStateFromLadderStep(5, now);
    const at = parseIso('2026-03-21T15:00:00+00:00');
    const run = (desiredRetention: number | null | undefined) => scheduleReview(s, 'right', at, { desiredRetention, fuzz: false });
    expect(run(null)).toEqual(run(0.9));
    expect(run(undefined)).toEqual(run(0.9));
    expect(run(0.5)).toEqual(run(0.7));
    expect(run(0.99)).toEqual(run(0.97));
    expect(run(0.7).nextDue.getTime()).toBeGreaterThan(run(0.97).nextDue.getTime());
  });
  it('NaN retention is the upper bound, as Python min/max leave it', () => {
    const s = seedStateFromLadderStep(5, now);
    const at = parseIso('2026-03-21T15:00:00+00:00');
    const want = pythonJson<[string, number]>(
      `import json
from datetime import datetime, timezone
import prep.domain.srs as srs
from prep.domain.srs import CardSRSState, Verdict, schedule_review
class NoFuzz(srs._FsrsScheduler):
    def __init__(self, *a, **k):
        k["enable_fuzzing"] = False
        super().__init__(*a, **k)
srs._FsrsScheduler = NoFuzz
srs._SCHEDULER_CACHE.clear()
s = CardSRSState(stability=30.0, difficulty=5.0, fsrs_state=2, last_review=datetime(2026, 3, 14, 15, tzinfo=timezone.utc))
r = schedule_review(s, Verdict.RIGHT, now=datetime(2026, 3, 21, 15, tzinfo=timezone.utc), desired_retention=float("nan"))
print(json.dumps([r.next_due.isoformat(), r.interval_seconds]))`,
    );
    const got = scheduleReview(s, 'right', at, { desiredRetention: NaN, fuzz: false });
    expect(isoUtc(got.nextDue)).toBe(want[0]);
    expect(got.intervalSeconds).toBe(want[1]);
    expect(got).toEqual(scheduleReview(s, 'right', at, { desiredRetention: 0.97, fuzz: false }));
  });
  it('fsrsState 0 or missing means Learning', () => {
    const zero = { ...freshState(), fsrsState: 0 as unknown as CardSRSState['fsrsState'] };
    const missing = { stability: null, difficulty: null, lastReview: null } as unknown as CardSRSState;
    const want = scheduleReview(freshState(), 'wrong', now, { fuzz: false });
    expect(scheduleReview(zero, 'wrong', now, { fuzz: false })).toEqual(want);
    expect(scheduleReview(missing, 'wrong', now, { fuzz: false })).toEqual(want);
    expect(want.intervalSeconds).toBe(60);
  });
});

// The corpus's elapsed offsets never leave a fractional day past 12h, so
// floor-versus-round on `timedelta.days` is pinned against Python directly.
// A wrong verdict on a Review card never reaches the fuzz path.
describe('elapsed days floor like timedelta.days', () => {
  const HOURS = [11, 13, 37, 47];
  const start = parseIso('2026-03-14T15:00:00+00:00');
  const want = pythonJson<[number, number, number, string, number][]>(
    `import json
from datetime import datetime, timedelta, timezone
from prep.domain.srs import CardSRSState, Verdict, schedule_review
start = datetime(2026, 3, 14, 15, tzinfo=timezone.utc)
out = []
for h in ${JSON.stringify(HOURS)}:
    s = CardSRSState(stability=7.0, difficulty=5.0, fsrs_state=2, last_review=start)
    r = schedule_review(s, Verdict.WRONG, now=start + timedelta(hours=h), desired_retention=0.9)
    out.append([r.state.stability, r.state.difficulty, r.state.fsrs_state, r.next_due.isoformat(), r.interval_seconds])
print(json.dumps(out))`,
  );
  it.each(HOURS.map((h, i) => [h, want[i]!] as const))('%s hours after the last review', (h, w) => {
    const state: CardSRSState = { stability: 7.0, difficulty: 5.0, fsrsState: FsrsState.Review, lastReview: start };
    const got = scheduleReview(state, 'wrong', new Date(start.getTime() + h * 3_600_000), { desiredRetention: 0.9, fuzz: false });
    expect(Math.abs(got.state.stability! - w[0])).toBeLessThanOrEqual(TOL);
    expect(Math.abs(got.state.difficulty! - w[1])).toBeLessThanOrEqual(TOL);
    expect(got.state.fsrsState).toBe(w[2]);
    expect(isoUtc(got.nextDue)).toBe(w[3]);
    expect(got.intervalSeconds).toBe(w[4]);
  });
  it('11h and 13h are both zero days, 37h and 47h both one, and the two differ', () => {
    expect(want[0]![0]).toBe(want[1]![0]);
    expect(want[2]![0]).toBe(want[3]![0]);
    expect(want[1]![0]).not.toBe(want[2]![0]);
  });
});
