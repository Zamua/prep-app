import { describe, expect, it } from 'vitest';
import { ANON_MAX_DECKS, ANON_MAX_QUESTIONS, RowCapReached, assertUnderRowCap } from '../../domain/limits';

const MESSAGE = 'guest account limit reached: 5 decks, 200 cards. Create an account to add more.';

describe('assertUnderRowCap', () => {
  it('pins the caps', () => {
    expect([ANON_MAX_DECKS, ANON_MAX_QUESTIONS]).toEqual([5, 200]);
  });

  it('admits one under and refuses at each cap', () => {
    expect(() => assertUnderRowCap({ isAnonymous: true, decks: 4, questions: 0 }, { newDecks: 1 })).not.toThrow();
    expect(() => assertUnderRowCap({ isAnonymous: true, decks: 5, questions: 0 }, { newDecks: 1 })).toThrow(RowCapReached);
    expect(() => assertUnderRowCap({ isAnonymous: true, decks: 0, questions: 199 }, { newQuestions: 1 })).not.toThrow();
    expect(() => assertUnderRowCap({ isAnonymous: true, decks: 0, questions: 200 }, { newQuestions: 1 })).toThrow(RowCapReached);
    expect(() => assertUnderRowCap({ isAnonymous: true, decks: 5, questions: 200 })).not.toThrow();
  });

  it('the message is byte-exact', () => {
    let caught: unknown;
    try {
      assertUnderRowCap({ isAnonymous: true, decks: 5, questions: 0 }, { newDecks: 1 });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(RowCapReached);
    expect((caught as Error).message).toBe(MESSAGE);
  });

  it('a non-anonymous or missing user has no ceiling', () => {
    expect(() => assertUnderRowCap({ isAnonymous: false, decks: 999, questions: 9999 }, { newDecks: 1 })).not.toThrow();
    expect(() => assertUnderRowCap({ isAnonymous: null, decks: 999, questions: 9999 }, { newDecks: 1 })).not.toThrow();
  });
});
