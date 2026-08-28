import { describe, expect, it } from 'vitest';
import { SeededRandom } from '../../runtime/adapters/random.js';
import { shuffle } from '../../runtime/adapters/sql/triviaRepo.js';
import { cell, H, TEST_NOW, at } from './setup.js';

function triviaDeck(c: ReturnType<typeof cell>, prompts: string[]) {
  const d = c.repos.decks.createTrivia('t', { topic: 'History', intervalMinutes: 60, displayName: 'T' });
  const ids = prompts.map((p) => {
    const qid = c.repos.questions.add(d, { type: 'short', prompt: p, answer: 'a', answer_regex: 'a', explanation: 'e' });
    c.repos.trivia.appendCard(qid, d);
    return qid;
  });
  return { d, ids };
}

describe('TriviaRepo: queue', () => {
  it('appends at max+1 per deck and rotates answered cards to the back', () => {
    const c = cell();
    const { d, ids } = triviaDeck(c, ['a', 'b', 'c']);
    expect(c.repos.trivia.listQueueForDeck(d).map((e) => [e.question_id, e.queue_position, e.last_answered_correctly])).toEqual([
      [ids[0], 1, null],
      [ids[1], 2, null],
      [ids[2], 3, null],
    ]);
    c.repos.decks.recordNotificationFire(d, '2026-03-14T14:00:00+00:00', 3);
    c.repos.trivia.markAnswered(ids[0]!, true);
    expect(c.storage.rows('trivia_queue').find((r) => r['question_id'] === ids[0])).toEqual({
      question_id: ids[0],
      queue_position: 4,
      last_answered_at: '2026-03-14T15:00:00+00:00',
      last_answered_correctly: 1,
    });
    expect(c.repos.decks.listTriviaDecks()[0]?.notification_ignored_streak).toBe(0);
    c.repos.trivia.markAnswered(999, true);
    expect(c.repos.trivia.countUnanswered(d)).toBe(2);
    expect(c.repos.trivia.countPendingReview(d)).toBe(2);
    expect(c.repos.trivia.deckStats(d)).toEqual({ total: 3, unanswered: 2, wrong: 0, mastered: 1 });
    c.repos.trivia.setLastCorrectness(ids[0]!, false);
    expect(c.repos.trivia.deckStats(d)).toEqual({ total: 3, unanswered: 2, wrong: 1, mastered: 0 });
    expect(c.storage.rows('trivia_queue').find((r) => r['question_id'] === ids[0])?.['queue_position']).toBe(4);
    expect(c.repos.trivia.hasAnswerSince(d, '2026-03-14T14:59:00+00:00')).toBe(true);
    expect(c.repos.trivia.hasAnswerSince(d, '2026-03-14T15:00:00+00:00')).toBe(false);
    expect(c.repos.trivia.hasAnswerSince(d, null)).toBe(false);
  });

  it('picks wrong before fresh before right', () => {
    const c = cell();
    const { d, ids } = triviaDeck(c, ['right', 'fresh', 'wrong']);
    c.repos.trivia.markAnswered(ids[0]!, true);
    c.repos.trivia.markAnswered(ids[2]!, false);
    expect(c.repos.trivia.pickNextForDeck(d)).toEqual({ question_id: ids[2], deck_id: d, prompt: 'wrong', is_fresh: false });
    c.repos.trivia.setLastCorrectness(ids[2]!, true);
    expect(c.repos.trivia.pickNextForDeck(d)?.question_id).toBe(ids[1]);
    expect(c.repos.trivia.pickNextForDeck(999)).toBeNull();
  });

  it('a session pick mixes review and fresh, backfills, and never exceeds the target', () => {
    const c = cell();
    const { d, ids } = triviaDeck(c, ['r1', 'f1', 'f2', 'w1']);
    c.repos.trivia.markAnswered(ids[0]!, true);
    c.repos.trivia.markAnswered(ids[3]!, false);
    const picked = c.repos.trivia.pickSessionForDeck(d);
    expect(picked).toHaveLength(3);
    const byId = new Map(picked.map((p) => [p.question_id, p]));
    expect(byId.has(ids[3]!)).toBe(true);
    expect(picked.filter((p) => p.is_fresh)).toHaveLength(1);
    expect(c.repos.trivia.pickSessionForDeck(d, { targetSize: 10 })).toHaveLength(4);
    const fresh = cell();
    const only = triviaDeck(fresh, ['f1', 'f2', 'f3', 'f4']);
    expect(fresh.repos.trivia.pickSessionForDeck(only.d).every((p) => p.is_fresh)).toBe(true);
    expect(fresh.repos.trivia.pickSessionForDeck(999)).toEqual([]);
  });

  it('shuffle is a Fisher-Yates over the port', () => {
    expect(shuffle([1, 2, 3, 4, 5], new SeededRandom(1)).sort()).toEqual([1, 2, 3, 4, 5]);
    expect(shuffle([], new SeededRandom(1))).toEqual([]);
  });

  it('prompt lookups', () => {
    const c = cell();
    const { d, ids } = triviaDeck(c, ['a', 'b']);
    expect(c.repos.trivia.promptForQuestion(ids[1]!)).toBe('b');
    expect(c.repos.trivia.promptForQuestion(999)).toBeNull();
    expect(c.repos.trivia.existingPrompts(d)).toEqual(['a', 'b']);
    const extra = c.repos.questions.add(d, { type: 'short', prompt: 'c', answer: 'a' });
    c.repos.trivia.importEntry(extra, 9, { lastAnsweredAt: '2026-03-01T00:00:00+00:00', lastAnsweredCorrectly: 0 });
    expect(c.repos.trivia.listQueueForDeck(d).at(-1)).toMatchObject({ question_id: extra, queue_position: 9, last_answered_correctly: false });
  });
});

describe('TriviaRepo: sessions', () => {
  it('starts, resumes, persists, completes and replaces one active session per deck', async () => {
    const c = cell();
    const { d, ids } = triviaDeck(c, ['a', 'b', 'c']);
    const s = await c.repos.trivia.startOrResume(d, { queue: [ids[0]!, ids[1]!], done: [] });
    expect(s).toEqual({ id: '06d904444c991b8d', deck_id: d, started_at: '2026-03-14T15:00:00+00:00', last_active: '2026-03-14T15:00:00+00:00', status: 'active', queue: [ids[0], ids[1]], done: [] });
    c.clock.set(at(TEST_NOW, H));
    const resumed = await c.repos.trivia.startOrResume(d, { queue: [ids[2]!], done: [[ids[0]!, 'r']] });
    expect(resumed).toMatchObject({ id: s.id, queue: [ids[0], ids[1]], last_active: '2026-03-14T16:00:00+00:00' });
    c.repos.trivia.persistState(d, { queue: [ids[1]!], done: [[ids[0]!, 'w']] });
    expect(c.repos.trivia.getActiveSessionForDeck(d)).toMatchObject({ queue: [ids[1]], done: [[ids[0], 'w']] });
    expect(c.storage.rows('trivia_sessions')[0]).toMatchObject({ queue: String(ids[1]), done: `${ids[0]}w` });
    const active = c.repos.trivia.listActiveSessions();
    expect(active).toEqual([{ deck_name: 't', deck_display_name: 'T', deck_id: d, last_active: '2026-03-14T16:00:00+00:00', queue: [ids[1]], done: [[ids[0], 'w']], snoozed_until: null }]);
    expect(c.repos.trivia.snoozeActiveForDeck(d, '2026-03-14T20:00:00+00:00')).toBe(1);
    expect(c.repos.trivia.listActiveSessions()).toEqual([]);
    expect(c.repos.trivia.listSnoozedSessions()[0]?.snoozed_until).toBe('2026-03-14T20:00:00+00:00');
    c.repos.trivia.snoozeActiveForDeck(d, null);
    const replaced = await c.repos.trivia.replaceActive(d, { queue: [ids[2]!] });
    expect(replaced.id).not.toBe(s.id);
    expect(c.storage.rows('trivia_sessions').map((r) => r['status'])).toEqual(['abandoned', 'active']);
    c.repos.trivia.completeSession(d);
    expect(c.repos.trivia.getActiveSessionForDeck(d)).toBeNull();
    expect(c.repos.trivia.abandonAllSessionsForDeck(d)).toBe(0);
  });

  it('ages idle sessions out on listActive', async () => {
    const c = cell();
    const { d } = triviaDeck(c, ['a']);
    await c.repos.trivia.startOrResume(d, { queue: [], done: [] });
    c.clock.set(at(TEST_NOW, 8 * 24 * H));
    expect(c.repos.trivia.listActiveSessions()).toEqual([]);
    expect(c.storage.rows('trivia_sessions')[0]?.['status']).toBe('abandoned');
  });
});
