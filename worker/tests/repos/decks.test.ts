import { describe, expect, it } from 'vitest';
import { DeckNameTaken } from '../../app/ports.js';
import { RowCapReached } from '../../domain/limits.js';
import { ANON_MAX_DECKS } from '../../domain/limits.js';
import { cell, H, TEST_NOW } from './setup.js';

const q = (prompt: string) => ({ type: 'short' as const, prompt, answer: 'a' });

describe('DeckRepo', () => {
  it('creates and reads a deck back with its stored column values', () => {
    const { repos, storage } = cell();
    const id = repos.decks.create('world-capitals', { contextPrompt: 'Capitals.', displayName: 'World Capitals' });
    expect(id).toBe(1);
    expect(repos.decks.findId('world-capitals')).toBe(1);
    expect(repos.decks.findName(1)).toBe('world-capitals');
    expect(repos.decks.getType(1)).toBe('srs');
    expect(repos.decks.getContextPrompt('world-capitals')).toBe('Capitals.');
    expect(repos.decks.getMeta(1)).toEqual({
      deck_id: 1,
      notifications_enabled: true,
      interval_minutes: null,
      session_size: 3,
      context_prompt: 'Capitals.',
      pinned: false,
      display_name: 'World Capitals',
    });
    expect(repos.decks.getMeta(99)).toEqual({ deck_id: 99, notifications_enabled: true, interval_minutes: null, session_size: 3, context_prompt: '', pinned: false, display_name: null });
    expect(storage.rows('decks')[0]).toMatchObject({ created_at: '2026-03-14T15:00:00+00:00', deck_type: 'srs', notifications_enabled: 1, trivia_session_size: 3 });
  });

  it('refuses a taken slug on create and on rename', () => {
    const { repos, storage } = cell();
    repos.decks.create('a');
    repos.decks.create('b');
    expect(() => repos.decks.create('a')).toThrow(DeckNameTaken);
    expect(repos.decks.rename('a', 'b')).toBe(false);
    expect(repos.decks.rename('zzz', 'c')).toBe(false);
    expect(repos.decks.rename('a', 'c')).toBe(true);
    expect(repos.decks.findId('c')).toBe(1);
  });

  it('getOrCreate resolves before it creates', () => {
    const { repos, storage } = cell();
    expect(repos.decks.getOrCreate('x')).toBe(1);
    expect(repos.decks.getOrCreate('x')).toBe(1);
    expect(repos.decks.getOrCreate('y')).toBe(2);
  });

  it('caps an anonymous account at ANON_MAX_DECKS, and only on the create', () => {
    const { repos, storage } = cell({ anonymous: true });
    for (let i = 0; i < ANON_MAX_DECKS; i++) repos.decks.create(`d${i}`);
    expect(() => repos.decks.create('one-more')).toThrow(RowCapReached);
    expect(() => repos.decks.getOrCreate('one-more')).toThrow(RowCapReached);
    expect(repos.decks.getOrCreate('d0')).toBe(1);
    expect(() => repos.decks.createTrivia('t', { topic: 't', intervalMinutes: 60 })).toThrow(RowCapReached);
    const signedIn = cell();
    for (let i = 0; i <= ANON_MAX_DECKS; i++) signedIn.repos.decks.create(`d${i}`);
  });

  it('lists summaries pinned first, then by display name, with due counts for SRS decks only', () => {
    const { repos, clock, storage } = cell();
    const b = repos.decks.create('bravo', { displayName: 'Bravo' });
    const a = repos.decks.create('alpha', { displayName: 'Alpha' });
    const t = repos.decks.createTrivia('trivia', { topic: 'x', intervalMinutes: 60, displayName: 'Trivia' });
    const due = repos.questions.add(a, q('due'));
    repos.questions.add(a, q('later'));
    const suspended = repos.questions.add(a, q('suspended'));
    repos.questions.setSuspended(suspended, true);
    repos.questions.add(t, q('trivia card'));
    repos.cards.restoreCardState(due, { next_due: '2026-03-14T14:00:00+00:00' });
    clock.advance(-1);
    expect(repos.decks.listSummaries().map((d) => [d.name, d.total, d.due, d.deck_type, d.pinned])).toEqual([
      ['alpha', 3, 1, 'srs', false],
      ['bravo', 0, 0, 'srs', false],
      ['trivia', 1, 0, 'trivia', false],
    ]);
    clock.set(TEST_NOW);
    expect(repos.decks.setPinned(b, true)).toBe(true);
    expect(repos.decks.listSummaries().map((d) => d.name)).toEqual(['bravo', 'alpha', 'trivia']);
    expect(storage.rows('decks').find((r) => r['id'] === b)?.['pinned_at']).toBe('2026-03-14T15:00:00+00:00');
    expect(repos.decks.dueBreakdown()).toEqual([['alpha', 2]]);
  });

  it('deletes the subtree and reports the count', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    repos.questions.add(d, q('p'));
    expect(repos.decks.delete('d')).toBe(1);
    expect(repos.decks.delete('d')).toBe(0);
    expect(storage.rows('questions')).toEqual([]);
    expect(storage.rows('cards')).toEqual([]);
  });

  it('trivia settings are bounds-checked and scoped to trivia decks', () => {
    const { repos, storage } = cell();
    const srs = repos.decks.create('srs');
    const t = repos.decks.createTrivia('t', { topic: 'History', intervalMinutes: 1440, displayName: 'T' });
    expect(repos.decks.getTriviaSourceMeta(t)).toEqual({ notification_interval_minutes: 1440, context_prompt: 'History' });
    expect(repos.decks.getTriviaSourceMeta(99)).toBeNull();
    expect(repos.decks.setNotificationInterval(t, 30)).toBe(true);
    expect(repos.decks.setNotificationInterval(srs, 30)).toBe(false);
    expect(() => repos.decks.setNotificationInterval(t, 0)).toThrow(RangeError);
    expect(repos.decks.setTriviaSessionSize(t, 5)).toBe(true);
    expect(repos.decks.getTriviaSessionSize(t)).toBe(5);
    expect(repos.decks.getTriviaSessionSize(srs)).toBe(3);
    expect(() => repos.decks.setTriviaSessionSize(t, 21)).toThrow(RangeError);
    expect(repos.decks.setNotificationsEnabled(srs, false)).toBe(true);
    expect(repos.decks.muteNotificationsUntil(t, '2026-03-15T00:00:00+00:00')).toBe(true);
    repos.decks.recordNotificationFire(t, '2026-03-14T15:00:00+00:00', 2);
    const [deck] = repos.decks.listTriviaDecks();
    expect(deck).toMatchObject({
      id: t,
      name: 't',
      deck_type: 'trivia',
      notification_interval_minutes: 30,
      notification_ignored_streak: 2,
      last_notified_at: '2026-03-14T15:00:00+00:00',
      trivia_session_size: 5,
      notifications_muted_until: '2026-03-15T00:00:00+00:00',
    });
    repos.decks.resetIgnoredStreakForDeck(t);
    expect(repos.decks.listTriviaDecks()[0]?.notification_ignored_streak).toBe(0);
  });

  it('retention override reads null until set and clears with null', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    expect(repos.decks.getDesiredRetention(d)).toBeNull();
    expect(repos.decks.setDesiredRetention(d, 0.95)).toBe(true);
    expect(repos.decks.getDesiredRetention(d)).toBe(0.95);
    expect(repos.decks.setDesiredRetention(d, null)).toBe(true);
    expect(repos.decks.getDesiredRetention(d)).toBeNull();
    expect(repos.decks.setDesiredRetention(42, 0.9)).toBe(false);
  });

  it('display name and context prompt update by slug', () => {
    const { repos, storage } = cell();
    repos.decks.create('d');
    expect(repos.decks.updateDisplayName('d', 'Deck')).toBe(true);
    expect(repos.decks.updateDisplayName('nope', 'Deck')).toBe(false);
    repos.decks.updateContextPrompt('d', 'ctx');
    expect(repos.decks.getContextPrompt('d')).toBe('ctx');
    expect(repos.decks.getContextPrompt('nope')).toBeNull();
  });

  it('stamps timestamps from the request clock', () => {
    const { repos, clock, storage } = cell();
    clock.set(new Date(TEST_NOW.getTime() + 2 * H + 123));
    repos.decks.create('d');
    expect(storage.rows('decks')[0]?.['created_at']).toBe('2026-03-14T17:00:00.123000+00:00');
  });
});
