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

/** The memory state ts-fsrs will accept. A restored row can hold anything. */
const STABILITY_MIN = 0.001;
const DIFFICULTY_MIN = 1;
const DIFFICULTY_MAX = 10;

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
 * The row as a ts-fsrs card. A row with no usable memory state has never been
 * scheduled whatever state it claims, so it enters as New; one outside the
 * supported band is brought inside it, since a card the scheduler refuses is a
 * card its owner can never study again. The (re)learning step is not
 * persisted, so a card resumes at the head of its ladder.
 */
function toCard(state: CardSRSState, lastReview: Date | null, reviewedAt: Date): CardInput {
  const scheduled = Number.isFinite(state.stability) && Number.isFinite(state.difficulty);
  return {
    due: lastReview ?? reviewedAt,
    stability: scheduled ? Math.max(STABILITY_MIN, state.stability!) : 0,
    difficulty: scheduled ? Math.min(DIFFICULTY_MAX, Math.max(DIFFICULTY_MIN, state.difficulty!)) : 0,
    elapsed_days: 0,
    scheduled_days: 0,
    learning_steps: 0,
    reps: 0,
    lapses: 0,
    state: scheduled ? stateOf(state.fsrsState) : State.New,
    last_review: scheduled ? lastReview : null,
  };
}

/** A UTC midnight, so a whole number of days from it is another UTC midnight. */
const ANCHOR_MS = 0;

/**
 * Elapsed time is whole 24-hour periods, so the hour of day a card is answered
 * at never shifts its next interval. ts-fsrs reads elapsed time off the UTC
 * calendar dates of `last_review` and `now`, so it is handed a pair of UTC
 * midnights that many days apart; the interval it returns is relative to the
 * second of them and is re-anchored to the real clock by the caller.
 */
function anchor(lastReview: Date | null, now: Date): { lastReview: Date | null; reviewedAt: Date } {
  if (lastReview === null) return { lastReview: null, reviewedAt: new Date(ANCHOR_MS) };
  const elapsedDays = Math.max(0, Math.floor((now.getTime() - lastReview.getTime()) / DAY_MS));
  return { lastReview: new Date(ANCHOR_MS), reviewedAt: new Date(ANCHOR_MS + elapsedDays * DAY_MS) };
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
  const at = anchor(state.lastReview, now);
  const { card } = scheduler.next(
    toCard(state, at.lastReview, at.reviewedAt),
    at.reviewedAt,
    verdict === 'right' ? Rating.Good : Rating.Again,
  );
  const scheduledMs = card.due.getTime() - at.reviewedAt.getTime();
  // ts-fsrs orders Good strictly after Hard, which can put Good one day past
  // the maximum once both saturate.
  const nextDue = new Date(now.getTime() + Math.min(scheduledMs, MAXIMUM_INTERVAL_DAYS * DAY_MS));
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
