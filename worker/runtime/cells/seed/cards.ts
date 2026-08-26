// The card sets the profiles share, with the column pins each card takes.
import type { NewQuestion, QuestionType } from '../../../app/entities.js';
import { pyJsonDumps } from '../../../domain/py.js';
import type { SeedContext } from './index.js';

export interface CardPins {
  due: string;
  step?: number;
  last_review?: string;
  suspended?: boolean;
}

export type SeedCard = [key: string, question: NewQuestion, pins: CardPins];

export function q(type: QuestionType, prompt: string, answer: string, extra: Partial<NewQuestion> = {}): NewQuestion {
  return { type, prompt, answer, ...extra };
}

/** The World Capitals deck: one of each type, a suspended card, distinct due minutes. */
export function capitalsCards(at: SeedContext['at']): SeedCard[] {
  return [
    [
      'mcq',
      q('mcq', 'Which city is the capital of Australia?', 'Canberra', { choices: ['Sydney', 'Canberra', 'Melbourne', 'Perth'], topic: 'oceania' }),
      { due: at({ hours: -3 }), step: 2, last_review: at({ days: -2 }) },
    ],
    [
      'short_regex',
      q('short', 'Capital of Kenya?', 'Nairobi', { answer_regex: '(?i)^\\s*nairobi\\s*$', topic: 'africa' }),
      { due: at({ hours: -2 }), step: 1, last_review: at({ days: -1 }) },
    ],
    [
      'multi',
      q('multi', 'Which of these are national capitals?', pyJsonDumps(['Ottawa', 'Lima']), {
        choices: ['Ottawa', 'Toronto', 'Lima', 'Rio de Janeiro'],
        topic: 'americas',
      }),
      { due: at({ hours: -1 }) },
    ],
    [
      'code',
      q('code', 'Return the capital for a country code from `table`, or `None` when unknown.', 'def capital(code, table):\n    return table.get(code)\n', {
        language: 'python',
        skeleton: 'def capital(code, table):\n    ...\n',
        rubric: '- Uses dict.get\n- Returns None on a miss',
        topic: 'python',
      }),
      { due: at({ days: 2 }), step: 3, last_review: at({ days: -5 }) },
    ],
    ['short_plain', q('short', 'Capital of Peru?', 'Lima', { topic: 'americas' }), { due: at({ days: 5 }), step: 4, last_review: at({ days: -9 }) }],
    ['suspended', q('short', 'Capital of Ghana?', 'Accra', { topic: 'africa' }), { due: at({ hours: -4 }), suspended: true }],
  ];
}

export function insertCards(ctx: SeedContext, deckId: number, cards: readonly SeedCard[]): Record<string, number> {
  const ids: Record<string, number> = {};
  for (const [key, question, pins] of cards) {
    const qid = ctx.repos.questions.add(deckId, question);
    ids[key] = qid;
    ctx.repos.cards.restoreCardState(qid, { next_due: pins.due, step: pins.step ?? 0, last_review: pins.last_review ?? null });
    if (pins.suspended) ctx.repos.questions.setSuspended(qid, true);
  }
  return ids;
}
