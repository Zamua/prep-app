import { capitalsCards, insertCards, q, type SeedCard } from './cards.js';
import { DEVICE_LABEL, type SeedContext } from './index.js';

/** One deck with every card type due, the mcq first, and a session one
 * answer in. Dues sit in distinct wall-clock hours. */
export async function profileStudy(ctx: SeedContext): Promise<Record<string, unknown>> {
  const { repos, at } = ctx;
  const d = repos.decks.create('geography', { contextPrompt: 'Physical and political geography.', displayName: 'Geography' });
  const pins: Record<string, string> = {
    mcq: at({ hours: -5 }),
    short_regex: at({ hours: -4 }),
    multi: at({ hours: -3 }),
    code: at({ hours: -2 }),
    short_plain: at({ hours: -1 }),
  };
  const cards: SeedCard[] = capitalsCards(at)
    .filter(([key]) => key !== 'suspended')
    .map(([key, question]) => [key, question, { due: pins[key]! }]);
  cards.push([
    'warmup',
    q('mcq', 'Which continent is Egypt in?', 'Africa', { choices: ['Africa', 'Asia', 'Europe'], topic: 'geography' }),
    { due: at({ days: 1 }), step: 1, last_review: at({ minutes: -4 }) },
  ]);
  const ids = insertCards(ctx, d, cards);
  const sid = await repos.sessions.create(d, DEVICE_LABEL);
  repos.pins.session(sid, at({ minutes: -2 }), at({ minutes: -6 }));
  repos.pins.answerInSession(sid, ids['warmup']!, at({ minutes: -4 }), 'right');
  repos.reviews.importReview(ids['warmup']!, at({ minutes: -4 }), 'right', 'Africa', null);
  return { deck: { id: d, slug: 'geography', display: 'Geography' }, questions: ids, session_id: sid };
}
