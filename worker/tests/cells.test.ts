import { beforeEach, describe, expect, it } from 'vitest';
import { composeWith } from '../runtime/compose.js';
import { DirectoryCell } from '../runtime/cells/DirectoryCell.js';
import { InstantLimiterCell } from '../runtime/cells/InstantLimiterCell.js';
import { JobCell } from '../runtime/cells/JobCell.js';
import { UnknownProfile, UserCell } from '../runtime/cells/UserCell.js';
import type { Env } from '../runtime/env.js';
import { corpusPage, fakeEnv, fakeState, req, spyRenderer } from './helpers.js';

let env: Env;
let renderer: ReturnType<typeof spyRenderer>;
let state: DurableObjectState;
let cell: UserCell;

beforeEach(() => {
  env = fakeEnv();
  renderer = spyRenderer();
  composeWith(env, { renderer });
  state = fakeState();
  cell = new UserCell(state, env);
});

describe('UserCell', () => {
  it('seed resets the state and returns the corpus seed', async () => {
    const seed = await cell.seed('reader');
    expect(seed).toEqual(JSON.parse(JSON.stringify(corpusPage('reader', 'seed'))));
    expect((seed as { decks: { srs_a: { slug: string } } }).decks.srs_a.slug).toBe('world-capitals');
    expect(await state.storage.get('parity')).toEqual({ profile: 'reader', flags: [] });
  });

  it('refuses an unknown profile', async () => {
    await expect(cell.seed('nope')).rejects.toBeInstanceOf(UnknownProfile);
  });

  it('answers the 404 page before any seed', async () => {
    const res = await cell.fetch(req('/'));
    expect(res.status).toBe(404);
    expect(renderer.calls[0]?.template).toBe('error.html');
    expect(renderer.calls[0]?.context.user).toBeNull();
  });

  it('renders the recorded page with the recorded context plus the request origin', async () => {
    await cell.seed('reader');
    const res = await cell.fetch(req('/'));
    const want = corpusPage('reader', '01-GET-root');
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe(want.headers['content-type']);
    expect(renderer.calls).toEqual([{ template: 'index.html', context: { ...want.context, app_base: 'https://parity.example.test' } }]);
  });

  it('takes the origin from the forwarded scheme when the ingress ends TLS', async () => {
    await cell.seed('reader');
    await cell.fetch(new Request('http://cell.internal:8080/settings/api', { headers: { 'x-forwarded-proto': 'https' } }));
    expect(renderer.calls[0]?.context.app_base).toBe('https://cell.internal:8080');
  });

  it('answers a partial by its own template', async () => {
    await cell.seed('empty');
    await cell.fetch(req('/api/active-workflows-badge'));
    expect(renderer.calls[0]?.template).toBe('partials/workflow_badge.html');
  });

  it('replays a redirect and flips the flag it sets', async () => {
    await cell.seed('reader');
    const before = await cell.fetch(req('/deck/world-capitals'));
    expect(before.status).toBe(200);
    expect((renderer.calls[0]?.context.deck_meta as { pinned: boolean }).pinned).toBe(false);

    const pin = await cell.fetch(req('/deck/world-capitals/pin', { method: 'POST' }));
    expect(pin.status).toBe(303);
    expect(pin.headers.get('location')).toBe('/deck/world-capitals');
    expect(await state.storage.get('parity')).toEqual({ profile: 'reader', flags: ['pinned'] });

    await cell.fetch(req('/deck/world-capitals'));
    expect((renderer.calls[1]?.context.deck_meta as { pinned: boolean }).pinned).toBe(true);
  });

  it('keeps flags across an eviction and drops them on reseed', async () => {
    await cell.seed('reader');
    await cell.fetch(req('/settings/agent/byok/anthropic-api/connect', { method: 'POST' }));
    const revived = new UserCell(state, env);
    await revived.fetch(req('/settings/agent'));
    expect(renderer.calls.at(-1)?.context.byok_sections).toEqual(corpusPage('reader', '16-GET-settings-agent@byok').context?.byok_sections);
    await revived.seed('reader');
    expect(await state.storage.get('parity')).toEqual({ profile: 'reader', flags: [] });
  });

  it('answers the 404 page for a path the profile never recorded', async () => {
    await cell.seed('empty');
    const res = await cell.fetch(req('/no-such-page-parity'));
    expect(res.status).toBe(404);
    expect(renderer.calls[0]?.template).toBe('error.html');
    expect(renderer.calls[0]?.context.path).toBe('/no-such-page-parity');
  });
});

describe('the declared cells', () => {
  it('answer 501 until their phase', async () => {
    for (const Cell of [DirectoryCell, InstantLimiterCell, JobCell]) {
      const res = await new Cell(fakeState(), env).fetch(req('/'));
      expect(res.status).toBe(501);
    }
  });
});
