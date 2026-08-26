import { describe, expect, it } from 'vitest';
import { QuestionNotFound } from '../../app/ports.js';
import { pythonJson } from '../pyoracle.js';
import { cell, D, H, M, PARITY_NOW, at } from './setup.js';

// Python's ReviewRepo.record on a fresh card, then a second review a day
// later, under the pinned clock. Learning-step intervals carry no fuzz, so
// the rows compare exactly.
const PY_RECORD = `
import os, tempfile, json
from datetime import datetime, timezone
d = tempfile.mkdtemp()
os.environ["PREP_DB_PATH"] = os.path.join(d, "x.sqlite")
os.environ["PREP_FAKE_NOW"] = "2026-03-14T15:00:00Z"
from prep.infrastructure import db, clock
db.init()
from prep.auth.repo import UserRepo
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.decks.entities import NewQuestion, QuestionType
from prep.study.repo import ReviewRepo
u = "parity@example.com"
UserRepo().upsert(u, email=u, display_name="Parity")
UserRepo().set_desired_retention(u, 0.85)
deck = DeckRepo().create(u, "d", display_name="D")
qid = QuestionRepo().add(u, deck, NewQuestion(type=QuestionType.SHORT, prompt="p", answer="a"))
def row():
    with db.cursor() as c:
        return dict(c.execute("SELECT * FROM cards WHERE question_id = ?", (qid,)).fetchone())
out = {}
out["r1"] = ReviewRepo().record(u, qid, "right", "a").model_dump(); out["row1"] = row()
clock.set_clock(clock.FixedClock(datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)))
out["r2"] = ReviewRepo().record(u, qid, "wrong", "b", notes="n").model_dump(); out["row2"] = row()
with db.cursor() as c:
    out["reviews"] = [dict(r) for r in c.execute("SELECT question_id, ts, result, user_answer, grader_notes FROM reviews ORDER BY id")]
print(json.dumps(out))
`;

interface Oracle {
  r1: { step: number; next_due: string; interval_minutes: number };
  row1: Record<string, unknown>;
  r2: { step: number; next_due: string; interval_minutes: number };
  row2: Record<string, unknown>;
  reviews: Record<string, unknown>[];
}

describe('ReviewRepo.record persists FSRS state as Python does', () => {
  const py = pythonJson<Oracle>(PY_RECORD);
  const { repos, clock, storage } = cell();
  repos.prefs.setDesiredRetention(0.85);
  const deck = repos.decks.create('d', { displayName: 'D' });
  const qid = repos.questions.add(deck, { type: 'short', prompt: 'p', answer: 'a' });

  it('the first review of a fresh card', () => {
    const r1 = repos.reviews.record(qid, 'right', 'a');
    expect(r1).toEqual(py.r1);
    expect(storage.rows('cards')[0]).toEqual(py.row1);
  });

  it('the second review a day later', () => {
    clock.set(at(PARITY_NOW, D));
    const r2 = repos.reviews.record(qid, 'wrong', 'b', 'n');
    expect(r2).toEqual(py.r2);
    expect(storage.rows('cards')[0]).toEqual(py.row2);
    expect(storage.rows('reviews').map(({ id: _id, ...r }) => r)).toEqual(py.reviews);
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
      { prompt: 'p', question_id: qid, step: 4, next_due: '2026-04-01T00:00:00+00:00', last_review: null, stability: 14, difficulty: 5, fsrs_state: 2 },
    ]);
    repos.reviews.importReview(qid, '2026-03-01T00:00:00+00:00', 'right', 'x', 'notes');
    repos.reviews.importReview(qid, '2026-02-01T00:00:00+00:00', 'wrong');
    expect(repos.reviews.listReviewsForDeck(d)).toEqual([
      { prompt: 'p', ts: '2026-02-01T00:00:00+00:00', result: 'wrong', user_answer: '', grader_notes: '' },
      { prompt: 'p', ts: '2026-03-01T00:00:00+00:00', result: 'right', user_answer: 'x', grader_notes: 'notes' },
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
    clock.set(at(PARITY_NOW, H));
    expect(repos.cards.countDue()).toBe(2);
    expect(repos.cards.nextDueMinutes()).toBeNull();
    expect(repos.cards.dueQuestions(d, 5).map((x) => x.id).sort()).toEqual([a, b]);
    expect(repos.cards.dueQuestions(d, 1)).toHaveLength(1);
  });

  it('srsState reads the card row', () => {
    const { repos, storage } = cell();
    const d = repos.decks.create('d');
    const qid = repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
    expect(repos.cards.srsState(qid)).toEqual({ question_id: qid, step: 0, next_due: '2026-03-14T15:00:00+00:00', last_review: null, stability: null, difficulty: null, fsrs_state: 1 });
    expect(repos.cards.srsState(999)).toBeNull();
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
