import { describe, expect, it } from 'vitest';
import { seedSequences } from '../../runtime/adapters/sql/migrate.js';
import { TARGET_COLUMNS } from '../../domain/merge.js';
import { DATA_TABLES } from '../../runtime/adapters/sql/schema.js';
import { cell, TEST_NOW } from './setup.js';

function populated() {
  const c = cell();
  const d = c.repos.decks.create('d', { displayName: 'D' });
  const q = c.repos.questions.add(d, { type: 'short', prompt: 'p', answer: 'a' });
  c.repos.reviews.importReview(q, '2026-03-01T00:00:00+00:00', 'right');
  c.repos.tokens.insert('h', 'prep_pat_x…yyyy', null);
  c.repos.byok.store('openai-api', 'ct', 'sk-…');
  c.repos.pushSubs.upsert('e', 'p', 'a');
  c.repos.notify.append({ title: 't', body: 'b', url: '/u', source: 's' });
  c.repos.jobs.register({ workflowId: 'w', workflowType: 'plan', deckId: d, deckName: 'd', urlPath: '/w' });
  c.repos.idempotency.recordSync('c1', 'card', 'created', q);
  return { c, d, q };
}

describe('ExportRepo', () => {
  it('dumps the profile as the user dict and every data table', () => {
    const { c, q } = populated();
    const snap = c.repos.export.dump();
    expect(snap.profile).toMatchObject({ tailscale_login: 'seed@example.com', display_name: 'Seed' });
    expect(Object.keys(snap.tables).sort()).toEqual([...DATA_TABLES].sort());
    expect(snap.tables['questions']).toEqual([expect.objectContaining({ id: q, prompt: 'p' })]);
    expect(snap.tables['cards']).toEqual([expect.objectContaining({ question_id: q })]);
    expect(snap.tables['reviews']).toHaveLength(1);
    expect(snap.tables['api_tokens']).toHaveLength(1);
  });

  it('projects only the columns the merge policy reads of a target', () => {
    const { c, d } = populated();
    const snap = c.repos.export.project(TARGET_COLUMNS);
    expect(snap.profile).toMatchObject({ tailscale_login: 'seed@example.com' });
    expect(Object.keys(snap.tables).sort()).toEqual(['decks', 'offline_sync_idempotency']);
    expect(snap.tables['decks']).toEqual([{ id: d, name: 'd' }]);
    expect(snap.tables['offline_sync_idempotency']).toEqual([{ client_id: 'c1' }]);
  });

  it('imports rows the target lacks, idempotent by primary key, dropping user columns', () => {
    const { c } = populated();
    const snap = c.repos.export.dump();
    const target = cell();
    seedSequences(target.storage.sql, 1);
    target.repos.decks.create('other');
    const withUserColumns = {
      profile: snap.profile,
      tables: Object.fromEntries(Object.entries(snap.tables).map(([t, rows]) => [t, rows.map((r) => ({ ...r, user_id: 'anon:x', user_login: 'anon:x' }))])),
    };
    const counts = target.repos.export.importRows(withUserColumns, { idempotentBy: 'id', conflict: 'ignore' });
    expect(counts).toEqual({ decks: 1, questions: 1, cards: 1, reviews: 1, offline_sync_idempotency: 1, notifications_log: 1, push_subscriptions: 1, byok_credentials: 1, api_tokens: 1, active_workflows: 1 });
    expect(target.repos.export.importRows(withUserColumns, { idempotentBy: 'id', conflict: 'ignore' })).toEqual({});
    expect(target.repos.decks.listSummaries().map((d) => d.name).sort()).toEqual(['d', 'other']);
    expect(target.storage.rows('questions')).toEqual(snap.tables['questions']);
    expect(target.repos.export.dump().profile?.tailscale_login).toBe('seed@example.com');
  });

  it("under 'ignore', a row whose primary key the target holds is skipped, never rewritten", () => {
    // The merge's rule: the two cells mint from disjoint id blocks, so a
    // collision is a bug and the target's row is the one to keep.
    const { c } = populated();
    const target = cell();
    target.repos.decks.create('mine', { displayName: 'Mine' });
    const counts = target.repos.export.importRows(c.repos.export.dump(), { idempotentBy: 'id', conflict: 'ignore' });
    expect(counts['decks']).toBeUndefined();
    expect(target.repos.decks.findName(1)).toBe('mine');
  });

  it("under 'update', a row that differs is rewritten and one that does not costs nothing", () => {
    // The migration's rule. `cards` is only ever rewritten, so an import
    // that could only insert would carry a studying user's pre-window
    // schedule forward with no re-run able to repair it.
    const { c } = populated();
    const target = cell();
    const first = c.repos.export.dump();
    const update = { idempotentBy: 'id', conflict: 'update' } as const;
    expect(target.repos.export.importRows(first, update)['cards']).toBe(1);
    expect(target.repos.export.importRows(first, update)['cards']).toBeUndefined();

    const card = first.tables['cards']![0]!;
    const moved = { ...card, step: 8, next_due: '2026-12-01T00:00:00+00:00', stability: 42.5, difficulty: 6.25, fsrs_state: 2 };
    const counts = target.repos.export.importRows({ profile: null, tables: { cards: [moved] } }, update);
    expect(counts).toEqual({ cards: 1 });
    expect(target.storage.rows('cards')).toEqual([moved]);

  });

  it("under 'update', a composite key is matched on the whole key", () => {
    const { c, d, q } = populated();
    const target = cell();
    target.repos.export.importRows(c.repos.export.dump(), { idempotentBy: 'id', conflict: 'update' });
    const at = '2026-03-14T15:00:00+00:00';
    const session = { id: 's1', deck_id: d, created_at: at, last_active: at, status: 'active', state: 'awaiting-answer', version: 1 };
    const answer = { session_id: 's1', question_id: q, answered_at: at, result: 'wrong', workflow_id: null };
    const update = { idempotentBy: 'id', conflict: 'update' } as const;
    target.repos.export.importRows({ profile: null, tables: { study_sessions: [session], study_session_answers: [answer] } }, update);

    const changed = { ...answer, result: 'right', workflow_id: 'wf-2' };
    expect(target.repos.export.importRows({ profile: null, tables: { study_session_answers: [changed] } }, update)).toEqual({ study_session_answers: 1 });
    expect(target.storage.rows('study_session_answers')).toEqual([changed]);
    expect(target.repos.export.importRows({ profile: null, tables: { study_session_answers: [changed] } }, update)).toEqual({});
  });

  it('wipe empties every data table and keeps the profile and the id counters', () => {
    const { c } = populated();
    c.repos.export.wipe();
    for (const t of DATA_TABLES) expect(c.storage.rows(t), t).toEqual([]);
    expect(c.repos.prefs.get()?.tailscale_login).toBe('seed@example.com');
    expect(c.repos.decks.create('again')).toBe(2);
  });
});

describe('TombstoneRepo', () => {
  it('writes once after a wipe, reads back, and stamps the scrub', async () => {
    const { c } = populated();
    expect(c.repos.tombstone.get()).toBeNull();
    await c.storage.deleteAll();
    c.repos.tombstone.write('merged', '2026-03-14T15:00:00+00:00', 4096);
    c.repos.tombstone.write('reaped', '2026-03-15T15:00:00+00:00', 1);
    expect(c.repos.tombstone.get()).toEqual({ reason: 'merged', at: '2026-03-14T15:00:00+00:00', scrubbed_at: null, former_bytes: 4096 });
    c.repos.tombstone.stampScrubbed('2026-03-14T15:00:01+00:00');
    c.repos.tombstone.stampScrubbed('2026-03-14T15:00:02+00:00');
    expect(c.repos.tombstone.get()?.scrubbed_at).toBe('2026-03-14T15:00:01+00:00');
    expect(TEST_NOW.toISOString()).toBe('2026-03-14T15:00:00.000Z');
  });
});
