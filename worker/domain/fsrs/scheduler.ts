// Port of py-fsrs 6.3.2 `Scheduler.review_card` and its math, Again and
// Good ratings only. Expressions keep the reference's operand order so the
// floating-point results agree bit for bit.
//
// py-fsrs: MIT License, Copyright (c) 2022 Open Spaced Repetition.
// Permission is hereby granted, free of charge, to any person obtaining a
// copy of this software and associated documentation files (the
// "Software"), to deal in the Software without restriction, including
// without limitation the rights to use, copy, modify, merge, publish,
// distribute, sublicense, and/or sell copies of the Software, and to permit
// persons to whom the Software is furnished to do so, subject to the
// following conditions: The above copyright notice and this permission
// notice shall be included in all copies or substantial portions of the
// Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
// EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
// MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
// NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
// DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
// OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
// USE OR OTHER DEALINGS IN THE SOFTWARE.

import { pyRound } from '../py';
import { fuzzedIntervalDays } from './fuzz';

export const DEFAULT_PARAMETERS: readonly number[] = [
  0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796, 1.4835, 0.0614, 0.2629, 1.6483,
  0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
];

export const STABILITY_MIN = 0.001;
export const MIN_DIFFICULTY = 1.0;
export const MAX_DIFFICULTY = 10.0;

export const Rating = { Again: 1, Good: 3 } as const;
export type Rating = (typeof Rating)[keyof typeof Rating];
const EASY = 4;

export const State = { Learning: 1, Review: 2, Relearning: 3 } as const;
export type State = (typeof State)[keyof typeof State];

const DAY_MS = 86_400_000;

export interface SchedulerConfig {
  parameters: readonly number[];
  desiredRetention: number;
  learningStepsMs: readonly number[];
  relearningStepsMs: readonly number[];
  maximumInterval: number;
  /** null for fuzz off; otherwise the uniform [0, 1) draw. */
  random: (() => number) | null;
}

export interface Card {
  stability: number | null;
  difficulty: number | null;
  state: State;
  /** Position in the (re)learning steps; null in Review. */
  step: number | null;
  lastReview: Date | null;
}

export interface Reviewed {
  stability: number;
  difficulty: number;
  state: State;
  step: number | null;
  intervalMs: number;
}

/** py-fsrs `assert card.step is not None` on a Relearning card. */
export class RelearningStepMissing extends Error {}

/** A state py-fsrs rejects: unknown, or Review without S and D. */
export class InvalidCardState extends Error {}

/** `timedelta.days`: whole days, floored. */
export function elapsedDays(now: Date, lastReview: Date): number {
  return Math.floor((now.getTime() - lastReview.getTime()) / DAY_MS);
}

function decay(w: readonly number[]): number {
  return -w[20]!;
}

function factor(w: readonly number[]): number {
  return Math.pow(0.9, 1 / decay(w)) - 1;
}

function clampDifficulty(d: number): number {
  return Math.min(Math.max(d, MIN_DIFFICULTY), MAX_DIFFICULTY);
}

function clampStability(s: number): number {
  return Math.max(s, STABILITY_MIN);
}

export function retrievability(w: readonly number[], card: Card, now: Date): number {
  if (card.lastReview === null || card.stability === null) return 0;
  const days = Math.max(0, elapsedDays(now, card.lastReview));
  return Math.pow(1 + (factor(w) * days) / card.stability, decay(w));
}

function initialStability(w: readonly number[], rating: Rating): number {
  return clampStability(w[rating - 1]!);
}

function initialDifficulty(w: readonly number[], rating: number, clamp: boolean): number {
  const d = w[4]! - Math.pow(Math.E, w[5]! * (rating - 1)) + 1;
  return clamp ? clampDifficulty(d) : d;
}

export function nextInterval(w: readonly number[], retention: number, maximumInterval: number, stability: number): number {
  const days = (stability / factor(w)) * (Math.pow(retention, 1 / decay(w)) - 1);
  return Math.min(Math.max(pyRound(days), 1), maximumInterval);
}

function shortTermStability(w: readonly number[], stability: number, rating: Rating): number {
  let increase = Math.pow(Math.E, w[17]! * (rating - 3 + w[18]!)) * Math.pow(stability, -w[19]!);
  if (rating === Rating.Good) increase = Math.max(increase, 1.0);
  return clampStability(stability * increase);
}

function nextDifficulty(w: readonly number[], difficulty: number, rating: Rating): number {
  const arg1 = initialDifficulty(w, EASY, false);
  const delta = -(w[6]! * (rating - 3));
  const arg2 = difficulty + ((10.0 - difficulty) * delta) / 9.0;
  const next = w[7]! * arg1 + (1 - w[7]!) * arg2;
  return clampDifficulty(next);
}

function nextForgetStability(w: readonly number[], difficulty: number, stability: number, r: number): number {
  const longTerm =
    w[11]! * Math.pow(difficulty, -w[12]!) * (Math.pow(stability + 1, w[13]!) - 1) * Math.pow(Math.E, (1 - r) * w[14]!);
  const shortTerm = stability / Math.pow(Math.E, w[17]! * w[18]!);
  return Math.min(longTerm, shortTerm);
}

function nextRecallStability(w: readonly number[], difficulty: number, stability: number, r: number): number {
  return (
    stability *
    (1 +
      Math.pow(Math.E, w[8]!) *
        (11 - difficulty) *
        Math.pow(stability, -w[9]!) *
        (Math.pow(Math.E, (1 - r) * w[10]!) - 1) *
        1 *
        1)
  );
}

function nextStability(w: readonly number[], difficulty: number, stability: number, r: number, rating: Rating): number {
  const next =
    rating === Rating.Again
      ? nextForgetStability(w, difficulty, stability, r)
      : nextRecallStability(w, difficulty, stability, r);
  return clampStability(next);
}

/** `review_card`: the updated card and its unfuzzed or fuzzed interval. */
export function reviewCard(card: Card, rating: Rating, now: Date, cfg: SchedulerConfig): Reviewed {
  const w = cfg.parameters;
  const days = card.lastReview ? elapsedDays(now, card.lastReview) : null;
  const recent = days !== null && days < 1;
  let stability: number;
  let difficulty: number;
  let state: State = card.state;
  let step = card.step;
  let intervalMs: number;

  switch (card.state) {
    case State.Learning: {
      if (step === null) throw new RelearningStepMissing('learning card without a step');
      if (card.stability === null || card.difficulty === null) {
        stability = initialStability(w, rating);
        difficulty = initialDifficulty(w, rating, true);
      } else if (recent) {
        stability = shortTermStability(w, card.stability, rating);
        difficulty = nextDifficulty(w, card.difficulty, rating);
      } else {
        stability = nextStability(w, card.difficulty, card.stability, retrievability(w, card, now), rating);
        difficulty = nextDifficulty(w, card.difficulty, rating);
      }
      const steps = cfg.learningStepsMs;
      if (steps.length === 0 || (step >= steps.length && rating === Rating.Good)) {
        state = State.Review;
        step = null;
        intervalMs = nextInterval(w, cfg.desiredRetention, cfg.maximumInterval, stability) * DAY_MS;
      } else if (rating === Rating.Again) {
        step = 0;
        intervalMs = steps[step]!;
      } else if (step + 1 === steps.length) {
        state = State.Review;
        step = null;
        intervalMs = nextInterval(w, cfg.desiredRetention, cfg.maximumInterval, stability) * DAY_MS;
      } else {
        step += 1;
        intervalMs = steps[step]!;
      }
      break;
    }
    case State.Review: {
      if (card.stability === null || card.difficulty === null) throw new InvalidCardState('review card without S and D');
      stability = recent
        ? shortTermStability(w, card.stability, rating)
        : nextStability(w, card.difficulty, card.stability, retrievability(w, card, now), rating);
      difficulty = nextDifficulty(w, card.difficulty, rating);
      if (rating === Rating.Again) {
        if (cfg.relearningStepsMs.length === 0) {
          intervalMs = nextInterval(w, cfg.desiredRetention, cfg.maximumInterval, stability) * DAY_MS;
        } else {
          state = State.Relearning;
          step = 0;
          intervalMs = cfg.relearningStepsMs[step]!;
        }
      } else {
        intervalMs = nextInterval(w, cfg.desiredRetention, cfg.maximumInterval, stability) * DAY_MS;
      }
      break;
    }
    case State.Relearning: {
      // The step is never persisted, so the reference's assertion always fires here.
      throw new RelearningStepMissing('relearning card without a step');
    }
  }

  if (cfg.random !== null && state === State.Review) {
    intervalMs = fuzzedIntervalDays(Math.floor(intervalMs / DAY_MS), cfg.random, cfg.maximumInterval) * DAY_MS;
  }
  return { stability, difficulty, state, step, intervalMs };
}
