// FSRS scheduling for prep: the two-verdict surface over ts-fsrs, plus the
// 0..5 stability bucket the card row and the offline shell still carry.

import { fsrs, Rating, State, StrategyMode, type CardInput } from 'ts-fsrs';

export type Verdict = 'right' | 'wrong';

export const FsrsState = { Learning: 1, Review: 2, Relearning: 3 } as const;
export type FsrsStateValue = (typeof FsrsState)[keyof typeof FsrsState];

/** The scheduler's whole memory of a card, one row of `cards`. */
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

export const MAXIMUM_INTERVAL_DAYS = 36500;

const LEARNING_STEPS = ['1m', '10m'] as const;
const RELEARNING_STEPS = ['10m'] as const;
const DAY_MS = 86_400_000;

/** Fuzz off, or on with the uniform [0, 1) draw that seeds it. */
export type Fuzz = false | { random: () => number };

export function freshState(): CardSRSState {
  return { stability: null, difficulty: null, fsrsState: FsrsState.Learning, lastReview: null };
}

/** Stability in days to the 0..5 ladder bucket. */
export function stepForStability(stability: number | null): number {
  if (stability === null || stability < 1) return 0;
  if (stability < 3) return 1;
  if (stability < 7) return 2;
  if (stability < 14) return 3;
  if (stability < 30) return 4;
  return 5;
}

const STABILITY_BY_STEP: Record<number, number> = { 1: 1.0, 2: 3.0, 3: 7.0, 4: 14.0, 5: 30.0 };

/** Import seed for a card at ladder `step`; difficulty is the FSRS midpoint. */
export function seedStateFromLadderStep(step: number, now: Date): CardSRSState {
  if (step <= 0) return freshState();
  return { stability: STABILITY_BY_STEP[step] ?? 30.0, difficulty: 5.0, fsrsState: FsrsState.Review, lastReview: now };
}

/** Anything outside the supported band, NaN included, resolves inside it. */
function clampRetention(wanted: number | null | undefined): number {
  const r = wanted ?? DEFAULT_DESIRED_RETENTION;
  if (!Number.isFinite(r)) return DEFAULT_DESIRED_RETENTION;
  return Math.min(MAX_DESIRED_RETENTION, Math.max(MIN_DESIRED_RETENTION, r));
}

function stateOf(raw: number | null | undefined): State {
  if (raw === FsrsState.Review) return State.Review;
  if (raw === FsrsState.Relearning) return State.Relearning;
  return State.Learning;
}

/**
 * The row as a ts-fsrs card. A row with no stability has never been
 * scheduled whatever state it claims, so it enters as New. The (re)learning
 * step is not persisted, so a card resumes at the head of its ladder.
 */
function toCard(state: CardSRSState, now: Date): CardInput {
  const scheduled = state.stability !== null && state.difficulty !== null;
  return {
    due: state.lastReview ?? now,
    stability: state.stability ?? 0,
    difficulty: state.difficulty ?? 0,
    elapsed_days: 0,
    scheduled_days: 0,
    learning_steps: 0,
    reps: 0,
    lapses: 0,
    state: scheduled ? stateOf(state.fsrsState) : State.New,
    last_review: scheduled ? state.lastReview : null,
  };
}

/** State + verdict + now to the next state; retention null means the default. */
export function scheduleReview(
  state: CardSRSState,
  verdict: Verdict,
  now: Date,
  opts: { desiredRetention?: number | null; fuzz: Fuzz },
): ScheduledReview {
  const scheduler = fsrs({
    request_retention: clampRetention(opts.desiredRetention),
    maximum_interval: MAXIMUM_INTERVAL_DAYS,
    learning_steps: LEARNING_STEPS,
    relearning_steps: RELEARNING_STEPS,
    enable_fuzz: opts.fuzz !== false,
  });
  if (opts.fuzz !== false) {
    const { random } = opts.fuzz;
    scheduler.useStrategy(StrategyMode.SEED, () => String(random()));
  }
  const { card } = scheduler.next(toCard(state, now), now, verdict === 'right' ? Rating.Good : Rating.Again);
  // ts-fsrs orders Good strictly after Hard, which can put Good one day past
  // the maximum once both saturate.
  const nextDue = new Date(Math.min(card.due.getTime(), now.getTime() + MAXIMUM_INTERVAL_DAYS * DAY_MS));
  return {
    state: {
      stability: card.stability,
      difficulty: card.difficulty,
      fsrsState: (card.state === State.New ? FsrsState.Learning : card.state) as FsrsStateValue,
      lastReview: now,
    },
    nextDue,
    intervalSeconds: Math.max(0, Math.trunc((nextDue.getTime() - now.getTime()) / 1000)),
    stepBucket: stepForStability(card.stability),
  };
}
