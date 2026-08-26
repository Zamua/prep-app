// The anonymous row cap over the cell's own counts; the rule is the domain's.
import { assertUnderRowCap, type AccountRows } from '../../../domain/limits.js';
import type { Db } from './storage.js';

export function accountRows(db: Db): AccountRows {
  const profile = db.first<{ is_anonymous: number }>('SELECT is_anonymous FROM profile LIMIT 1');
  const decks = db.first<{ n: number }>('SELECT COUNT(*) AS n FROM decks');
  const questions = db.first<{ n: number }>('SELECT COUNT(*) AS n FROM questions');
  return {
    isAnonymous: profile ? Boolean(profile.is_anonymous) : null,
    decks: Number(decks?.n ?? 0),
    questions: Number(questions?.n ?? 0),
  };
}

/** Throws `RowCapReached` before any write. */
export function refuseOverRowCap(db: Db, add: { newDecks?: number; newQuestions?: number }): void {
  assertUnderRowCap(accountRows(db), add);
}
