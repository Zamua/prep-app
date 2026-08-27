import { beforeEach, describe, expect, it } from 'vitest';
import worker, { DISPLAY_NAME_HEADER, SUBJECT_HEADER, UserCell } from '../runtime/worker.js';
import { composeWith } from '../runtime/compose.js';
import type { Env } from '../runtime/env.js';
import { corpusPage, fakeEnv, fakeState, IDENTIFIED, namespaceOf, req, spyRenderer } from './helpers.js';
import { EMAIL_HEADER, KIND_HEADER, PAT_HASH_HEADER, PICTURE_HEADER } from '../runtime/cells/router.js';
import { assembleToken } from '../domain/pat.js';
import { mintCookie } from '../runtime/adapters/anonCookie.js';
import { WebCryptoHasher } from '../runtime/adapters/hash.js';

interface Forwarded {
  request: Request;
  name: string;
}

let env: Env;
let renderer: ReturnType<typeof spyRenderer>;
let forwarded: Forwarded[];
let seeded: { name: string; profile: string }[];
/** The rows the owner's dump names, which is where the seed finds its jobs. */
let jobRows: Record<string, Record<string, unknown>[]>;
let jobsWiped: string[];
let forgotten: { owner: string; jobId: string }[];

beforeEach(() => {
  forwarded = [];
  seeded = [];
  jobRows = {};
  jobsWiped = [];
  forgotten = [];
  env = fakeEnv({
    USER: namespaceOf((name) => ({
      fetch: async (request: Request) => {
        forwarded.push({ request, name });
        return new Response('from cell', { headers: { 'content-type': 'text/html; charset=utf-8' } });
      },
      wipe: async (profile: string) => {
        seeded.push({ name, profile: `wipe:${profile}` });
      },
      seed: async (profile: string) => {
        seeded.push({ name, profile });
        return { user: name, profile };
      },
      dump: async () => ({ profile: null, tables: jobRows }),
      forgetJob: async (jobId: string) => void forgotten.push({ owner: name, jobId }),
    })),
    JOB: namespaceOf((name) => ({
      wipe: async () => void jobsWiped.push(name),
    })),
  });
  renderer = spyRenderer();
  composeWith(env, { renderer });
});

const call = (path: string, init: RequestInit = {}) => worker.fetch(req(path, init), env);

/** The router's context for a page equals what Python passed, plus the
 * request origin Python read from `request` itself. */
function expectCorpus(file: string, index = 0) {
  const want = corpusPage('anonymous', file);
  expect(renderer.calls[index]).toEqual({ template: want.template, context: { ...want.context, app_base: 'https://parity.example.test' } });
}

describe('liveness and readiness', () => {
  const broken = () => fakeEnv({ PREP_ENV: 'prod', PREP_PARITY_MODE: '1' });

  it('liveness answers without composing', async () => {
    const res = await worker.fetch(req('/healthz'), broken());
    expect(res.status).toBe(200);
    expect(await res.text()).toBe('ok');
  });

  it('readiness composes first', async () => {
    expect((await worker.fetch(req('/readyz'), broken())).status).toBe(500);
    const res = await call('/readyz');
    expect(res.status).toBe(200);
    expect(await res.text()).toBe('ok');
  });

  it('reports a misconfigured composition as a 500', async () => {
    const res = await worker.fetch(req('/privacy'), broken());
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

  it('answer GET and HEAD only, as the Python routes do', async () => {
    expect((await call('/privacy', { method: 'HEAD' })).status).toBe(200);
    for (const [path, method] of [
      ['/privacy', 'POST'],
      ['/offline', 'PUT'],
    ] as const) {
      const res = await call(path, { method });
      expect(res.status, `${method} ${path}`).toBe(405);
      const last = renderer.calls.at(-1);
      expect(last?.template).toBe('error.html');
      expect(last?.context.status_code).toBe(405);
      expect(last?.context.blurb).toContain('(Method Not Allowed)');
    }
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
    // The wipe is its own RPC and precedes the rows it makes room for.
    expect(seeded).toEqual([
      { name: 'parity@example.com', profile: 'wipe:reader' },
      { name: 'parity@example.com', profile: 'reader' },
    ]);
  });

  it('seed empties the job cells the owner still names, once each', async () => {
    jobRows = {
      active_workflows: [{ workflow_id: 'plan-a' }, { workflow_id: 'grade-b' }],
      job_progress: [{ workflow_id: 'grade-b' }],
    };
    const body = JSON.stringify({ user: 'parity@example.com', profile: 'workflows' });
    const ok = await call('/_parity/seed', { method: 'POST', body, headers: { 'x-internal-token': 'parity-internal-token' } });
    expect(ok.status).toBe(200);
    expect(jobsWiped).toEqual(['plan-a', 'grade-b']);
  });

  it('abandon empties the job cell and leaves its owner nothing to answer with', async () => {
    const headers = { 'x-internal-token': 'parity-internal-token' };
    const body = JSON.stringify({ id: 'plan-a-1', owner: 'parity@example.com' });
    expect((await call('/_parity/job/abandon', { method: 'POST', body })).status).toBe(401);
    expect((await call('/_parity/job/abandon', { method: 'POST', body: '{"id":"x"}', headers })).status).toBe(422);
    const ok = await call('/_parity/job/abandon', { method: 'POST', body, headers });
    expect(ok.status).toBe(200);
    expect(jobsWiped).toEqual(['plan-a-1']);
    expect(forgotten).toEqual([{ owner: 'parity@example.com', jobId: 'plan-a-1' }]);
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

  it('hand the cell the body intact', async () => {
    const seen: unknown[] = [];
    const cellEnv = fakeEnv({
      USER: namespaceOf(() => ({
        fetch: async (request: Request) => {
          seen.push((await request.formData()).get('api_key'));
          return new Response('ok');
        },
      })),
    });
    composeWith(cellEnv, { renderer });
    const res = await worker.fetch(
      req('/settings/agent/byok/anthropic-api/connect', {
        method: 'POST',
        headers: { ...IDENTIFIED, 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ api_key: 'sk-parity' }).toString(),
      }),
      cellEnv,
    );
    expect(res.status).toBe(200);
    expect(seen).toEqual(['sk-parity']);
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
    expect(renderer.calls.at(-1)).toEqual({
      template: 'index.html',
      context: { ...corpusPage('empty', '01-GET-root').context, app_base: 'https://parity.example.test' },
    });
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

describe('the fake provider is gated on the internal token', () => {
  it('ignores the tailscale headers without it, which is a visitor', async () => {
    const res = await call('/', { headers: { 'tailscale-user-login': 'parity@example.com' } });
    expect(res.status).toBe(200);
    expect(renderer.calls.at(-1)?.template).toBe('landing.html');
    expect(forwarded).toEqual([]);
  });

  it('ignores a wrong token the same way', async () => {
    await call('/', { headers: { ...IDENTIFIED, 'x-internal-token': 'not-the-token' } });
    expect(forwarded).toEqual([]);
  });

  it('forwards the claims it does read', async () => {
    await call('/', { headers: { ...IDENTIFIED, 'tailscale-user-profile-pic': 'https://img.test/p.png' } });
    const sent = forwarded[0]!.request.headers;
    expect(sent.get(KIND_HEADER)).toBe('fake');
    expect(decodeURIComponent(sent.get(EMAIL_HEADER)!)).toBe('parity@example.com');
    expect(decodeURIComponent(sent.get(PICTURE_HEADER)!)).toBe('https://img.test/p.png');
  });
});

describe('the provider flows', () => {
  it('404 on this deploy, which has no in-app sign-in or sign-out', async () => {
    expect((await call('/sign-in')).status).toBe(404);
    expect((await call('/sign-out')).status).toBe(404);
    expect(renderer.calls.at(-1)?.context.blurb).toContain('no in-app sign-out flow');
  });

  it('redirect and render the interstitial once a provider exposes them', async () => {
    const clerkish = fakeEnv();
    composeWith(clerkish, {
      renderer,
      identity: {
        name: 'clerk',
        identify: async () => null,
        hasDormantSession: () => false,
        urls: () => ({
          sign_in: 'https://accounts.test/sign-in?redirect_url=x',
          sign_up: null,
          sign_out: 'https://accounts.test/sign-out',
          account: null,
        }),
      },
    });
    const signIn = await worker.fetch(req('/sign-in'), clerkish);
    expect(signIn.status).toBe(303);
    expect(signIn.headers.get('location')).toBe('https://accounts.test/sign-in?redirect_url=x');
    const signOut = await worker.fetch(req('/sign-out'), clerkish);
    expect(signOut.status).toBe(200);
    expect(renderer.calls.at(-1)?.template).toBe('sign_out_interstitial.html');
    expect(renderer.calls.at(-1)?.context.redirect_url).toBe('/');
    // Provider sign-out leaves the anonymous cookie, so the app clears it.
    expect(signOut.headers.get('set-cookie')).toMatch(/^prep_anon=""; expires=/);
  });

  // What the e2e readiness probe rests on: an identity a node's provider
  // cannot verify is refused by the router before any cell is touched, so
  // only the anonymous cookie proves the cells are up on every deploy shape.
  it('routes an anonymous cookie to its own cell whatever the provider is', async () => {
    const clerkish = fakeEnv({ USER: env.USER });
    const c = composeWith(clerkish, {
      renderer,
      identity: {
        name: 'clerk',
        identify: async () => null,
        hasDormantSession: () => false,
        urls: () => ({ sign_in: 'https://accounts.test/sign-in', sign_up: null, sign_out: null, account: null }),
      },
    });
    const id = `anon:${'00'.repeat(16)}`;
    const cookie = await mintCookie((await c.signer())!, id, Math.floor(c.clock.now().getTime() / 1000));
    const res = await worker.fetch(req('/api/dashboard/overview', { headers: { cookie: `prep_anon=${cookie}` } }), clerkish);
    expect(res.status).toBe(200);
    expect(forwarded.at(-1)?.name).toBe(id);
    expect(forwarded.at(-1)?.request.headers.get(KIND_HEADER)).toBe('anon');
  });

  it('forget-device redirects home and drops the cookie', async () => {
    const res = await call('/forget-device', { method: 'POST' });
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe('/');
    expect(res.headers.get('set-cookie')).toMatch(/^prep_anon=""; expires=/);
  });

  it('refuses forget-device from another site, where the confirm never ran', async () => {
    const res = await call('/forget-device', { method: 'POST', headers: { 'sec-fetch-site': 'cross-site' } });
    expect(res.status).toBe(403);
    expect(res.headers.get('set-cookie')).toBeNull();
    const byOrigin = await call('/forget-device', { method: 'POST', headers: { origin: 'https://evil.test', host: 'parity.example.test' } });
    expect(byOrigin.status).toBe(403);
  });

  it('answers 405 on the wrong method', async () => {
    expect((await call('/forget-device')).status).toBe(405);
    expect((await call('/sign-in', { method: 'POST' })).status).toBe(405);
  });
});

describe('a dormant provider session', () => {
  const dormantEnv = (uat: string) => {
    const e = fakeEnv();
    composeWith(e, {
      renderer,
      identity: {
        name: 'clerk',
        identify: async () => null,
        hasDormantSession: (request: Request) => (request.headers.get('cookie') ?? '').includes(`__client_uat=${uat}`),
        urls: () => ({ sign_in: 'https://accounts.test/sign-in', sign_up: null, sign_out: null, account: null }),
      },
    });
    return e;
  };

  it('renders the session-restoring shell instead of the landing page', async () => {
    const e = dormantEnv('1771000000');
    const res = await worker.fetch(req('/', { headers: { cookie: '__client_uat=1771000000' } }), e);
    expect(res.status).toBe(200);
    expect(renderer.calls.at(-1)?.template).toBe('reauth.html');
  });

  it('falls through to the landing page once the shell gives up', async () => {
    const e = dormantEnv('1771000000');
    await worker.fetch(req('/', { headers: { cookie: '__client_uat=1771000000; prep_reauth_fallback=1' } }), e);
    expect(renderer.calls.at(-1)?.template).toBe('landing.html');
  });

  it('never consults the anonymous cookie while it is dormant', async () => {
    const e = dormantEnv('1771000000');
    const res = await worker.fetch(req('/', { headers: { cookie: '__client_uat=1771000000; prep_anon=garbage' } }), e);
    // A stale-cookie delete here would destroy the account behind the value.
    expect(res.headers.get('set-cookie')).toBeNull();
  });
});

describe('a bearer token', () => {
  it('routes to the owner named in the token, with its hash', async () => {
    const token = assembleToken('parity@example.com', new Uint8Array(32).fill(9));
    const res = await call('/api/v1/decks', { headers: { authorization: `Bearer ${token}` } });
    expect(res.status).toBe(200);
    expect(forwarded[0]?.name).toBe('parity@example.com');
    const sent = forwarded[0]!.request.headers;
    expect(sent.get(KIND_HEADER)).toBe('pat');
    expect(sent.get(PAT_HASH_HEADER)).toBe(await new WebCryptoHasher().sha256Hex(token));
  });

  it('answers the refusals Python answers, before any cell is reached', async () => {
    expect(await (await call('/api/v1/decks')).json()).toEqual({ detail: 'missing Authorization header' });
    expect(await (await call('/api/v1/decks', { headers: { authorization: 'Basic x' } })).json()).toEqual({
      detail: "Authorization must be 'Bearer <token>'",
    });
    expect(await (await call('/api/v1/decks', { headers: { authorization: 'Bearer prep_pat_legacy' } })).json()).toEqual({
      detail: 'invalid or revoked token',
    });
    expect(forwarded).toEqual([]);
  });

  it('is not consulted on a page route, where the cookie rules', async () => {
    const token = assembleToken('parity@example.com', new Uint8Array(32));
    await call('/deck/x', { headers: { ...IDENTIFIED, authorization: `Bearer ${token}` } });
    expect(forwarded[0]?.request.headers.get(KIND_HEADER)).toBe('pat');
  });
});
