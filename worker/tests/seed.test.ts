import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { composeWith } from '../runtime/compose.js';
import { UserCell } from '../runtime/cells/UserCell.js';
import { fakeCellState } from './fakes/sqlStorage.js';
import { CORPUS, fakeEnv, spyRenderer } from './helpers.js';

const USER = 'parity@example.com';

function seeded(profile: string) {
  const env = fakeEnv();
  composeWith(env, { renderer: spyRenderer() });
  const state = fakeCellState();
  const cell = new UserCell(state, env);
  // The constructor arms the alarm off the migrated rows; `wipe` drops them.
  // The real runtime orders those, so the fake waits before driving the cell.
  const seed = state.ready().then(() => cell.wipe(profile)).then(() => cell.seed(profile, USER, null));
  return { env, state, cell, seed };
}

describe('the parity seed profiles', () => {
  it.each(['reader', 'empty', 'anonymous'])('%s reproduces tests/fixtures/parity/pages/<profile>/seed.json exactly', async (profile) => {
    const { seed } = seeded(profile);
    const golden = readFileSync(join(CORPUS, profile, 'seed.json'), 'utf8');
    expect(JSON.parse(JSON.stringify(await seed))).toEqual(JSON.parse(golden));
  });

  it('reader: the rows the profile pins', async () => {
    const { state, seed } = seeded('reader');
    const ids = (await seed) as { questions: { srs_a: Record<string, number>; trivia: Record<string, number> }; sessions: { active: string; snoozed: string } };
    const cards = Object.fromEntries(state.fake.rows('cards').map((r) => [r['question_id'], r]));
    expect(cards[ids.questions.srs_a['mcq']!]).toMatchObject({ step: 2, next_due: '2026-03-14T12:00:00+00:00', last_review: '2026-03-12T15:00:00+00:00' });
    expect(cards[ids.questions.srs_a['short_plain']!]).toMatchObject({ step: 4, next_due: '2026-03-19T15:00:00+00:00' });
    expect(cards[ids.questions.trivia['rome']!]).toBeUndefined();
    expect(state.fake.rows('questions').find((q) => q['id'] === ids.questions.srs_a['suspended'])?.['suspended']).toBe(1);
    expect(state.fake.rows('questions').find((q) => q['id'] === ids.questions.srs_a['multi'])?.['answer']).toBe('["Ottawa", "Lima"]');
    expect(state.fake.rows('reviews').map((r) => [r['question_id'], r['ts'], r['result'], r['user_answer'], r['grader_notes']])).toEqual([
      [1, '2026-03-12T15:00:00+00:00', 'right', 'Canberra', null],
      [1, '2026-03-08T15:00:00+00:00', 'wrong', 'Sydney', null],
      [2, '2026-03-13T15:00:00+00:00', 'right', 'Nairobi', null],
      [4, '2026-03-09T15:00:00+00:00', 'right', 'table.get(code)', null],
    ]);
    const decks = state.fake.rows('decks');
    expect(decks[1]).toMatchObject({ name: 'distributed-systems', pinned_at: '2026-03-13T15:00:00+00:00' });
    expect(decks[3]).toMatchObject({ name: 'world-history', deck_type: 'trivia', notification_interval_minutes: 1440, context_prompt: 'World history from antiquity to 1900.' });
    expect(state.fake.rows('trivia_queue').find((r) => r['question_id'] === ids.questions.trivia['rome'])).toMatchObject({ queue_position: 4, last_answered_correctly: 1, last_answered_at: '2026-03-14T15:00:00+00:00' });
    const sessions = Object.fromEntries(state.fake.rows('study_sessions').map((r) => [r['id'], r]));
    expect(sessions[ids.sessions.active]).toMatchObject({ deck_id: 1, current_question_id: ids.questions.srs_a['mcq'], last_active: '2026-03-14T14:40:00+00:00', created_at: '2026-03-14T14:35:00+00:00', device_label: 'iPhone' });
    expect(sessions[ids.sessions.snoozed]).toMatchObject({ deck_id: 2, current_question_id: 7, snoozed_until: '2026-03-14T18:00:00+00:00' });
    expect(state.fake.rows('notifications_log').map((n) => n['sent_at'])).toEqual(['2026-03-14T12:00:00+00:00', '2026-03-13T15:00:00+00:00']);
    expect(state.fake.rows('api_tokens')[0]).toMatchObject({ key_prefix: 'prep_pat_Pa…0000', label: 'Parity CLI', created_at: '2026-03-11T15:00:00+00:00', token_hash: expect.stringMatching(/^[0-9a-f]{64}$/) });
    expect(state.fake.rows('active_workflows')[0]).toMatchObject({ workflow_id: 'transform-world-capitals-parity01', started_at: '2026-03-14T14:55:00+00:00', status: 'computing', deck_id: 1 });
    expect(state.fake.rows('profile')[0]?.['notification_prefs']).toContain('"tz": "America/New_York"');
  });

  it('study: every card due in its own hour, the warm-up already answered in the session', async () => {
    const { state, seed } = seeded('study');
    const ids = (await seed) as { deck: { id: number; slug: string }; questions: Record<string, number>; session_id: string };
    expect(ids.deck).toEqual({ id: 1, slug: 'geography', display: 'Geography' });
    expect(Object.keys(ids.questions).sort()).toEqual(['code', 'mcq', 'multi', 'short_plain', 'short_regex', 'warmup']);
    expect(ids.session_id).toBe('81426e386f04220d');
    const session = state.fake.rows('study_sessions')[0];
    expect(session).toMatchObject({ current_question_id: ids.questions['mcq'], last_active: '2026-03-14T14:58:00+00:00' });
    expect(state.fake.rows('study_session_answers')).toEqual([{ session_id: ids.session_id, question_id: ids.questions['warmup'], answered_at: '2026-03-14T14:56:00+00:00', result: 'right', workflow_id: null }]);
    expect(state.fake.rows('cards').find((c) => c['question_id'] === ids.questions['warmup'])).toMatchObject({ step: 1, next_due: '2026-03-15T15:00:00+00:00', last_review: '2026-03-14T14:56:00+00:00' });
  });

  it('workflows: the two SRS decks, the trivia deck and a session on a free-text card', async () => {
    const { state, seed } = seeded('workflows');
    const ids = (await seed) as {
      decks: Record<string, { id: number; slug: string; display: string }>;
      questions: { srs_a: Record<string, number>; srs_b: Record<string, number> };
      session_id: string;
    };
    expect(ids.decks).toEqual({
      srs_a: { id: 1, slug: 'algorithms', display: 'Algorithms' },
      srs_b: { id: 2, slug: 'databases', display: 'Databases' },
      trivia: { id: 3, slug: 'systems-trivia', display: 'Systems Trivia' },
    });
    expect(Object.keys(ids.questions.srs_a)).toEqual(['complexity', 'traversal', 'binary_search', 'annotated', 'retired', 'duplicate']);
    expect(Object.keys(ids.questions.srs_b)).toEqual(['acid', 'btree', 'wal']);
    // The transform flows address cards by id, so the order the profile
    // inserts them in is part of the contract.
    expect(Object.values(ids.questions.srs_a)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(Object.values(ids.questions.srs_b)).toEqual([7, 8, 9]);
    const cards = Object.fromEntries(state.fake.rows('cards').map((r) => [r['question_id'], r]));
    expect(cards[ids.questions.srs_a['complexity']!]).toMatchObject({ step: 0, next_due: '2026-03-14T10:00:00+00:00' });
    expect(cards[ids.questions.srs_a['binary_search']!]).toMatchObject({ step: 2, next_due: '2026-03-14T12:00:00+00:00', last_review: '2026-03-11T15:00:00+00:00' });
    const annotated = state.fake.rows('questions').find((q) => q['id'] === ids.questions.srs_a['annotated']);
    expect(annotated).toMatchObject({ answer_regex: '(?i)merge', explanation: 'Merge sort keeps equal keys in input order; heapsort does not.' });
    expect(state.fake.rows('decks')[2]).toMatchObject({ name: 'systems-trivia', deck_type: 'trivia', notification_interval_minutes: 1440 });
    expect(state.fake.rows('study_sessions')[0]).toMatchObject({
      id: ids.session_id,
      deck_id: 1,
      current_question_id: ids.questions.srs_a['complexity'],
      last_active: '2026-03-14T14:58:00+00:00',
      created_at: '2026-03-14T14:54:00+00:00',
    });
    expect(state.fake.rows('active_workflows')).toEqual([]);
  });
});
