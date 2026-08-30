import { describe, expect, it } from 'vitest';
import { SyncItemRejected } from '../../app/ports.js';
import { cell, D, H, M, TEST_NOW, at } from './setup.js';

describe('OfflineRepo: snapshot', () => {
  it('lists SRS decks in dashboard order with totals, and every unsuspended SRS card with its step', () => {
    const { repos } = cell();
    const b = repos.decks.create('bravo', { displayName: 'Bravo' });
    const a = repos.decks.create('alpha', { displayName: 'Alpha' });
    const t = repos.decks.createTrivia('t', { topic: 't', intervalMinutes: 60 });
    const q1 = repos.questions.add(a, { type: 'mcq', prompt: 'p', answer: 'a', choices: ['a', 'b'], rubric: 'r', explanation: 'e' });
    const s = repos.questions.add(a, { type: 'short', prompt: 's', answer: 'a' });
    repos.questions.setSuspended(s, true);
    repos.questions.add(t, { type: 'short', prompt: 'tq', answer: 'a' });
    repos.cards.restoreCardState(q1, { stability: 8 });
    repos.decks.setPinned(b, true);
    expect(repos.offline.snapshotDecks()).toEqual([
      { id: b, name: 'bravo', display_name: 'Bravo', pinned_at: '2026-03-14T15:00:00+00:00', total: 0 },
      { id: a, name: 'alpha', display_name: 'Alpha', pinned_at: null, total: 2 },
    ]);
    expect(repos.offline.snapshotCards()).toEqual([
      {
        question_id: q1,
        deck_id: a,
        type: 'mcq',
        prompt: 'p',
        choices: ['a', 'b'],
        answer: 'a',
        answer_regex: null,
        rubric: 'r',
        skeleton: null,
        explanation: 'e',
        step: 3,
        next_due: '2026-03-14T15:00:00+00:00',
      },
    ]);
  });
});

describe('OfflineRepo: sync writes', () => {
  it('creates a card with its ledger row in one transaction and rejects a non-SRS deck', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const t = repos.decks.createTrivia('t', { topic: 't', intervalMinutes: 60 });
    const qid = repos.offline.createCard('c1', d, 'prompt', 'answer', '(?i)^answer$');
    expect(storage.rows('questions')[0]).toMatchObject({ id: qid, type: 'short', prompt: 'prompt', answer: 'answer', answer_regex: '(?i)^answer$' });
    expect(storage.rows('cards')[0]).toMatchObject({ question_id: qid, step: 0, next_due: '2026-03-14T15:00:00+00:00' });
    expect(repos.idempotency.findSync('c1')).toEqual({ kind: 'card', status: 'created', question_id: qid });
    expect(repos.offline.resolveCardClientId('c1')).toBe(qid);
    expect(repos.offline.resolveCardClientId('nope')).toBeNull();
    expect(() => repos.offline.createCard('c2', t, 'p', 'a', null)).toThrow(SyncItemRejected);
    expect(() => repos.offline.createCard('c2', 999, 'p', 'a', null)).toThrow(SyncItemRejected);
    expect(() => repos.offline.createCard('c1', d, 'p', 'a', null)).toThrow(/UNIQUE|PRIMARY KEY/);
    expect(storage.rows('questions')).toHaveLength(1);
  });

  it('applies a review at the client instant, or only logs it when a later review owns the card', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    expect(repos.offline.applyReview('r1', qid, 'right', 'a', at(TEST_NOW, -H), 'auto')).toBe('applied');
    const card = repos.cards.srsState(qid)!;
    expect(card.last_review).toBe('2026-03-14T14:00:00+00:00');
    expect(card.stability).not.toBeNull();
    expect(repos.offline.applyReview('r2', qid, 'wrong', 'b', at(TEST_NOW, -2 * H), 'auto')).toBe('logged_no_reschedule');
    expect(repos.cards.srsState(qid)).toEqual(card);
    expect(storage.rows('reviews').map((r) => [r['ts'], r['result']])).toEqual([
      ['2026-03-14T14:00:00+00:00', 'right'],
      ['2026-03-14T13:00:00+00:00', 'wrong'],
    ]);
    expect(repos.idempotency.findSync('r2')).toEqual({ kind: 'review', status: 'logged_no_reschedule', question_id: qid });
    expect(() => repos.offline.applyReview('r3', 999, 'right', 'a', TEST_NOW, '')).toThrow(SyncItemRejected);
    expect(repos.idempotency.findSync('r3')).toBeNull();
  });

  it('persists the learning rung across successive offline reviews', () => {
    const { repos } = cell();
    const deck = repos.decks.create('d');
    const qid = repos.questions.add(deck, { type: 'short', prompt: 'p', answer: 'a' });

    expect(repos.offline.applyReview('r1', qid, 'right', 'a', TEST_NOW, '')).toBe('applied');
    expect(repos.cards.srsState(qid)).toMatchObject({ fsrs_state: 1, learning_steps: 1 });
    expect(repos.offline.applyReview('r2', qid, 'right', 'a', at(TEST_NOW, 10 * M), '')).toBe('applied');
    expect(repos.cards.srsState(qid)).toMatchObject({ fsrs_state: 2, learning_steps: 0 });
  });

  it('resolves the inbox and named decks SRS-only, suffixing past taken slugs', () => {
    const { repos } = cell();
    expect(repos.offline.findSrsInbox()).toEqual({ taken: false });
    const inbox = repos.offline.resolveSrsInbox();
    expect(repos.decks.findName(inbox)).toBe('inbox');
    expect(repos.offline.resolveSrsInbox()).toBe(inbox);
    const other = cell();
    other.repos.decks.createTrivia('inbox', { topic: 't', intervalMinutes: 60 });
    expect(other.repos.offline.findSrsInbox()).toEqual({ taken: true });
    expect(other.repos.decks.findName(other.repos.offline.resolveSrsInbox())).toBe('inbox-offline');

    const named = repos.offline.resolveNamedSrsDeck('My Deck!');
    expect(repos.decks.findName(named)).toBe('my-deck');
    expect(repos.offline.resolveNamedSrsDeck('My Deck!')).toBe(named);
    repos.decks.createTrivia('other-deck', { topic: 't', intervalMinutes: 60, displayName: 'Other Deck' });
    const suffixed = repos.offline.resolveNamedSrsDeck('Other Deck');
    expect(repos.decks.findName(suffixed)).toBe('other-deck-2');
    expect(repos.offline.findSrsDeckByLabel('Other Deck')).toBe(suffixed);
    expect(repos.decks.findName(repos.offline.resolveNamedSrsDeck('!!!'))).toBe('deck');
    expect(D).toBeGreaterThan(0);
  });
});
