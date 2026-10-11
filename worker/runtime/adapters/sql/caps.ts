// The anonymous row cap over the cell's own counts; the rule is the domain's.
import { assertUnderRowCap, isRowCapped, type AccountRows } from '../../../domain/limits.js';
import type { Db } from './storage.js';

function isAnonymous(db: Db): boolean | null {
  const profile = db.first<{ is_anonymous: number }>('SELECT is_anonymous FROM profile LIMIT 1');
  return profile ? Boolean(profile.is_anonymous) : null;
}

/** A COUNT(*) reads every row it counts. */
function counted(db: Db, isAnon: boolean | null): AccountRows {
  const decks = db.first<{ n: number }>('SELECT COUNT(*) AS n FROM decks');
  const questions = db.first<{ n: number }>('SELECT COUNT(*) AS n FROM questions');
  return { isAnonymous: isAnon, decks: Number(decks?.n ?? 0), questions: Number(questions?.n ?? 0) };
}

export function accountRows(db: Db): AccountRows {
  return counted(db, isAnonymous(db));
}

/** Throws `RowCapReached` before any write. Counts only an account the cap binds. */
export function refuseOverRowCap(db: Db, add: { newDecks?: number; newQuestions?: number }): void {
  const isAnon = isAnonymous(db);
  if (!isRowCapped(isAnon)) return;
  assertUnderRowCap(counted(db, isAnon), add);
}
