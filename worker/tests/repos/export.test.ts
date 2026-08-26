import { describe, expect, it } from 'vitest';
import { seedSequences } from '../../runtime/adapters/sql/migrate.js';
import { TARGET_COLUMNS } from '../../domain/merge.js';
import { DATA_TABLES } from '../../runtime/adapters/sql/schema.js';
import { cell, PARITY_NOW } from './setup.js';

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
    expect(snap.profile).toMatchObject({ tailscale_login: 'parity@example.com', display_name: 'Parity' });
    expect(Object.keys(snap.tables).sort()).toEqual([...DATA_TABLES].sort());
    expect(snap.tables['questions']).toEqual([expect.objectContaining({ id: q, prompt: 'p' })]);
    expect(snap.tables['cards']).toEqual([expect.objectContaining({ question_id: q })]);
    expect(snap.tables['reviews']).toHaveLength(1);
    expect(snap.tables['api_tokens']).toHaveLength(1);
  });

  it('projects only the columns the merge policy reads of a target', () => {
    const { c, d } = populated();
    const snap = c.repos.export.project(TARGET_COLUMNS);
    expect(snap.profile).toMatchObject({ tailscale_login: 'parity@example.com' });
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
    const counts = target.repos.export.importRows(withUserColumns, { idempotentBy: 'id' });
    expect(counts).toEqual({ decks: 1, questions: 1, cards: 1, reviews: 1, offline_sync_idempotency: 1, notifications_log: 1, push_subscriptions: 1, byok_credentials: 1, api_tokens: 1, active_workflows: 1 });
    expect(target.repos.export.importRows(withUserColumns, { idempotentBy: 'id' })).toEqual({});
    expect(target.repos.decks.listSummaries().map((d) => d.name).sort()).toEqual(['d', 'other']);
    expect(target.storage.rows('questions')).toEqual(snap.tables['questions']);
    expect(target.repos.export.dump().profile?.tailscale_login).toBe('parity@example.com');
  });

  it('a row whose primary key the target already holds is skipped, never rewritten', () => {
    const { c } = populated();
    const target = cell();
    target.repos.decks.create('mine', { displayName: 'Mine' });
    const counts = target.repos.export.importRows(c.repos.export.dump(), { idempotentBy: 'id' });
    expect(counts['decks']).toBeUndefined();
    expect(target.repos.decks.findName(1)).toBe('mine');
  });

  it('wipe empties every data table and keeps the profile and the id counters', () => {
    const { c } = populated();
    c.repos.export.wipe();
    for (const t of DATA_TABLES) expect(c.storage.rows(t), t).toEqual([]);
    expect(c.repos.prefs.get()?.tailscale_login).toBe('parity@example.com');
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
    expect(PARITY_NOW.toISOString()).toBe('2026-03-14T15:00:00.000Z');
  });
});
