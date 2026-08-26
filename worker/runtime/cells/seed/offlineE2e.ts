import { insertCards, q, type SeedCard } from './cards.js';
import type { SeedContext } from './index.js';

/** One SRS deck holding the three card shapes the offline loop grades, plus a
 * suspended card. Each due sits in its own past hour so oldest-first order is
 * pinned rather than incidental. */
export async function profileOfflineE2e(ctx: SeedContext): Promise<Record<string, unknown>> {
  const deckId = ctx.repos.decks.create('offline-e2e', { displayName: 'Offline E2E' });
  const cards: SeedCard[] = [
    ['mcq', q('mcq', 'Capital of France?', 'Paris', { choices: ['Paris', 'Lyon', 'Marseille'] }), { due: ctx.at({ hours: -3 }) }],
    ['regex', q('short', 'Capital of Peru?', 'Lima', { answer_regex: 'lima' }), { due: ctx.at({ hours: -2 }) }],
    ['short', q('short', 'What does the acronym SRS stand for?', 'Spaced repetition system.'), { due: ctx.at({ hours: -1 }) }],
    ['suspended', q('short', 'Suspended: never studied, still in the deck.', 'yes'), { due: ctx.at({ hours: -4 }), suspended: true }],
  ];
  const ids = insertCards(ctx, deckId, cards);
  return {
    deck_id: deckId,
    mcq_id: ids['mcq'],
    regex_id: ids['regex'],
    short_id: ids['short'],
    suspended_id: ids['suspended'],
  };
}
