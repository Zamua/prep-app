import { describe, expect, it } from 'vitest';
import {
  FsrsState,
  MAXIMUM_INTERVAL_DAYS,
  freshState,
  scheduleReview,
  seedStateFromLadderStep,
  stepForStability,
  type CardSRSState,
} from '../../domain/fsrs';

const NOW = new Date('2026-03-14T15:00:00Z');
const MINUTE = 60;
const DAY = 86_400;
const OFF = { fuzz: false } as const;

const review = (over: Partial<CardSRSState> = {}): CardSRSState => ({
  stability: 10,
  difficulty: 5,
  fsrsState: FsrsState.Review,
  lastReview: new Date(NOW.getTime() - 7 * DAY * 1000),
  ...over,
});

describe('the ladder bucket', () => {
  it('is the stability thresholds 1, 3, 7, 14, 30', () => {
    const table: [number | null, number][] = [
      [null, 0], [0, 0], [0.999, 0],
      [1, 1], [2.999, 1],
      [3, 2], [6.999, 2],
      [7, 3], [13.999, 3],
      [14, 4], [29.999, 4],
      [30, 5], [1000, 5],
    ];
    for (const [stability, bucket] of table) expect(stepForStability(stability), String(stability)).toBe(bucket);
  });
});

describe('the states a card row can hold', () => {
  it('freshState is a never-studied Learning card', () => {
    expect(freshState()).toEqual({ stability: null, difficulty: null, fsrsState: FsrsState.Learning, lastReview: null });
  });

  it('seedStateFromLadderStep follows the ladder table', () => {
    expect(seedStateFromLadderStep(0, NOW)).toEqual(freshState());
    expect(seedStateFromLadderStep(-1, NOW)).toEqual(freshState());
    expect(seedStateFromLadderStep(1, NOW)).toEqual({ stability: 1, difficulty: 5, fsrsState: FsrsState.Review, lastReview: NOW });
    expect(seedStateFromLadderStep(3, NOW)).toEqual({ stability: 7, difficulty: 5, fsrsState: FsrsState.Review, lastReview: NOW });
    expect(seedStateFromLadderStep(5, NOW).stability).toBe(30);
    expect(seedStateFromLadderStep(9, NOW).stability).toBe(30);
  });

  it('an unknown or missing state reads as Learning', () => {
    const want = scheduleReview(freshState(), 'wrong', NOW, OFF);
    const zero = { ...freshState(), fsrsState: 0 as unknown as CardSRSState['fsrsState'] };
    const junk = { ...freshState(), fsrsState: 47 as unknown as CardSRSState['fsrsState'] };
    const missing = { stability: null, difficulty: null, lastReview: null } as unknown as CardSRSState;
    for (const state of [zero, junk, missing]) expect(scheduleReview(state, 'wrong', NOW, OFF)).toEqual(want);
    expect(want.intervalSeconds).toBe(MINUTE);
  });

  // An archive can restore a row claiming Review with no stability; it is
  // scheduled as never-studied rather than refused.
  it('a row with no stability starts over whatever state it claims', () => {
    const claimed = review({ stability: null, difficulty: null });
    expect(scheduleReview(claimed, 'right', NOW, OFF)).toEqual(scheduleReview(freshState(), 'right', NOW, OFF));
  });

  // A card the scheduler refuses is a card its owner can never study again,
  // so a row outside the supported band schedules at the nearest edge of it.
  it('a memory state outside the supported band schedules at the band edge', () => {
    const at = (over: Partial<CardSRSState>) => scheduleReview(review(over), 'right', NOW, OFF);
    const table: [Partial<CardSRSState>, Partial<CardSRSState>][] = [
      [{ stability: 1e-7 }, { stability: 0.001 }],
      [{ stability: 0 }, { stability: 0.001 }],
      [{ stability: -4 }, { stability: 0.001 }],
      [{ difficulty: 0 }, { difficulty: 1 }],
      [{ difficulty: -50 }, { difficulty: 1 }],
      [{ difficulty: 11 }, { difficulty: 10 }],
      [{ stability: 0, difficulty: 0.5 }, { stability: 0.001, difficulty: 1 }],
    ];
    for (const [given, clamped] of table) expect(at(given), JSON.stringify(given)).toEqual(at(clamped));
  });

  it('a non-finite memory state starts over rather than throwing', () => {
    const scratch = scheduleReview(freshState(), 'right', NOW, OFF);
    for (const junk of [{ stability: NaN }, { difficulty: NaN }, { stability: Infinity }, { difficulty: -Infinity }]) {
      expect(scheduleReview(review(junk), 'right', NOW, OFF), JSON.stringify(junk)).toEqual(scratch);
    }
  });
});

describe('the two verdicts move a card through the states', () => {
  it('a fresh card enters the learning steps', () => {
    const wrong = scheduleReview(freshState(), 'wrong', NOW, OFF);
    expect(wrong.state.fsrsState).toBe(FsrsState.Learning);
    expect(wrong.intervalSeconds).toBe(MINUTE);

    const right = scheduleReview(freshState(), 'right', NOW, OFF);
    expect(right.state.fsrsState).toBe(FsrsState.Learning);
    expect(right.intervalSeconds).toBe(10 * MINUTE);
    expect(right.state.stability).toBeGreaterThan(0);
    expect(right.state.difficulty).toBeGreaterThanOrEqual(1);
    expect(right.state.difficulty).toBeLessThanOrEqual(10);
  });

  it('a right answer on a review card keeps it in review, days out', () => {
    const r = scheduleReview(review(), 'right', NOW, OFF);
    expect(r.state.fsrsState).toBe(FsrsState.Review);
    expect(r.state.stability!).toBeGreaterThan(10);
    expect(r.intervalSeconds % DAY).toBe(0);
    expect(r.intervalSeconds).toBeGreaterThanOrEqual(DAY);
  });

  // The lapse path: wrong sends a review card to relearning, and the next
  // right graduates it back with a day-scale interval.
  it('a wrong answer lapses to relearning and a right answer graduates back', () => {
    const lapse = scheduleReview(review(), 'wrong', NOW, OFF);
    expect(lapse.state.fsrsState).toBe(FsrsState.Relearning);
    expect(lapse.intervalSeconds).toBe(10 * MINUTE);
    expect(lapse.state.stability!).toBeLessThan(10);
    expect(lapse.state.difficulty!).toBeGreaterThan(5);

    const back = scheduleReview(lapse.state, 'right', new Date(NOW.getTime() + 600_000), OFF);
    expect(back.state.fsrsState).toBe(FsrsState.Review);
    expect(back.intervalSeconds % DAY).toBe(0);
    expect(back.intervalSeconds).toBeGreaterThanOrEqual(DAY);
  });

  it('a wrong answer in relearning stays in relearning', () => {
    const relearning = review({ stability: 1.4, difficulty: 8, fsrsState: FsrsState.Relearning, lastReview: NOW });
    const r = scheduleReview(relearning, 'wrong', new Date(NOW.getTime() + 600_000), OFF);
    expect(r.state.fsrsState).toBe(FsrsState.Relearning);
    expect(r.intervalSeconds).toBe(10 * MINUTE);
  });

  it('the result is self-consistent: due, interval and bucket agree', () => {
    for (const state of [freshState(), review(), seedStateFromLadderStep(4, NOW)]) {
      for (const verdict of ['right', 'wrong'] as const) {
        const r = scheduleReview(state, verdict, NOW, OFF);
        expect(r.state.lastReview).toEqual(NOW);
        expect(r.nextDue.getTime()).toBe(NOW.getTime() + r.intervalSeconds * 1000);
        expect(r.stepBucket).toBe(stepForStability(r.state.stability));
      }
    }
  });

  it('the interval never passes the maximum', () => {
    const huge = review({ stability: 1e6, difficulty: 1 });
    expect(scheduleReview(huge, 'right', NOW, OFF).intervalSeconds).toBe(MAXIMUM_INTERVAL_DAYS * DAY);
  });
});

describe('desired retention', () => {
  const seed = seedStateFromLadderStep(5, NOW);
  const later = new Date(NOW.getTime() + 7 * DAY * 1000);
  const run = (desiredRetention: number | null | undefined) => scheduleReview(seed, 'right', later, { desiredRetention, fuzz: false });

  it('null, undefined and a value out of range all resolve inside the band', () => {
    expect(run(null)).toEqual(run(0.9));
    expect(run(undefined)).toEqual(run(0.9));
    expect(run(NaN)).toEqual(run(0.9));
    expect(run(0.5)).toEqual(run(0.7));
    expect(run(0.99)).toEqual(run(0.97));
  });

  it('asking for less retention buys a longer interval', () => {
    expect(run(0.7).intervalSeconds).toBeGreaterThan(run(0.9).intervalSeconds);
    expect(run(0.9).intervalSeconds).toBeGreaterThan(run(0.97).intervalSeconds);
  });
});

describe('fuzz', () => {
  const seed = seedStateFromLadderStep(5, NOW);
  const later = new Date(NOW.getTime() + 7 * DAY * 1000);
  const on = (random: () => number) => scheduleReview(seed, 'right', later, { desiredRetention: 0.9, fuzz: { random } });
  const plain = scheduleReview(seed, 'right', later, { desiredRetention: 0.9, fuzz: false });

  it('moves the interval and nothing else', () => {
    for (let i = 0; i < 64; i++) {
      const r = on(() => i / 64);
      expect(r.state).toEqual(plain.state);
      expect(r.stepBucket).toBe(plain.stepBucket);
      expect(r.nextDue.getTime()).toBe(later.getTime() + r.intervalSeconds * 1000);
      expect(r.intervalSeconds % DAY).toBe(0);
    }
  });

  // FSRS fuzz is a small band around the interval, not a free hand.
  it('stays within a few percent of the unfuzzed interval', () => {
    const days = plain.intervalSeconds / DAY;
    for (let i = 0; i < 64; i++) {
      const got = on(() => i / 64).intervalSeconds / DAY;
      expect(got).toBeGreaterThanOrEqual(2);
      expect(Math.abs(got - days)).toBeLessThanOrEqual(3 + 0.06 * days);
    }
  });

  it('the same draw gives the same interval, and different draws spread', () => {
    expect(on(() => 0.25).intervalSeconds).toBe(on(() => 0.25).intervalSeconds);
    const seen = new Set<number>();
    for (let i = 0; i < 64; i++) seen.add(on(() => i / 64).intervalSeconds);
    expect(seen.size).toBeGreaterThanOrEqual(3);
  });

  it('leaves the learning and relearning steps alone', () => {
    const draw = () => 0.999999;
    for (const [state, verdict] of [
      [freshState(), 'right'],
      [freshState(), 'wrong'],
      [review(), 'wrong'],
    ] as const) {
      const fuzzed = scheduleReview(state, verdict, NOW, { fuzz: { random: draw } });
      expect(fuzzed).toEqual(scheduleReview(state, verdict, NOW, OFF));
    }
  });

  it('leaves a short interval alone', () => {
    const short = review({ stability: 0.5, difficulty: 9, lastReview: NOW });
    const at = new Date(NOW.getTime() + DAY * 1000);
    expect(scheduleReview(short, 'right', at, OFF).intervalSeconds).toBe(2 * DAY);
    for (const draw of [0, 0.5, 0.999999]) {
      expect(scheduleReview(short, 'right', at, { fuzz: { random: () => draw } })).toEqual(scheduleReview(short, 'right', at, OFF));
    }
  });
});

describe('elapsed time', () => {
  const late = new Date('2026-03-10T22:00:00Z');
  const card = (): CardSRSState => ({ stability: 30, difficulty: 5, fsrsState: FsrsState.Review, lastReview: late });
  const after = (hours: number) =>
    scheduleReview(card(), 'right', new Date(late.getTime() + hours * 3600_000), { desiredRetention: 0.9, fuzz: false })
      .intervalSeconds / DAY;

  it('is whole 24-hour periods, so the hour of day answered never moves the interval', () => {
    for (const [a, b] of [[1, 23], [25, 47], [49, 71]] as const) expect(after(a), `${a}h vs ${b}h`).toBe(after(b));
  });

  it('counts a period only once it has fully passed, crossing midnight or not', () => {
    expect(after(23)).toBe(after(0));
    expect(after(24)).toBeGreaterThan(after(23));
    expect(after(48)).toBeGreaterThan(after(24));
  });

  it('a review before the last one reads as no time passed', () => {
    const backwards = new Date(late.getTime() - 5 * 3600_000);
    expect(scheduleReview(card(), 'right', backwards, OFF).intervalSeconds / DAY).toBe(after(0));
  });
});
