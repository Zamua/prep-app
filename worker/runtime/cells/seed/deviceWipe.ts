import { insertCards, q, type SeedCard } from './cards.js';
import type { SeedContext } from './index.js';

/** Two past-due short cards: the snapshot a device holds while its owner
 * signs out. */
export async function profileDeviceWipe(ctx: SeedContext): Promise<Record<string, unknown>> {
  const deckId = ctx.repos.decks.create('device-capitals', { displayName: 'Device Capitals' });
  const cards: SeedCard[] = [
    ['0', q('short', 'Capital of Peru?', 'Lima'), { due: ctx.at({ hours: -1 }) }],
    ['1', q('short', 'Capital of Japan?', 'Tokyo'), { due: ctx.at({ hours: -2 }) }],
  ];
  const ids = insertCards(ctx, deckId, cards);
  return { deck_id: deckId, qids: [ids['0'], ids['1']] };
}
