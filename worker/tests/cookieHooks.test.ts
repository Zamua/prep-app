import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';
import { resolveIdentity, type CookieVerdict } from '../app/auth/resolve';
import { REFRESH_AFTER_SECONDS } from '../domain/anonCookie';
import { ANON_COOKIE_HEADER, composeWith, cookieHooks, type Composition } from '../runtime/compose';
import type { Env } from '../runtime/env';
import { NoIdentityProvider } from '../runtime/adapters/fakeIdentity';
import { fakeEnv, req } from './helpers';

// The recorded responses are the gate for these bytes: a browser that stops
// accepting the value stops holding the account.
interface Pair {
  name: string;
  request: { method: string; path: string; headers: Record<string, string> | null };
  response: { status: number; set_cookie: string[] | null };
}

const pairs: Pair[] = JSON.parse(readFileSync(join(new URL('.', import.meta.url).pathname, 'fixtures', 'api-contract.json'), 'utf8')).pairs;
const pair = (name: string): Pair => {
  const found = pairs.find((p) => p.name === name);
  if (!found) throw new Error(`no such contract pair: ${name}`);
  return found;
};

const PARITY_NOW = 1773500400;
const MASTER = '11'.repeat(32);
/** `expires` on a delete is the wall clock at recording time, so only its
 * presence and the rest of the value are the contract. */
const normalise = (header: string) => header.replace(/expires=[^;]+/, 'expires=<now>');

let env: Env;
let c: Composition;

beforeEach(() => {
  env = fakeEnv({ PREP_KEY_ENCRYPTION_SECRET: MASTER });
  c = composeWith(env, { identity: new NoIdentityProvider() });
});

/** One recorded request replayed through the resolver and the response hook. */
async function replay(name: string, opts: { now?: number; ask?: string } = {}): Promise<string[]> {
  const p = pair(name);
  const now = opts.now ?? PARITY_NOW;
  const headers = { ...(p.request.headers ?? {}) };
  const request = req(p.request.path, { method: p.request.method, headers });
  const resolution = await resolveIdentity(request, {
    provider: c.identity,
    signer: await c.signer(),
    nowUnix: now,
    cookieValue: headers['cookie']?.replace(/^prep_anon=/, '') ?? null,
  });
  // The cell answers 401 for a cookie whose row is gone; the corpus pairs
  // that clear are the ones the resolver itself refused, so the verdict is
  // the whole input here.
  const response = new Response(null, { status: p.response.status });
  if (opts.ask) response.headers.set(ANON_COOKIE_HEADER, opts.ask);
  const withClock = req(p.request.path, { method: p.request.method, headers: { ...headers, 'x-prep-now': new Date(now * 1000).toISOString() } });
  const out = await cookieHooks(c, withClock, resolution.cookie, response);
  return out.headers.getSetCookie();
}

const expected = (name: string): string[] => (pair(name).response.set_cookie ?? []).map(normalise);

describe('the contract corpus Set-Cookie sequences', () => {
  it('a visitor who generates gets exactly the recorded fresh cookie', async () => {
    expect(await replay('instant-visitor-mints', { ask: 'mint=anon:75667288ef1fdcbaf8bb23f942f81d65' })).toEqual(expected('instant-visitor-mints'));
  });

  it('a fresh cookie is left alone', async () => {
    expect(await replay('cookie-fresh-no-refresh')).toEqual([]);
    expect(expected('cookie-fresh-no-refresh')).toEqual([]);
  });

  it('a cookie past the refresh window is re-minted to the recorded value', async () => {
    const later = PARITY_NOW + REFRESH_AFTER_SECONDS + 1;
    expect(await replay('cookie-refreshed-after-window', { now: later })).toEqual(expected('cookie-refreshed-after-window'));
  });

  it('the re-minted value is then accepted without another refresh', async () => {
    const later = PARITY_NOW + REFRESH_AFTER_SECONDS + 1;
    expect(await replay('cookie-refreshed-value-accepted', { now: later })).toEqual([]);
  });

  it('a future, forged or garbage cookie is deleted', async () => {
    for (const name of ['cookie-from-the-future-cleared', 'cookie-bad-signature-cleared', 'cookie-garbage-cleared']) {
      expect((await replay(name)).map(normalise), name).toEqual(expected(name));
    }
  });

  it('forget-device deletes it', async () => {
    expect((await replay('forget-device', { ask: 'clear' })).map(normalise)).toEqual(expected('forget-device'));
  });
});

describe('the hook precedence', () => {
  const stale: CookieVerdict = { kind: 'stale' };
  const refresh: CookieVerdict = { kind: 'refresh', externalId: 'anon:' + 'ab'.repeat(16) };
  const at = { 'x-prep-now': new Date(PARITY_NOW * 1000).toISOString() };

  const run = async (verdict: CookieVerdict, ask?: string) => {
    const res = new Response(null);
    if (ask) res.headers.set(ANON_COOKIE_HEADER, ask);
    return (await cookieHooks(c, req('/', { headers: at }), verdict, res)).headers.getSetCookie();
  };

  it('a mint supersedes a pending stale or refresh', async () => {
    const minted = await run(stale, 'mint=anon:' + 'cd'.repeat(16));
    expect(minted).toHaveLength(1);
    expect(minted[0]).toMatch(/^prep_anon=v1\./);
    expect(await run(refresh, 'mint=anon:' + 'cd'.repeat(16))).toEqual(minted);
  });

  it('a clear beats a refresh', async () => {
    const cleared = await run(refresh, 'clear');
    expect(cleared[0]).toMatch(/^prep_anon=""; expires=/);
  });

  it('never leaks the internal header to the client', async () => {
    const res = new Response(null, { headers: { [ANON_COOKIE_HEADER]: 'clear' } });
    const out = await cookieHooks(c, req('/', { headers: at }), { kind: 'none' }, res);
    expect(out.headers.get(ANON_COOKIE_HEADER)).toBeNull();
  });

  it('writes nothing when there is nothing to say', async () => {
    const res = new Response('body');
    const out = await cookieHooks(c, req('/', { headers: at }), { kind: 'none' }, res);
    expect(out).toBe(res);
    expect(await out.text()).toBe('body');
  });

  it('omits Secure on a plain-http request', async () => {
    const res = new Response(null, { headers: { [ANON_COOKIE_HEADER]: 'clear' } });
    const out = await cookieHooks(c, new Request('http://parity.example.test/', { headers: at }), { kind: 'none' }, res);
    expect(out.headers.get('set-cookie')).not.toMatch(/Secure/);
  });

  it('honours a forwarded https scheme, because TLS ends at the ingress', async () => {
    const res = new Response(null, { headers: { [ANON_COOKIE_HEADER]: 'clear' } });
    const request = new Request('http://parity.example.test/', { headers: { ...at, 'x-forwarded-proto': 'https,http' } });
    expect((await cookieHooks(c, request, { kind: 'none' }, res)).headers.get('set-cookie')).toMatch(/Secure$/);
  });

  it('mints on the instant the router resolved on, not the composition default', async () => {
    // The hook sees the inbound request, which spells the parity clock
    // `x-parity-now`; resolving a refresh on one clock and stamping it on
    // another re-refreshes the cookie on every later request.
    const advanced = PARITY_NOW + REFRESH_AFTER_SECONDS + 1;
    const request = req('/', { headers: { 'x-parity-now': new Date(advanced * 1000).toISOString() } });
    const out = await cookieHooks(c, request, { kind: 'refresh', externalId: 'anon:' + 'ab'.repeat(16) }, new Response(null));
    expect(out.headers.get('set-cookie')!.split('.')[2]).toBe(String(advanced));
  });

  it('cannot mint without a signing secret, so nothing is written', async () => {
    const bare = fakeEnv({ PREP_KEY_ENCRYPTION_SECRET: undefined });
    const off = composeWith(bare, { identity: new NoIdentityProvider() });
    const res = new Response(null, { headers: { [ANON_COOKIE_HEADER]: 'mint=anon:' + 'ab'.repeat(16) } });
    const out = await cookieHooks(off, req('/', { headers: at }), { kind: 'none' }, res);
    expect(out.headers.getSetCookie()).toEqual([]);
  });
});
