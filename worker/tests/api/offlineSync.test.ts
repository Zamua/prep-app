// Sync semantics the corpus does not reach: the order reviews replay in,
// a client_id repeated inside one batch, and the two parse-level caps.
import { describe, expect, it } from 'vitest';
import { MAX_SYNC_CARDS, MAX_SYNC_REVIEWS, parseBatch, syncBatch } from '../../app/offline/sync.js';
import { RequestValidationError } from '../../app/validation.js';
import { isoUtc } from '../../domain/py.js';
import { cell } from '../repos/setup.js';

const at = (minutes: number) => isoUtc(new Date(Date.UTC(2026, 2, 14, 15, 0, 0) + minutes * 60_000));

function deck(): { deps: ReturnType<typeof cell>; qid: number } {
  const c = cell();
  const id = c.repos.decks.create('capitals');
  return { deps: c, qid: c.repos.questions.add(id, { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima' }) };
}

describe('the batch caps', () => {
  it('reports the cap as pydantic does, with the length it saw', () => {
    const items = Array.from({ length: MAX_SYNC_CARDS + 1 }, (_, i) => ({ client_id: `c${i}`, prompt: 'p', answer: 'a' }));
    try {
      parseBatch({ new_cards: items, reviews: [] });
      expect.unreachable();
    } catch (e) {
      const [error] = (e as RequestValidationError).errors;
      expect(error).toMatchObject({
        type: 'too_long',
        loc: ['body', 'new_cards'],
        msg: `List should have at most ${MAX_SYNC_CARDS} items after validation, not ${MAX_SYNC_CARDS + 1}`,
        ctx: { actual_length: MAX_SYNC_CARDS + 1, field_type: 'List', max_length: MAX_SYNC_CARDS },
      });
    }
  });

  it('names the review cap separately', () => {
    const items = Array.from({ length: MAX_SYNC_REVIEWS + 1 }, (_, i) => ({ client_id: `r${i}` }));
    expect(() => parseBatch({ reviews: items })).toThrow(RequestValidationError);
  });

  it('reports a non-object item at its index', () => {
    try {
      parseBatch({ new_cards: ['not an object'], reviews: [1] });
      expect.unreachable();
    } catch (e) {
      expect((e as RequestValidationError).errors.map((x) => x.loc)).toEqual([
        ['body', 'new_cards', 0],
        ['body', 'reviews', 0],
      ]);
    }
  });

  it('treats a missing list as empty', () => {
    expect(parseBatch({})).toEqual({ new_cards: [], reviews: [] });
  });
});

describe('review ordering', () => {
  it('replays in reviewed_at order across the batch, whatever order they arrived in', () => {
    const { deps, qid } = deck();
    const result = syncBatch(deps, {
      reviews: [
        { client_id: 'late', question_id: qid, verdict: 'right', graded_by: 'auto', reviewed_at: at(-5) },
        { client_id: 'early', question_id: qid, verdict: 'wrong', graded_by: 'auto', reviewed_at: at(-60) },
      ],
    });
    // Results come back in request order, but the rows were written oldest
    // first, so the later review is the one that owns the card state.
    expect(result.reviews).toEqual([
      { client_id: 'late', status: 'applied' },
      { client_id: 'early', status: 'applied' },
    ]);
    expect(deps.storage.rows('reviews').map((r) => r['ts'])).toEqual([at(-60), at(-5)]);
    expect(deps.repos.cards.srsState(qid)!.last_review).toBe(at(-5));
  });

  it('logs without rescheduling when a later review already owns the card', () => {
    const { deps, qid } = deck();
    syncBatch(deps, { reviews: [{ client_id: 'newer', question_id: qid, verdict: 'right', graded_by: 'auto', reviewed_at: at(-5) }] });
    const result = syncBatch(deps, { reviews: [{ client_id: 'older', question_id: qid, verdict: 'wrong', graded_by: 'auto', reviewed_at: at(-60) }] });
    expect(result.reviews).toEqual([{ client_id: 'older', status: 'logged_no_reschedule' }]);
    expect(deps.repos.cards.srsState(qid)!.last_review).toBe(at(-5));
  });

  it('replays the first outcome for a client_id repeated inside one batch', () => {
    const { deps, qid } = deck();
    const item = { client_id: 'same', question_id: qid, verdict: 'right', graded_by: 'auto', reviewed_at: at(-5) };
    const result = syncBatch(deps, { reviews: [item, { ...item }] });
    expect(result.reviews).toEqual([
      { client_id: 'same', status: 'applied' },
      { client_id: 'same', status: 'applied' },
    ]);
    expect(deps.repos.reviews.listReviewsForDeck(deps.repos.decks.findId('capitals')!)).toHaveLength(1);
  });

  it('clamps a future timestamp to server-now and says so in the trail', () => {
    const { deps, qid } = deck();
    syncBatch(deps, { reviews: [{ client_id: 'future', question_id: qid, verdict: 'right', graded_by: 'self', reviewed_at: '2099-01-01T00:00:00+00:00' }] });
    const [row] = deps.repos.reviews.listReviewsForDeck(deps.repos.decks.findId('capitals')!);
    expect(row!.ts).toBe('2026-03-14T15:00:00+00:00');
    expect(row!.grader_notes).toBe('(offline self-graded) (client reviewed_at 2099-01-01T00:00:00+00:00 clamped to server now)');
  });
});

describe('per-item rejection', () => {
  it('rejects an over-long client id without touching the batch', () => {
    const { deps, qid } = deck();
    const result = syncBatch(deps, {
      reviews: [
        { client_id: 'x'.repeat(65), question_id: qid, verdict: 'right', graded_by: 'auto', reviewed_at: at(-5) },
        { client_id: 'ok', question_id: qid, verdict: 'right', graded_by: 'auto', reviewed_at: at(-5) },
      ],
    });
    expect(result.reviews[0]).toEqual({ client_id: 'x'.repeat(65), status: 'rejected', error: 'client_id too long' });
    expect(result.reviews[1]).toEqual({ client_id: 'ok', status: 'applied' });
  });

  it('files a deck-less card by its label, and into the inbox when the label is unusable', () => {
    const { deps } = deck();
    const result = syncBatch(deps, {
      new_cards: [
        { client_id: 'a', deck_name: 'Offline Notes', prompt: 'p1', answer: 'a1' },
        { client_id: 'b', deck_name: 'x'.repeat(81), prompt: 'p2', answer: 'a2' },
        { client_id: 'c', deck_name: 'has\nnewline', prompt: 'p3', answer: 'a3' },
      ],
    });
    expect(result.cards.map((r) => r.status)).toEqual(['created', 'created', 'created']);
    expect(deps.repos.decks.findId('offline-notes')).not.toBeNull();
    expect(deps.repos.questions.promptsInDeck(deps.repos.decks.findId('inbox')!)).toEqual(['p2', 'p3']);
  });

  it('never trusts the client regex: an unusable pattern stores null', () => {
    const { deps } = deck();
    const result = syncBatch(deps, { new_cards: [{ client_id: 'a', prompt: 'p', answer: 'Santiago', answer_regex: '(' }] });
    expect(result.cards[0]!.status).toBe('created');
    expect(deps.repos.questions.get(result.cards[0]!.question_id!)!.answer_regex).toBeNull();
  });
});
