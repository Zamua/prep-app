import { beforeEach, describe, expect, it } from 'vitest';
import worker, { DISPLAY_NAME_HEADER, SUBJECT_HEADER, UserCell } from '../runtime/worker.js';
import { composeWith } from '../runtime/compose.js';
import type { Env } from '../runtime/env.js';
import { corpusPage, fakeEnv, fakeState, IDENTIFIED, namespaceOf, req, spyRenderer } from './helpers.js';

interface Forwarded {
  request: Request;
  name: string;
}

let env: Env;
let renderer: ReturnType<typeof spyRenderer>;
let forwarded: Forwarded[];
let seeded: { name: string; profile: string }[];

beforeEach(() => {
  forwarded = [];
  seeded = [];
  env = fakeEnv({
    USER: namespaceOf((name) => ({
      fetch: async (request: Request) => {
        forwarded.push({ request, name });
        return new Response('from cell', { headers: { 'content-type': 'text/html; charset=utf-8' } });
      },
      seed: async (profile: string) => {
        seeded.push({ name, profile });
        return { user: name, profile };
      },
    })),
  });
  renderer = spyRenderer();
  composeWith(env, { renderer });
});

const call = (path: string, init: RequestInit = {}) => worker.fetch(req(path, init), env);

/** The router's context for a page equals what Python passed. */
function expectCorpus(file: string, index = 0) {
  const want = corpusPage('anonymous', file);
  expect(renderer.calls[index]).toEqual({ template: want.template, context: want.context });
}

describe('liveness', () => {
  it('answers without composing', async () => {
    const broken = fakeEnv({ PREP_ENV: 'prod', PREP_PARITY_MODE: '1' });
    for (const path of ['/healthz', '/readyz']) {
      const res = await worker.fetch(req(path), broken);
      expect(res.status).toBe(200);
      expect(await res.text()).toBe('ok');
    }
  });

  it('reports a misconfigured composition as a 500', async () => {
    const res = await worker.fetch(req('/privacy'), fakeEnv({ PREP_ENV: 'prod', PREP_PARITY_MODE: '1' }));
    expect(res.status).toBe(500);
  });
});

describe('unauthenticated pages', () => {
  it('/privacy renders with no user and no-cache', async () => {
    const res = await call('/privacy');
    expect(res.status).toBe(200);
    expect(res.headers.get('cache-control')).toBe('no-cache');
    expectCorpus('02-GET-privacy');
  });

  it('/offline echoes an accepted build only', async () => {
    await call('/offline?build=abc1234');
    expect(renderer.calls[0]).toEqual({ template: 'offline.html', context: expect.objectContaining({ build: 'abc1234' }) });
    await call('/offline?build=../etc');
    expect(renderer.calls[1]?.context.build).toBe('ce11d0000000');
    await call('/offline');
    expect(renderer.calls[2]?.context.build).toBe('ce11d0000000');
  });

  it('anonymous GET / is the landing page from the fixture', async () => {
    const res = await call('/');
    expect(res.status).toBe(200);
    expectCorpus('01-GET-root');
    expect(forwarded).toEqual([]);
  });

  it('anonymous elsewhere is the 404 page', async () => {
    const res = await call('/no-such-page-parity');
    expect(res.status).toBe(404);
    expectCorpus('05-GET-no-such-page-parity');
    expect(res.headers.get('cache-control')).toBe('no-cache');
  });
});

describe('the parity routes', () => {
  it('raise renders the 500 and 429 pages', async () => {
    expect((await call('/_parity/raise')).status).toBe(500);
    expectCorpus('06-GET-_parity-raise', 0);
    expect((await call('/_parity/raise?status=429')).status).toBe(429);
    expectCorpus('07-GET-_parity-raise-status-429', 1);
  });

  it('reauth and sign-out render their shells', async () => {
    expect((await call('/_parity/reauth')).status).toBe(200);
    expectCorpus('03-GET-_parity-reauth', 0);
    expect((await call('/_parity/sign-out')).status).toBe(200);
    expectCorpus('04-GET-_parity-sign-out', 1);
  });

  it('seed checks the internal token, then forwards to the user cell', async () => {
    const body = JSON.stringify({ user: 'parity@example.com', profile: 'reader' });
    expect((await call('/_parity/seed', { method: 'POST', body })).status).toBe(401);
    expect((await call('/_parity/seed', { method: 'POST', body, headers: { 'x-internal-token': 'wrong' } })).status).toBe(401);
    const ok = await call('/_parity/seed', { method: 'POST', body, headers: { 'x-internal-token': 'parity-internal-token' } });
    expect(ok.status).toBe(200);
    expect(await ok.json()).toEqual({ user: 'parity@example.com', profile: 'reader' });
    expect(seeded).toEqual([{ name: 'parity@example.com', profile: 'reader' }]);
  });

  it('seed fails closed without a configured token', async () => {
    const bare = fakeEnv({ PREP_INTERNAL_TOKEN: undefined, USER: env.USER });
    const res = await worker.fetch(
      req('/_parity/seed', { method: 'POST', body: '{"user":"u","profile":"reader"}', headers: { 'x-internal-token': '' } }),
      bare,
    );
    expect(res.status).toBe(503);
  });

  it('seed rejects a malformed body', async () => {
    const headers = { 'x-internal-token': 'parity-internal-token' };
    expect((await call('/_parity/seed', { method: 'POST', body: 'nope', headers })).status).toBe(400);
    expect((await call('/_parity/seed', { method: 'POST', body: '{"user":"u"}', headers })).status).toBe(422);
  });

  it('do not exist outside parity', async () => {
    const plain = fakeEnv({ PREP_PARITY_MODE: undefined });
    composeWith(plain, { renderer });
    expect((await worker.fetch(req('/_parity/raise'), plain)).status).toBe(404);
    expect((await worker.fetch(req('/_parity/seed', { method: 'POST' }), plain)).status).toBe(404);
  });
});

describe('identified requests', () => {
  it('are forwarded to the subject cell with the identity headers', async () => {
    const res = await call('/deck/world-capitals', { headers: IDENTIFIED });
    expect(await res.text()).toBe('from cell');
    expect(res.headers.get('cache-control')).toBe('no-cache');
    expect(forwarded).toHaveLength(1);
    expect(forwarded[0]?.name).toBe('parity@example.com');
    expect(forwarded[0]?.request.headers.get(SUBJECT_HEADER)).toBe('parity@example.com');
    expect(forwarded[0]?.request.headers.get(DISPLAY_NAME_HEADER)).toBe('Parity');
    expect(forwarded[0]?.request.method).toBe('GET');
  });

  it('strip inbound copies of the identity headers', async () => {
    await call('/', { headers: { ...IDENTIFIED, [SUBJECT_HEADER]: 'evil', [DISPLAY_NAME_HEADER]: 'Evil' } });
    expect(forwarded[0]?.request.headers.get(SUBJECT_HEADER)).toBe('parity@example.com');
    expect(forwarded[0]?.request.headers.get(DISPLAY_NAME_HEADER)).toBe('Parity');
    const res = await call('/', { headers: { [SUBJECT_HEADER]: 'evil' } });
    expect(forwarded).toHaveLength(1);
    expect(renderer.calls.at(-1)?.template).toBe('landing.html');
    expect(res.status).toBe(200);
  });

  it('render the 500 page when the cell throws', async () => {
    const boom = fakeEnv({
      USER: namespaceOf(() => ({
        fetch: async () => {
          throw new Error('cell exploded');
        },
      })),
    });
    composeWith(boom, { renderer });
    const res = await worker.fetch(req('/deck/x', { headers: IDENTIFIED }), boom);
    expect(res.status).toBe(500);
    expect(renderer.calls.at(-1)?.context.status_code).toBe(500);
  });
});

describe('router and cell together', () => {
  it('seed then dashboard through the real UserCell', async () => {
    const live = fakeEnv({
      USER: namespaceOf(() => new UserCell(fakeState(), live)),
    });
    composeWith(live, { renderer });
    const seed = await worker.fetch(
      req('/_parity/seed', {
        method: 'POST',
        body: JSON.stringify({ user: 'parity@example.com', profile: 'empty' }),
        headers: { 'x-internal-token': 'parity-internal-token' },
      }),
      live,
    );
    expect(seed.status).toBe(200);
    expect(await seed.json()).toEqual(corpusPage('empty', 'seed'));
    const res = await worker.fetch(req('/', { headers: IDENTIFIED }), live);
    expect(res.status).toBe(200);
    expect(res.headers.get('cache-control')).toBe('no-cache');
    expect(renderer.calls.at(-1)).toEqual({ template: 'index.html', context: corpusPage('empty', '01-GET-root').context });
    const bad = await worker.fetch(
      req('/_parity/seed', {
        method: 'POST',
        body: JSON.stringify({ user: 'parity@example.com', profile: 'nope' }),
        headers: { 'x-internal-token': 'parity-internal-token' },
      }),
      live,
    );
    expect(bad.status).toBe(400);
  });
});
