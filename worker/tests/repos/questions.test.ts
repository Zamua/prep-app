import { describe, expect, it } from 'vitest';
import { QuestionNotFound } from '../../app/ports.js';
import { ANON_MAX_QUESTIONS, RowCapReached } from '../../domain/limits.js';
import { cell } from './setup.js';

describe('QuestionRepo', () => {
  it('stores a question as Python does: JSON choices, list answers and rubrics, code-only fields', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const mcq = repos.questions.add(d, { type: 'mcq', prompt: 'Capital?', answer: 'Canberra', choices: ['Sydney', 'Canberra'], topic: 'oceania' });
    const multi = repos.questions.add(d, { type: 'multi', prompt: 'Which?', answer: ['Ottawa', 'Lima'], rubric: ['one', 'two'] });
    const short = repos.questions.add(d, { type: 'short', prompt: 'Short', answer: 'x', skeleton: 'ignored', language: 'ignored' });
    const code = repos.questions.add(d, { type: 'code', prompt: 'Code', answer: 'def f(): pass', skeleton: 'def f():\n    ...', language: 'python' });
    const rows = storage.rows('questions');
    expect(rows[0]).toMatchObject({ id: mcq, choices: '["Sydney", "Canberra"]', topic: 'oceania', suspended: 0, created_at: '2026-03-14T15:00:00+00:00' });
    expect(rows[1]).toMatchObject({ id: multi, answer: '["Ottawa", "Lima"]', rubric: '- one\n- two', choices: null });
    expect(rows[2]).toMatchObject({ id: short, skeleton: null, language: null });
    expect(rows[3]).toMatchObject({ id: code, skeleton: 'def f():\n    ...', language: 'python' });
    expect(repos.questions.get(mcq)).toEqual({
      id: mcq,
      deck_id: d,
      type: 'mcq',
      topic: 'oceania',
      prompt: 'Capital?',
      choices: ['Sydney', 'Canberra'],
      answer: 'Canberra',
      rubric: null,
      created_at: '2026-03-14T15:00:00+00:00',
      suspended: false,
      skeleton: null,
      language: null,
      explanation: null,
      answer_regex: null,
    });
    expect(repos.questions.get(999)).toBeNull();
  });

  it('seeds a cards row for an SRS deck only', () => {
    const { repos, storage } = cell();
    const srs = repos.decks.create('srs');
    const trivia = repos.decks.createTrivia('t', { topic: 't', intervalMinutes: 60 });
    const a = repos.questions.add(srs, { type: 'short', prompt: 'p', answer: 'a' });
    repos.questions.add(trivia, { type: 'short', prompt: 'p', answer: 'a' });
    expect(storage.rows('cards')).toEqual([{ question_id: a, step: 0, next_due: '2026-03-14T15:00:00+00:00', last_review: null, stability: null, difficulty: null, fsrs_state: 1 }]);
  });

  it('caps an anonymous account at ANON_MAX_QUESTIONS', () => {
    const { repos, storage } = cell({ anonymous: true });
    const d = repos.decks.create('d');
    for (let i = 0; i < ANON_MAX_QUESTIONS; i++) repos.questions.add(d, { type: 'short', prompt: `p${i}`, answer: 'a' });
    expect(() => repos.questions.add(d, { type: 'short', prompt: 'over', answer: 'a' })).toThrow(RowCapReached);
    expect(storage.rows('questions').length).toBe(ANON_MAX_QUESTIONS);
  });

  it('update keeps SRS state and rejects an unknown id', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    repos.cards.restoreCardState(qid, { step: 3, next_due: '2030-01-01T00:00:00+00:00' });
    repos.questions.update(qid, { type: 'mcq', prompt: 'p2', answer: 'b', choices: ['a', 'b'] });
    expect(repos.questions.get(qid)).toMatchObject({ type: 'mcq', prompt: 'p2', choices: ['a', 'b'] });
    expect(storage.rows('cards')[0]).toMatchObject({ step: 3, next_due: '2030-01-01T00:00:00+00:00' });
    expect(() => repos.questions.update(999, { type: 'short', prompt: 'x', answer: 'y' })).toThrow(QuestionNotFound);
  });

  it('lists deck cards by due then id, with attempts and rights', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const later = repos.questions.add(d, { type: 'short', prompt: 'later', answer: 'a' });
    const soon = repos.questions.add(d, { type: 'short', prompt: 'soon', answer: 'a' });
    repos.cards.restoreCardState(later, { next_due: '2026-03-20T00:00:00+00:00' });
    repos.cards.restoreCardState(soon, { next_due: '2026-03-10T00:00:00+00:00', step: 2 });
    repos.reviews.importReview(soon, '2026-03-01T00:00:00+00:00', 'right');
    repos.reviews.importReview(soon, '2026-03-02T00:00:00+00:00', 'wrong');
    const cards = repos.questions.listInDeck(d);
    expect(cards.map((c) => [c.id, c.step, c.attempts, c.rights])).toEqual([
      [soon, 2, 2, 1],
      [later, 0, 0, 0],
    ]);
    expect(repos.questions.promptsInDeck(d)).toEqual(['later', 'soon']);
  });

  it('moves questions to a deck the cell owns and refuses a missing destination', () => {
    const { repos, storage } = cell();
    const a = repos.decks.create('a');
    const b = repos.decks.create('b');
    const q1 = repos.questions.add(a, { type: 'short', prompt: 'p', answer: 'a' });
    const q2 = repos.questions.add(a, { type: 'short', prompt: 'p2', answer: 'a' });
    expect(repos.questions.moveToDeck([q1, q2], 999)).toBe(0);
    expect(repos.questions.moveToDeck([], b)).toBe(0);
    expect(repos.questions.moveToDeck([q1], b)).toBe(1);
    expect(repos.questions.get(q1)?.deck_id).toBe(b);
  });

  it('suspend, regex and delete', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    repos.questions.setSuspended(qid, true);
    expect(repos.questions.get(qid)?.suspended).toBe(true);
    expect(repos.questions.setAnswerRegex(qid, '(?i)^a$')).toBe(true);
    expect(repos.questions.setAnswerRegex(999, 'x')).toBe(false);
    expect(repos.questions.get(qid)?.answer_regex).toBe('(?i)^a$');
    expect(repos.questions.delete(qid)).toBe(true);
    expect(repos.questions.delete(qid)).toBe(false);
    expect(storage.rows('cards')).toEqual([]);
  });
});
