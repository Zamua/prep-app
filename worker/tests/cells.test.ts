import { beforeEach, describe, expect, it } from 'vitest';
import type { CellSnapshot } from '../app/entities.js';
import { RowCapReached } from '../domain/limits.js';
import { composeWith, type Composition } from '../runtime/compose.js';
import { JobCell } from '../runtime/cells/JobCell.js';
import { KIND_HEADER, NOW_HEADER, PAT_HASH_HEADER, SUBJECT_HEADER, type Route } from '../runtime/cells/router.js';
import { TOMBSTONED_HEADER, UnknownProfile, UserCell } from '../runtime/cells/UserCell.js';
import * as pages from '../runtime/cells/routes/pages.js';
import type { Env } from '../runtime/env.js';
import { fakeCellState } from './fakes/sqlStorage.js';
import { corpusPage, fakeEnv, fakeState, req, spyRenderer } from './helpers.js';

const USER = 'parity@example.com';
const ANON = 'anon:' + 'ab'.repeat(16);
const AT = '2026-03-14T15:00:00+00:00';
const IDENTIFIED = { [SUBJECT_HEADER]: USER, 'x-prep-display-name': 'Parity' };

let env: Env;
let c: Composition;
let renderer: ReturnType<typeof spyRenderer>;
let state: ReturnType<typeof fakeCellState>;
let cell: UserCell;

beforeEach(() => {
  env = fakeEnv();
  renderer = spyRenderer();
  c = composeWith(env, { renderer });
  state = fakeCellState();
  cell = new UserCell(state, env);
});

const identified = (path: string, init: RequestInit = {}) => req(path, { ...init, headers: { ...IDENTIFIED, ...(init.headers as Record<string, string>) } });

/** A live anonymous account, minted the way the instant path mints one. A
 * cell holding no anonymous profile refuses an anon identity outright, so a
 * gate can only be reached through a real one. */
async function anonymousCell(): Promise<{ cell: UserCell; state: ReturnType<typeof fakeCellState> }> {
  const state = fakeCellState();
  const cell = new UserCell(state, env);
  await cell.createInstantDeck({ displayName: 'Guest deck', cards: [], mint: { id: ANON, displayName: 'Guest', idx: 7 }, at: AT });
  return { cell, state };
}

/** Routes the cell serves for the test's duration. Declaration order
 * decides a match, so a test route is prepended and wins over the real
 * table's entry for the same pattern. */
function withRoutes(routes: Route[], run: () => Promise<void>): Promise<void> {
  const table = pages.pageRoutes as Route[];
  table.unshift(...routes);
  return run().finally(() => table.splice(0, routes.length));
}

describe('UserCell.seed', () => {
  it.each(['reader', 'empty', 'anonymous'])('reproduces the Python seed JSON for %s', async (profile) => {
    const seed = await cell.seed(profile, USER, null);
    expect(seed).toEqual(JSON.parse(JSON.stringify(corpusPage(profile, 'seed'))));
  });

  it('refuses an unknown profile', async () => {
    await expect(cell.seed('nope', USER, null)).rejects.toBeInstanceOf(UnknownProfile);
  });

  it('wipes, re-migrates, pins block 0 and registers idx 0 in the directory', async () => {
    await cell.seed('reader', USER, null);
    await cell.seed('reader', USER, null);
    expect(state.fake.rows('decks').map((d) => d['id'])).toEqual([1, 2, 3, 4]);
    expect(await c.directory.lookup(USER)).toMatchObject({ idx: 0, is_anonymous: false });
    expect(await state.storage.get('parity')).toEqual({ profile: 'reader', flags: [] });
    expect(state.fake.rows('profile')[0]).toMatchObject({ id: USER, display_name: 'Parity', email: USER, id_base: 0 });
    await cell.seed('anonymous', USER, null);
    expect(state.fake.rows('profile')).toEqual([]);
  });

  it('runs on the parity instant the router forwards', async () => {
    const seed = await cell.seed('empty', USER, '2026-03-15T10:00:00Z');
    expect(seed['now']).toBe('2026-03-15T10:00:00+00:00');
    expect(state.fake.rows('profile')[0]?.['created_at']).toBe('2026-03-15T10:00:00+00:00');
  });
});

describe('UserCell.fetch', () => {
  it('replays a recorded page while no route claims the path, and flips the flag it sets', async () => {
    await cell.seed('reader', USER, null);
    // A path the ported tables do not claim still answers from the recording.
    const res = await cell.fetch(identified('/api/dashboard/deck-menus'));
    expect(res.status).toBe(200);
    expect(renderer.calls[0]?.template).toBe('partials/deck_menus.html');
    expect(await state.storage.get('parity')).toEqual({ profile: 'reader', flags: [] });
  });

  it('answers 404 before any identity or seed', async () => {
    expect((await cell.fetch(req('/'))).status).toBe(404);
    expect((await cell.fetch(identified('/nothing-here'))).status).toBe(404);
  });

  it('serves a route through the gate, the last_seen bump and the page context', async () => {
    const route: Route = { method: 'GET', pattern: '/deck/{name}', gate: 'signedIn', handler: ({ params, repos }) => ({ page: 'deck.html', context: { deck_name: params['name'], decks: repos.decks.listSummaries().length } }) };
    await withRoutes([route], async () => {
      await cell.seed('reader', USER, null);
      const res = await cell.fetch(identified('/deck/world-capitals', { headers: { [NOW_HEADER]: '2026-03-14T16:00:00Z' } }));
      expect(res.status).toBe(200);
      expect(res.headers.get('content-type')).toBe('text/html; charset=utf-8');
      const ctx = renderer.calls[0]!.context;
      expect(renderer.calls[0]!.template).toBe('deck.html');
      expect(ctx).toMatchObject({ deck_name: 'world-capitals', decks: 4, notif_unseen_count: 2, agent_available: true, auth_provider: 'tailscale', app_base: 'https://parity.example.test' });
      expect(ctx['deck_display']).toEqual({ 'distributed-systems': 'Distributed Systems', 'world-capitals': 'World Capitals', scratch: 'Scratch', 'world-history': 'World History Trivia' });
      expect((ctx['user'] as { last_seen_at: string }).last_seen_at).toBe('2026-03-14T16:00:00+00:00');
    });
  });

  it('gates: an anonymous identity on a signed-in route, a token on a page, a page on a token route', async () => {
    const routes: Route[] = [
      { method: 'GET', pattern: '/settings', gate: 'signedIn', handler: () => ({ json: { ok: true } }) },
      { method: 'GET', pattern: '/api/v1/decks', gate: 'pat', handler: () => ({ json: [] }) },
    ];
    await withRoutes(routes, async () => {
      await cell.seed('reader', USER, null);
      const guest = await anonymousCell();
      const anon = { [SUBJECT_HEADER]: ANON, [KIND_HEADER]: 'anon' };
      const html = await guest.cell.fetch(req('/settings', { headers: anon }));
      expect(html.status).toBe(303);
      expect(html.headers.get('location')).toBe('/sign-in');
      const json = await guest.cell.fetch(req('/settings', { headers: { ...anon, accept: 'application/json' } }));
      expect(json.status).toBe(403);
      expect(await json.json()).toEqual({ detail: 'sign in required' });
      // The seeded token's own hash, or the credential check refuses before the gate.
      const hash = String(state.fake.rows('api_tokens')[0]!['token_hash']);
      const asToken = { ...IDENTIFIED, [KIND_HEADER]: 'pat', [PAT_HASH_HEADER]: hash };
      const pat = await cell.fetch(req('/settings', { headers: asToken }));
      expect(pat.status).toBe(401);
      expect(await pat.json()).toEqual({ detail: 'not authenticated' });
      expect((await cell.fetch(identified('/api/v1/decks'))).status).toBe(401);
      expect((await cell.fetch(req('/api/v1/decks', { headers: asToken }))).status).toBe(200);
    });
  });

  it('an anonymous identity is touched, never upserted; a provider identity is created and registered', async () => {
    const route: Route = { method: 'GET', pattern: '/x', gate: 'user', handler: () => ({ empty: true }) };
    await withRoutes([route], async () => {
      const guest = await anonymousCell();
      const claims = { [SUBJECT_HEADER]: ANON, [KIND_HEADER]: 'anon', 'x-prep-display-name': 'Parity', [NOW_HEADER]: '2026-03-14T16:00:00Z' };
      expect((await guest.cell.fetch(req('/x', { headers: claims }))).status).toBe(204);
      // Touched, never upserted: the bump lands, the presented claims do not.
      expect(guest.state.fake.rows('profile')).toMatchObject([{ id: ANON, display_name: 'Guest', is_anonymous: 1, last_seen_at: '2026-03-14T16:00:00+00:00' }]);
      expect(await guest.cell.precheck()).toEqual({ exists: true, isAnonymous: true, tombstoned: null });
      expect((await cell.fetch(identified('/x'))).status).toBe(204);
      expect(await cell.precheck()).toEqual({ exists: true, isAnonymous: false, tombstoned: null });
      expect(await c.directory.lookup(USER)).toMatchObject({ idx: 1 });
      expect(state.fake.rows('profile')[0]).toMatchObject({ id: USER, display_name: 'Parity', id_base: 1 });
    });
  });

  it('maps every handler shape and the cap refusal', async () => {
    const routes: Route[] = [
      { method: 'GET', pattern: '/j', gate: 'user', handler: () => ({ json: { a: 1 }, status: 201 }) },
      { method: 'GET', pattern: '/r', gate: 'user', handler: () => ({ redirect: '/deck/x' }) },
      { method: 'GET', pattern: '/t', gate: 'user', handler: () => ({ text: 'plain' }) },
      { method: 'GET', pattern: '/e', gate: 'user', handler: () => ({ empty: true, headers: { 'hx-redirect': '/' } }) },
      { method: 'POST', pattern: '/cap', gate: 'user', handler: () => { throw new RowCapReached('guest account limit reached'); } },
    ];
    await withRoutes(routes, async () => {
      await cell.seed('empty', USER, null);
      const j = await cell.fetch(identified('/j'));
      expect([j.status, await j.json()]).toEqual([201, { a: 1 }]);
      const r = await cell.fetch(identified('/r'));
      expect([r.status, r.headers.get('location')]).toEqual([303, '/deck/x']);
      expect(await (await cell.fetch(identified('/t'))).text()).toBe('plain');
      const e = await cell.fetch(identified('/e'));
      expect([e.status, e.headers.get('hx-redirect')]).toEqual([204, '/']);
      const html = await cell.fetch(identified('/cap', { method: 'POST', headers: { accept: 'text/html' } }));
      expect(html.status).toBe(429);
      expect(renderer.calls.at(-1)?.context.status_code).toBe(429);
      const json = await cell.fetch(identified('/cap', { method: 'POST', headers: { accept: 'application/json' } }));
      expect(await json.json()).toEqual({ error: { code: 'deck_limit', message: 'guest account limit reached' } });
    });
  });
});

describe('UserCell RPC', () => {
  it('upsert with an idx adopts the id block once; dump and import round-trip', async () => {
    const profile = await cell.upsert(USER, { email: USER, displayName: 'P' }, '2026-03-14T15:00:00Z', 3);
    expect(profile).toMatchObject({ tailscale_login: USER, email: USER, last_seen_at: '2026-03-14T15:00:00+00:00' });
    const repos = c.userRepos(state.fake, c.clock);
    expect(repos.decks.create('d')).toBe(3 * 2 ** 32 + 1);
    await cell.upsert(USER, {}, '2026-03-14T16:00:00Z', 9);
    expect(repos.prefs.getIdBase()).toBe(3);
    expect(await cell.lastSeenAt()).toBe('2026-03-14T16:00:00+00:00');
    const snap = await cell.dump();
    expect(snap.tables['decks']).toHaveLength(1);
    const other = new UserCell(fakeCellState(), env);
    expect(await other.importRows(snap)).toEqual({ decks: 1 });
    expect(await other.importRows(snap)).toEqual({});
  });

  it('createInstantDeck mints the profile on the block the router registered', async () => {
    const anon = 'anon:' + 'ab'.repeat(16);
    const r = await cell.createInstantDeck({ displayName: 'Topic', cards: [{ prompt: 'p', answer: 'a', answer_regex: null }], mint: { id: anon, displayName: 'Guest', idx: 5 }, at: '2026-03-14T15:00:00Z' });
    expect(r.deck_id).toBe(5 * 2 ** 32 + 1);
    expect(await cell.precheck()).toEqual({ exists: true, isAnonymous: true, tombstoned: null });
    expect(state.fake.rows('profile')[0]).toMatchObject({ id: anon, is_anonymous: 1, id_base: 5 });
  });

  it('destroy then scrub: the tombstone survives the wipe and every request answers it', async () => {
    await cell.seed('reader', USER, null);
    const before = state.fake.sql.databaseSize;
    await cell.destroy('merged', '2026-03-14T15:00:00+00:00');
    await cell.destroy('reaped', '2026-03-14T15:00:01+00:00');
    expect(state.fake.rows('tombstone')).toEqual([{ reason: 'merged', at: '2026-03-14T15:00:00+00:00', scrubbed_at: null, former_bytes: before }]);
    expect(state.fake.db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").all()).toEqual([{ name: 'tombstone' }]);
    const res = await cell.fetch(identified('/'));
    expect(res.status).toBe(410);
    expect(res.headers.get(TOMBSTONED_HEADER)).toBe('merged');
    expect(await res.json()).toEqual({ tombstoned: 'merged' });
    expect(await cell.precheck()).toEqual({ exists: false, isAnonymous: false, tombstoned: 'merged' });
    await cell.scrub('2026-03-14T15:00:02+00:00');
    await cell.scrub('2026-03-14T15:00:03+00:00');
    expect(state.fake.rows('tombstone')[0]?.['scrubbed_at']).toBe('2026-03-14T15:00:02+00:00');
    expect(state.fake.db.prepare("SELECT name FROM sqlite_master WHERE name = 'scrub'").all()).toEqual([]);
    expect(state.fake.sql.databaseSize).toBeGreaterThanOrEqual(before);
    const revived = new UserCell(state, env);
    expect((await revived.fetch(identified('/'))).status).toBe(410);
    expect(state.fake.rows('tombstone')).toHaveLength(1);
  });

  it('a snapshot with foreign user columns imports without them', async () => {
    const snap: CellSnapshot = {
      profile: null,
      tables: { decks: [{ id: 7, user_id: 'anon:x', name: 'moved', created_at: 't', deck_type: 'srs', notifications_enabled: 1, notification_ignored_streak: 0, trivia_session_size: 3 }] },
    };
    expect(await cell.importRows(snap)).toEqual({ decks: 1 });
    expect(state.fake.rows('decks')[0]).toMatchObject({ id: 7, name: 'moved' });
  });
});

describe('the declared cells', () => {
  it('JobCell answers 501 until its phase', async () => {
    const res = await new JobCell(fakeState(), env).fetch(req('/'));
    expect(res.status).toBe(501);
  });
});
