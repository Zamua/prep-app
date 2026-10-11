// Where a study session's next pick may start its walk of the due index.
//
// A pick finds the earliest due card its session could be shown, so every
// card the session could still be shown is due no earlier. Two writes keep
// that true: a review rescheduling a card to no earlier than the floor, and
// an answer recorded, which only takes a card out. Any other write to what
// the pick reads, and anything the read cache drops for, forgets the floor,
// and the next pick walks from the start of the index. What a held floor
// skips is the walk over other decks' older due cards on every advance.
import { WRITE_SCHEDULED } from './cardRepo.js';
import { wordsOf, type LoggedWrite, type Mark, type ReadCache } from './readCache.js';
import { PICK_STATEMENTS, RECORD_ANSWER, RECORD_GRADED_ANSWER, type PickFloors } from './sessionRepo.js';
import type { SqlValue } from './storage.js';

const TABLES = wordsOf(PICK_STATEMENTS.join(' '));
const MAX_SESSIONS = 16;

interface Floor {
  deckId: number;
  at: string;
  mark: Mark;
}

const held = new WeakMap<ReadCache, Map<string, Floor>>();

/** Whether `w` leaves every card a session could be shown due at or after `at`. */
export function keepsFloor(w: Pick<LoggedWrite, 'sql' | 'bindings'>, at: string): boolean {
  if (w.sql === RECORD_ANSWER || w.sql === RECORD_GRADED_ANSWER) return true;
  const due = w.bindings?.[1];
  return w.sql === WRITE_SCHEDULED && typeof due === 'string' && due >= at;
}

export class HeldPickFloors implements PickFloors {
  constructor(private readonly cache: ReadCache) {}

  private floors(): Map<string, Floor> {
    let floors = held.get(this.cache);
    if (!floors) held.set(this.cache, (floors = new Map()));
    return floors;
  }

  floor(sid: string, deckId: number): string | null {
    const floors = this.floors();
    const f = floors.get(sid);
    if (!f || f.deckId !== deckId) return null;
    const writes = this.cache.writesSince(f.mark, TABLES);
    if (writes === null || !writes.every((w) => keepsFloor(w, f.at))) {
      floors.delete(sid);
      return null;
    }
    return f.at;
  }

  found(sid: string, deckId: number, nextDue: SqlValue): void {
    const floors = this.floors();
    floors.delete(sid);
    if (typeof nextDue !== 'string') return;
    if (floors.size >= MAX_SESSIONS) floors.delete(floors.keys().next().value!);
    floors.set(sid, { deckId, at: nextDue, mark: this.cache.mark() });
  }
}
