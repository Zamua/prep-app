// Deck-level operations that span more than one repository: the pieces
// prep/decks/service.py owns, minus the workflow orchestration (phase 4).
import type { NewQuestion } from '../entities.js';
import type { UserRepos } from '../ports.js';

/** A split refused for a reason the form re-renders verbatim. */
export class SplitRejected extends Error {}

/** Inserts the question and, on a trivia deck, puts it in the rotation:
 * a trivia deck picks from the queue, never from `cards`. */
export function addQuestion(repos: UserRepos, deckId: number, q: NewQuestion): number {
  const qid = repos.questions.add(deckId, q);
  if (repos.decks.getType(deckId) === 'trivia') repos.trivia.appendCard(qid, deckId);
  return qid;
}

/** Pausing a deck also abandons its in-progress sessions: resuming a deck
 * the user just silenced is not what "pause" means. */
export function setNotificationsEnabled(repos: UserRepos, deckId: number, enabled: boolean): boolean {
  if (!repos.decks.setNotificationsEnabled(deckId, enabled)) return false;
  if (enabled) return true;
  repos.sessions.abandonAllForDeck(deckId);
  repos.trivia.abandonAllSessionsForDeck(deckId);
  return true;
}

export async function splitDeck(
  repos: UserRepos,
  input: { sourceDeckId: number; newDeckName: string; questionIds: readonly number[]; newTopicPrompt: string | null },
): Promise<number> {
  const cleaned = (input.newDeckName || '').trim();
  if (!cleaned) throw new SplitRejected('new deck name is required');
  if (!input.questionIds.length) throw new SplitRejected('select at least one card to move');
  if (repos.decks.findId(cleaned) !== null) throw new SplitRejected(`a deck named "${cleaned}" already exists`);

  const sourceType = repos.decks.getType(input.sourceDeckId);
  if (sourceType === null) throw new SplitRejected('source deck not found');

  const topicPrompt = (input.newTopicPrompt ?? '').trim() || null;
  let newId: number;
  if (sourceType === 'trivia') {
    const src = repos.decks.getTriviaSourceMeta(input.sourceDeckId);
    const interval = (src?.notification_interval_minutes ?? 0) || 30;
    const topic = topicPrompt || src?.context_prompt || cleaned;
    newId = repos.decks.createTrivia(cleaned, { topic, intervalMinutes: interval });
  } else {
    newId = repos.decks.create(cleaned, { contextPrompt: topicPrompt });
  }

  const moved = repos.questions.moveToDeck(input.questionIds, newId);
  if (moved === 0) {
    // Every requested id belonged elsewhere: drop the husk deck.
    repos.decks.delete(cleaned);
    throw new SplitRejected('none of the selected cards could be moved');
  }

  if (sourceType === 'trivia' && repos.trivia.getActiveSessionForDeck(input.sourceDeckId)) {
    // The moved cards no longer live here; a "resume" would point at them.
    await repos.trivia.replaceActive(input.sourceDeckId, { queue: [] });
    repos.trivia.completeSession(input.sourceDeckId);
  }
  return newId;
}
