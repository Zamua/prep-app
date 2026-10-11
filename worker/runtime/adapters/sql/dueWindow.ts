// The two due reads the alarm's plan makes after every write, held across
// requests: how many cards are due, and when the next one comes due. Both
// move with the clock, so the read cache alone never repeats them. Held here,
// an answer stands while nothing has written the tables they read and the
// clock has not reached the next card's due stamp: until then no card can
// have come due, so both reads would return what they returned.
import type { CardRow, Question } from '../../../app/entities.js';
import type { CardRepo, Clock } from '../../../app/ports.js';
import type { ScheduledReview } from '../../../domain/fsrs/index.js';
import { COUNT_DUE, minutesUntil, NEXT_DUE, type SqlCardRepo } from './cardRepo.js';
import { wordsOf, type Mark, type ReadCache } from './readCache.js';
import { isoUtc } from './time.js';

const TABLES = wordsOf(`${COUNT_DUE} ${NEXT_DUE}`);

export interface DueWindow {
  /** The instant both reads were taken at, as bound. */
  at: string;
  /** The first stored `next_due` after `at`, or null when none is scheduled. */
  next: string | null;
  /** `countDue` per cap, each as of `at`. */
  counts: Map<number, number>;
  mark: Mark;
}

/**
 * Whether reads bound to `now` return what they returned bound to `at`. The
 * stamps compare as the statements compare them, as strings. No due stamp
 * lies in `(at, now]` when the first one after `at` is still ahead of `now`,
 * and `countDue`'s cards are a subset of `nextDueAfter`'s, so neither answer
 * has moved. The clock running backwards is a fresh read.
 */
export function windowHolds(w: { at: string; next: string | null }, now: string): boolean {
  return w.at <= now && (w.next === null || now < w.next);
}

const windows = new WeakMap<ReadCache, DueWindow>();

export class DueWindowCards implements CardRepo {
  constructor(
    private readonly cards: SqlCardRepo,
    private readonly cache: ReadCache,
    private readonly clock: Clock,
  ) {}

  srsState(qid: number): CardRow | null {
    return this.cards.srsState(qid);
  }

  effectiveRetention(qid: number): number | null {
    return this.cards.effectiveRetention(qid);
  }

  writeScheduled(qid: number, scheduled: ScheduledReview, reviewedAt: string): void {
    this.cards.writeScheduled(qid, scheduled, reviewedAt);
  }

  listCardStateForDeck(deckId: number): (CardRow & { prompt: string })[] {
    return this.cards.listCardStateForDeck(deckId);
  }

  restoreCardState(qid: number, fields: Partial<Omit<CardRow, 'question_id'>>): void {
    this.cards.restoreCardState(qid, fields);
  }

  dueQuestions(deckId: number, limit?: number): Question[] {
    return this.cards.dueQuestions(deckId, limit);
  }

  countDue(cap?: number): number {
    const at = isoUtc(this.clock.now());
    const w = this.window(at);
    const key = cap ?? -1;
    let n = w.counts.get(key);
    if (n === undefined) {
      n = this.cards.countDueAt(at, cap);
      w.counts.set(key, n);
    }
    return n;
  }

  nextDueMinutes(deckId: number | null = null): number | null {
    const now = this.clock.now();
    const at = isoUtc(now);
    return minutesUntil(deckId === null ? this.window(at).next : this.cards.nextDueAfter(at, deckId), now);
  }

  /** The window `at` falls in, read afresh when the held one no longer holds.
   * A next stamp that is not text is answered but never held: no string
   * compare bounds it. */
  private window(at: string): DueWindow {
    const held = windows.get(this.cache);
    if (held && windowHolds(held, at) && this.cache.holds(held.mark, TABLES)) return held;
    windows.delete(this.cache);
    const mark = this.cache.mark();
    const next = this.cards.nextDueAfter(at, null);
    const fresh: DueWindow = { at, next: next as string | null, counts: new Map(), mark };
    if (next === null || typeof next === 'string') windows.set(this.cache, fresh);
    return fresh;
  }
}
