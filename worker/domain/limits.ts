// Row ceilings for anonymous accounts. An anonymous account costs one
// generation to obtain, so its content is bounded by policy.

export const ANON_MAX_DECKS = 5;
export const ANON_MAX_QUESTIONS = 200;

/** Raised before any write, so nothing was inserted. */
export class RowCapReached extends Error {}

export interface AccountRows {
  /** null: no users row; the write's own foreign key answers that. */
  isAnonymous: boolean | null;
  decks: number;
  questions: number;
}

/** Whether the caps bind at all; an account they do not bind is never counted. */
export function isRowCapped(isAnonymous: boolean | null): boolean {
  return Boolean(isAnonymous);
}

export function assertUnderRowCap(
  account: AccountRows,
  add: { newDecks?: number; newQuestions?: number } = {},
): void {
  if (!isRowCapped(account.isAnonymous)) return;
  const { newDecks = 0, newQuestions = 0 } = add;
  if (account.decks + newDecks > ANON_MAX_DECKS || account.questions + newQuestions > ANON_MAX_QUESTIONS) {
    throw new RowCapReached(
      `guest account limit reached: ${ANON_MAX_DECKS} decks, ${ANON_MAX_QUESTIONS} cards. Create an account to add more.`,
    );
  }
}
