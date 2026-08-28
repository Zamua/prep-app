import { describe, expect, it } from 'vitest';
import { SessionNotFound, StaleVersionError } from '../../app/ports.js';
import { cell, D, H, M, PARITY_NOW, at } from './setup.js';

const short = (prompt: string, extra: Record<string, unknown> = {}) => ({ type: 'short' as const, prompt, answer: 'a', ...extra });

function deckWithDue(c: ReturnType<typeof cell>, dues: string[]) {
  const d = c.repos.decks.create('d', { displayName: 'Deck' });
  const ids = dues.map((due, i) => {
    const qid = c.repos.questions.add(d, short(`q${i}`, i === 0 ? { skeleton: 'def f():\n', language: 'python', type: 'code' } : {}));
    c.repos.cards.restoreCardState(qid, { next_due: due });
    return qid;
  });
  return { d, ids };
}

describe('SessionRepo', () => {
  it('creates a session on the most overdue card and seeds the draft from its skeleton', async () => {
    const c = cell();
    const { d, ids } = deckWithDue(c, ['2026-03-14T12:00:00+00:00', '2026-03-14T13:00:00+00:00', '2026-03-20T00:00:00+00:00']);
    const sid = await c.repos.sessions.create(d, 'iPhone');
    expect(sid).toBe('81426e386f04220d');
    const s = c.repos.sessions.get(sid);
    expect(s).toMatchObject({
      id: sid,
      deck_id: d,
      status: 'active',
      state: 'awaiting-answer',
      current_question_id: ids[0],
      current_draft: 'def f():\n',
      version: 1,
      device_label: 'iPhone',
      created_at: '2026-03-14T15:00:00+00:00',
    });
    expect(c.repos.sessions.findActiveForDeck(d)?.id).toBe(sid);
    expect(c.repos.sessions.get('nope')).toBeNull();
  });

  it('ties within the same hour are shuffled, earlier hours win', async () => {
    const c = cell();
    const { d, ids } = deckWithDue(c, ['2026-03-14T13:30:00+00:00', '2026-03-14T13:10:00+00:00', '2026-03-14T11:59:00+00:00']);
    const sid = await c.repos.sessions.create(d, 'x');
    expect(c.repos.sessions.get(sid)?.current_question_id).toBe(ids[2]);
  });

  it('version-checks the mutations and advances through the due cards to completion', async () => {
    const c = cell();
    const { d, ids } = deckWithDue(c, ['2026-03-14T12:00:00+00:00', '2026-03-14T13:00:00+00:00', '2026-03-20T00:00:00+00:00']);
    const sid = await c.repos.sessions.create(d, 'x');
    expect(() => c.repos.sessions.updateDraft(sid, 'x', 7)).toThrow(StaleVersionError);
    expect(() => c.repos.sessions.updateDraft('nope', 'x', 1)).toThrow(SessionNotFound);
    expect(c.repos.sessions.updateDraft(sid, 'draft', 1)).toBe(2);
    expect(c.repos.sessions.get(sid)?.current_draft).toBe('draft');
    const verdict = { result: 'right', feedback: 'ok' };
    const state = { step: 1, next_due: '2026-03-14T15:10:00+00:00', interval_minutes: 10 };
    expect(c.repos.sessions.recordAnswerSync(sid, ids[0]!, 2, 'a', verdict, state)).toBe(3);
    const answered = c.repos.sessions.get(sid)!;
    expect(answered).toMatchObject({ state: 'showing-result', current_draft: null, last_answered_qid: ids[0], last_answered_verdict: verdict, last_answered_state: state, version: 3 });
    expect(c.storage.rows('study_sessions')[0]?.['last_answered_verdict']).toBe('{"result":"right","feedback":"ok"}');
    expect(c.storage.rows('study_session_answers')).toEqual([{ session_id: sid, question_id: ids[0], answered_at: '2026-03-14T15:00:00+00:00', result: 'right', workflow_id: null }]);
    expect(c.repos.sessions.advance(sid, 3)).toBe(4);
    expect(c.repos.sessions.get(sid)).toMatchObject({ state: 'awaiting-answer', current_question_id: ids[1], last_answered_qid: null, last_answered_verdict: null });
    c.repos.sessions.recordAnswerSync(sid, ids[1]!, 4, 'a', verdict, state);
    expect(c.repos.sessions.advance(sid, 5)).toBe(6);
    expect(c.repos.sessions.get(sid)).toMatchObject({ status: 'completed', current_question_id: null, version: 6 });
  });

  it('grading round trip: set, complete once, abandon by workflow id', async () => {
    const c = cell();
    const { d, ids } = deckWithDue(c, ['2026-03-14T12:00:00+00:00']);
    const sid = await c.repos.sessions.create(d, 'x');
    expect(c.repos.sessions.setGrading(sid, ids[0]!, 'wf-1', 1)).toBe(2);
    expect(c.repos.sessions.get(sid)).toMatchObject({ state: 'grading', current_grading_workflow_id: 'wf-1' });
    c.repos.sessions.gradingAbandoned(sid, 'wf-other');
    expect(c.repos.sessions.get(sid)?.state).toBe('grading');
    c.repos.sessions.gradingCompleted(sid, ids[0]!, { result: 'wrong' }, { step: 0 }, 'wf-1');
    c.repos.sessions.gradingCompleted(sid, ids[0]!, { result: 'right' }, { step: 9 }, 'wf-1');
    expect(c.repos.sessions.get(sid)).toMatchObject({ state: 'showing-result', current_grading_workflow_id: null, last_answered_verdict: { result: 'wrong' }, version: 3 });
    expect(c.storage.rows('study_session_answers')[0]).toMatchObject({ workflow_id: 'wf-1', result: 'wrong' });
    c.repos.sessions.setGrading(sid, ids[0]!, 'wf-2', 3);
    c.repos.sessions.gradingAbandoned(sid, 'wf-2');
    expect(c.repos.sessions.get(sid)).toMatchObject({ state: 'awaiting-answer', current_grading_workflow_id: null, version: 5 });
  });

  it('lists recent sessions with deck and prompt, ages idle ones out, hides snoozed ones', async () => {
    const c = cell();
    const { d, ids } = deckWithDue(c, ['2026-03-14T12:00:00+00:00']);
    const old = await c.repos.sessions.create(d, 'old');
    c.repos.pins.session(old, '2026-03-01T00:00:00+00:00');
    const fresh = await c.repos.sessions.create(d, 'fresh');
    const snoozed = await c.repos.sessions.create(d, 'snoozed');
    c.repos.sessions.snooze(snoozed, '2026-03-14T18:00:00+00:00');
    const recent = c.repos.sessions.listRecent();
    expect(recent.map((r) => r.id)).toEqual([fresh]);
    expect(recent[0]).toMatchObject({ deck_name: 'd', deck_display_name: 'Deck', current_question_id: ids[0], current_prompt: 'q0', current_type: 'code', device_label: 'fresh', snoozed_until: null });
    expect(c.repos.sessions.get(old)?.status).toBe('abandoned');
    expect(c.repos.sessions.listSnoozed().map((r) => [r.id, r.snoozed_until])).toEqual([[snoozed, '2026-03-14T18:00:00+00:00']]);
    c.clock.set(at(PARITY_NOW, 4 * H));
    expect(c.repos.sessions.listRecent().map((r) => r.id)).toEqual([snoozed, fresh]);
    c.repos.sessions.snooze(fresh, null);
    expect(c.repos.sessions.listRecent(1)).toHaveLength(1);
  });

  it('abandon, markCompleted and abandonAllForDeck bump the version', async () => {
    const c = cell();
    const { d } = deckWithDue(c, ['2026-03-14T12:00:00+00:00']);
    const a = await c.repos.sessions.create(d, 'x');
    const b = await c.repos.sessions.create(d, 'y');
    c.repos.sessions.abandon(a);
    expect(c.repos.sessions.get(a)).toMatchObject({ status: 'abandoned', version: 2 });
    c.repos.sessions.markCompleted(b);
    expect(c.repos.sessions.get(b)).toMatchObject({ status: 'completed', version: 2 });
    const e = await c.repos.sessions.create(d, 'z');
    c.clock.set(at(PARITY_NOW, D + M));
    expect(c.repos.sessions.abandonAllForDeck(d)).toBe(1);
    expect(c.repos.sessions.get(e)).toMatchObject({ status: 'abandoned', last_active: '2026-03-15T15:01:00+00:00' });
  });
});
