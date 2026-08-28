import { describe, expect, it } from 'vitest';
import { SLUG_ALPHABET, SLUG_LENGTH } from '../../app/entities.js';
import { ANON_MAX_DECKS, RowCapReached } from '../../domain/limits.js';
import { cell } from './setup.js';

const CARDS = [
  { prompt: 'Year the Bastille fell?', answer: '1789', answer_regex: '1789' },
  { prompt: 'The Directory fell to whom?', answer: 'Napoleon', answer_regex: 'napoleon( bonaparte)?' },
];

describe('InstantRepo', () => {
  it('mints the account, the deck and the cards in one write', () => {
    const c = cell({ profile: false });
    const anon = 'anon:' + 'ab'.repeat(16);
    const r = c.repos.instant.createInstantDeck('French Revolution', CARDS, { id: anon, displayName: 'Guest' });
    expect(r.slug).toMatch(new RegExp(`^[${SLUG_ALPHABET}]{${SLUG_LENGTH}}$`));
    expect(c.repos.prefs.get()).toMatchObject({ login: anon, display_name: 'Guest', is_anonymous: 1, email: null });
    expect(c.storage.rows('decks')[0]).toMatchObject({ id: r.deck_id, name: r.slug, display_name: 'French Revolution', created_at: '2026-03-14T15:00:00+00:00' });
    expect(c.storage.rows('questions').map((q) => [q['type'], q['prompt'], q['answer_regex']])).toEqual([
      ['short', 'Year the Bastille fell?', '1789'],
      ['short', 'The Directory fell to whom?', 'napoleon( bonaparte)?'],
    ]);
    expect(c.storage.rows('cards').map((r) => [r['step'], r['next_due']])).toEqual([
      [0, '2026-03-14T15:00:00+00:00'],
      [0, '2026-03-14T15:00:00+00:00'],
    ]);
  });

  it('draws the slug from the seeded generator, one choice per character', () => {
    const a = cell();
    const b = cell();
    const first = a.repos.instant.createInstantDeck('x', CARDS, null).slug;
    expect(first).toBe(b.repos.instant.createInstantDeck('x', CARDS, null).slug);
    expect(a.repos.instant.createInstantDeck('y', CARDS, null).slug).not.toBe(first);
  });

  it('caps an existing anonymous account before writing anything', () => {
    const c = cell({ anonymous: true });
    for (let i = 0; i < ANON_MAX_DECKS; i++) c.repos.decks.create(`d${i}`);
    expect(() => c.repos.instant.createInstantDeck('x', CARDS, null)).toThrow(RowCapReached);
    expect(c.storage.rows('questions')).toEqual([]);
    const signedIn = cell();
    for (let i = 0; i < ANON_MAX_DECKS; i++) signedIn.repos.decks.create(`d${i}`);
    expect(signedIn.repos.instant.createInstantDeck('x', CARDS, null).deck_id).toBe(ANON_MAX_DECKS + 1);
  });
});
