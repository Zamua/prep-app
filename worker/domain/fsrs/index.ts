// FSRS scheduling for prep: the two-verdict surface over the py-fsrs port
// in scheduler.ts, plus the legacy ladder-step bucket templates still read.

import {
  DEFAULT_PARAMETERS,
  InvalidCardState,
  Rating,
  State,
  reviewCard,
  type Card,
  type SchedulerConfig,
} from './scheduler';
import { pyRound } from '../py';

export { RelearningStepMissing, InvalidCardState, DEFAULT_PARAMETERS } from './scheduler';
export { fuzzRange, fuzzedIntervalDays } from './fuzz';

export type Verdict = 'right' | 'wrong';

export const FsrsState = { Learning: 1, Review: 2, Relearning: 3 } as const;
export type FsrsStateValue = (typeof FsrsState)[keyof typeof FsrsState];

export interface CardSRSState {
  stability: number | null;
  difficulty: number | null;
  fsrsState: FsrsStateValue;
  lastReview: Date | null;
}

export interface ScheduledReview {
  state: CardSRSState;
  nextDue: Date;
  intervalSeconds: number;
  stepBucket: number;
}

export const DEFAULT_DESIRED_RETENTION = 0.9;
export const MIN_DESIRED_RETENTION = 0.7;
export const MAX_DESIRED_RETENTION = 0.97;
export const TERMINAL_STEP = 5;

export const LEARNING_STEPS_MS: readonly number[] = [60_000, 600_000];
export const RELEARNING_STEPS_MS: readonly number[] = [600_000];
export const MAXIMUM_INTERVAL_DAYS = 36500;

/** Fuzz off, or on with the uniform [0, 1) draw to use. */
export type Fuzz = false | { random: () => number };

export function freshState(): CardSRSState {
  return { stability: null, difficulty: null, fsrsState: FsrsState.Learning, lastReview: null };
}

/** Stability in days to the legacy 0..5 ladder bucket. */
export function stepForStability(stability: number | null): number {
  if (stability === null || stability < 1) return 0;
  if (stability < 3) return 1;
  if (stability < 7) return 2;
  if (stability < 14) return 3;
  if (stability < 30) return 4;
  return 5;
}

const STABILITY_BY_STEP: Record<number, number> = { 1: 1.0, 2: 3.0, 3: 7.0, 4: 14.0, 5: 30.0 };

/** Migration seed for a card at ladder `step`; difficulty is the FSRS midpoint. */
export function seedStateFromLadderStep(step: number, now: Date): CardSRSState {
  if (step <= 0) return freshState();
  return { stability: STABILITY_BY_STEP[step] ?? 30.0, difficulty: 5.0, fsrsState: FsrsState.Review, lastReview: now };
}

function stateOf(raw: number | null | undefined): State {
  if (!raw) return State.Learning;
  if (raw === State.Learning || raw === State.Review || raw === State.Relearning) return raw;
  throw new InvalidCardState(`unknown card state: ${raw}`);
}

/** State + verdict + now to the next state; retention null means the default. */
export function scheduleReview(
  state: CardSRSState,
  verdict: Verdict,
  now: Date,
  opts: { desiredRetention?: number | null; fuzz: Fuzz },
): ScheduledReview {
  const wanted = opts.desiredRetention ?? DEFAULT_DESIRED_RETENTION;
  // Python's min/max keep the bound when the other side is NaN.
  const clamped = Number.isNaN(wanted) ? MAX_DESIRED_RETENTION : Math.max(MIN_DESIRED_RETENTION, Math.min(MAX_DESIRED_RETENTION, wanted));
  const retention = pyRound(clamped, 3);
  const cfg: SchedulerConfig = {
    parameters: DEFAULT_PARAMETERS,
    desiredRetention: retention,
    learningStepsMs: LEARNING_STEPS_MS,
    relearningStepsMs: RELEARNING_STEPS_MS,
    maximumInterval: MAXIMUM_INTERVAL_DAYS,
    random: opts.fuzz ? opts.fuzz.random : null,
  };
  const fsrsState = stateOf(state.fsrsState);
  const card: Card = {
    stability: state.stability,
    difficulty: state.difficulty,
    state: fsrsState,
    step: fsrsState === State.Learning ? 0 : null,
    lastReview: state.lastReview,
  };
  const r = reviewCard(card, verdict === 'right' ? Rating.Good : Rating.Again, now, cfg);
  const nextDue = new Date(now.getTime() + r.intervalMs);
  return {
    state: { stability: r.stability, difficulty: r.difficulty, fsrsState: r.state, lastReview: now },
    nextDue,
    intervalSeconds: Math.max(0, Math.trunc((nextDue.getTime() - now.getTime()) / 1000)),
    stepBucket: stepForStability(r.stability),
  };
}
