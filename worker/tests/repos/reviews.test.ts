import { describe, expect, it } from 'vitest';
import { QuestionNotFound } from '../../app/ports.js';
import { parseIso } from '../../domain/time.js';
import { cell, D, H, M, TEST_NOW, at } from './setup.js';

describe('ReviewRepo.record', () => {
  const { repos, clock, storage } = cell();
  repos.prefs.setDesiredRetention(0.85);
  const deck = repos.decks.create('d', { displayName: 'D' });
  const qid = repos.questions.add(deck, { type: 'short', prompt: 'p', answer: 'a' });

  it('the first review of a fresh card enters the learning steps', () => {
    const r1 = repos.reviews.record(qid, 'right', 'a');
    expect(r1).toEqual({ step: 1, next_due: '2026-03-14T15:10:00+00:00', interval_minutes: 10 });
    expect(storage.rows('cards')[0]).toEqual({
      question_id: qid,
      step: 1,
      next_due: '2026-03-14T15:10:00+00:00',
      last_review: '2026-03-14T15:00:00+00:00',
      stability: 2.3065,
      difficulty: 2.11810397,
      fsrs_state: 1,
      learning_steps: 1,
    });
  });

  it('the second review a day later persists the scheduler output and the review row', () => {
    clock.set(at(TEST_NOW, D));
    const r2 = repos.reviews.record(qid, 'wrong', 'b', 'n');
    expect(r2).toEqual({ step: 0, next_due: '2026-03-15T15:01:00+00:00', interval_minutes: 1 });
    expect(storage.rows('cards')[0]).toEqual({
      question_id: qid,
      step: 0,
      next_due: '2026-03-15T15:01:00+00:00',
      last_review: '2026-03-15T15:00:00+00:00',
      stability: 0.57129918,
      difficulty: 7.39450274,
      fsrs_state: 1,
      learning_steps: 0,
    });
    expect(storage.rows('reviews').map(({ id: _id, ...r }) => r)).toEqual([
      { question_id: qid, ts: '2026-03-14T15:00:00+00:00', result: 'right', user_answer: 'a', grader_notes: '' },
      { question_id: qid, ts: '2026-03-15T15:00:00+00:00', result: 'wrong', user_answer: 'b', grader_notes: 'n' },
    ]);
  });
});

describe('ReviewRepo and CardRepo', () => {
  it('resolves retention deck first, then profile, then null', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    expect(repos.cards.effectiveRetention(qid)).toBeNull();
    repos.prefs.setDesiredRetention(0.8);
    expect(repos.cards.effectiveRetention(qid)).toBe(0.8);
    repos.decks.setDesiredRetention(d, 0.95);
    expect(repos.cards.effectiveRetention(qid)).toBe(0.95);
    expect(repos.cards.effectiveRetention(999)).toBeNull();
  });

  it('refuses a review of a question with no row or no card', () => {
    const { repos, storage } = cell();
    const t = repos.decks.createTrivia('t', { topic: 't', intervalMinutes: 60 });
    const trivia = repos.questions.add(t, { type: 'short', prompt: 'p', answer: 'a' });
    expect(() => repos.reviews.record(999, 'right', 'a')).toThrow(QuestionNotFound);
    expect(() => repos.reviews.record(trivia, 'right', 'a')).toThrow(QuestionNotFound);
    expect(() => repos.reviews.record(trivia, 'meh' as 'right', 'a')).toThrow(RangeError);
  });

  it('archive reads and writes bypass the scheduler', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    repos.cards.restoreCardState(qid, { step: 4, next_due: '2026-04-01T00:00:00+00:00', stability: 14, difficulty: 5, fsrs_state: 2 });
    repos.cards.restoreCardState(qid, {});
    expect(repos.cards.listCardStateForDeck(d)).toEqual([
      { prompt: 'p', question_id: qid, step: 4, next_due: '2026-04-01T00:00:00+00:00', last_review: null, stability: 14, difficulty: 5, fsrs_state: 2, learning_steps: 0 },
    ]);
    repos.reviews.importReview(qid, '2026-03-01T00:00:00+00:00', 'right', 'x', 'notes');
    repos.reviews.importReview(qid, '2026-02-01T00:00:00+00:00', 'wrong');
    expect(repos.reviews.listReviewsForDeck(d)).toEqual([
      { question_id: qid, prompt: 'p', ts: '2026-02-01T00:00:00+00:00', result: 'wrong', user_answer: '', grader_notes: '' },
      { question_id: qid, prompt: 'p', ts: '2026-03-01T00:00:00+00:00', result: 'right', user_answer: 'x', grader_notes: 'notes' },
    ]);
    expect(repos.reviews.getLastUserAnswer(qid)).toBe('');
    expect(repos.reviews.getLastUserAnswer(999)).toBeNull();
    expect(() => repos.reviews.importReview(qid, 't', 'nope' as 'right')).toThrow(RangeError);
  });

  it('counts due cards for enabled SRS decks and finds the next due minute', () => {
    const { repos, clock, storage } = cell();
    const d = repos.decks.create('d');
    const paused = repos.decks.create('paused');
    const t = repos.decks.createTrivia('t', { topic: 't', intervalMinutes: 60 });
    repos.decks.setNotificationsEnabled(paused, false);
    const a = repos.questions.add(d, { type: 'short', prompt: 'a', answer: 'a' });
    const b = repos.questions.add(d, { type: 'short', prompt: 'b', answer: 'a' });
    const s = repos.questions.add(d, { type: 'short', prompt: 's', answer: 'a' });
    repos.questions.add(paused, { type: 'short', prompt: 'p', answer: 'a' });
    repos.questions.add(t, { type: 'short', prompt: 't', answer: 'a' });
    repos.questions.setSuspended(s, true);
    repos.cards.restoreCardState(b, { next_due: '2026-03-14T15:00:20+00:00' });
    expect(repos.cards.countDue()).toBe(1);
    expect(repos.cards.nextDueMinutes()).toBe(1);
    expect(repos.cards.nextDueMinutes(d)).toBe(1);
    expect(repos.cards.nextDueMinutes(paused)).toBeNull();
    clock.set(at(TEST_NOW, H));
    expect(repos.cards.countDue()).toBe(2);
    expect(repos.cards.nextDueMinutes()).toBeNull();
    expect(repos.cards.dueQuestions(d, 5).map((x) => x.id).sort()).toEqual([a, b]);
    expect(repos.cards.dueQuestions(d, 1)).toHaveLength(1);
  });

  it('srsState reads the card row', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    expect(repos.cards.srsState(qid)).toEqual({ question_id: qid, step: 0, next_due: '2026-03-14T15:00:00+00:00', last_review: null, stability: null, difficulty: null, fsrs_state: 1, learning_steps: 0 });
    expect(repos.cards.srsState(999)).toBeNull();
  });

  it('persists the rung and graduates a new card on the second right', () => {
    const { repos, clock, storage } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    const first = repos.reviews.record(qid, 'right', 'a');
    clock.set(parseIso(first.next_due));
    const second = repos.reviews.record(qid, 'right', 'a');

    expect(first.interval_minutes).toBe(10);
    expect(second.interval_minutes).toBeGreaterThan(24 * 60);
    expect(storage.rows('cards')[0]).toMatchObject({ question_id: qid, fsrs_state: 2, learning_steps: 0 });
  });

  it('does not put an answered card back in the queue ten minutes on', () => {
    const { repos, clock } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    const first = repos.reviews.record(qid, 'right', 'a');
    clock.set(parseIso(first.next_due));
    repos.reviews.record(qid, 'right', 'a');
    clock.advance(10 * M);
    expect(repos.cards.countDue()).toBe(0);
    expect(repos.cards.nextDueMinutes(d)).toBeGreaterThan(24 * 60);
  });

  it('interval_minutes floors at one', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    const first = repos.reviews.record(qid, 'wrong', 'a');
    expect(first.interval_minutes).toBeGreaterThanOrEqual(1);
    expect(first.step).toBe(0);
    expect(first.next_due).toBe('2026-03-14T15:01:00+00:00');
    expect(M).toBe(60_000);
  });
});
