import { insertCards, q, type SeedCard } from './cards.js';
import type { SeedContext } from './index.js';

/** The account a visitor holds before signing up: one instant deck with one
 * due card, owned by an anonymous profile so the merge admits it. */
export async function profileMergeAnon(ctx: SeedContext): Promise<Record<string, unknown>> {
  const deckId = ctx.repos.decks.create('instant-9f3c', { displayName: 'African capitals' });
  const cards: SeedCard[] = [['anon', q('short', 'Capital of Kenya?', 'Nairobi'), { due: ctx.at({ hours: -1 }) }]];
  const ids = insertCards(ctx, deckId, cards);
  return { deck_id: deckId, deck_name: 'instant-9f3c', question_id: ids['anon'] };
}
