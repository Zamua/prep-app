import { beforeEach, describe, expect, it } from 'vitest';
import { resolveIdentity, type CookieVerdict } from '../app/auth/resolve';
import {
  assembleCookie,
  cookiePayload,
  REFRESH_AFTER_SECONDS,
} from '../domain/anonCookie';
import { ANON_COOKIE_HEADER, composeWith, cookieHooks, type Composition } from '../runtime/compose';
import type { Env } from '../runtime/env';
import { NoIdentityProvider } from '../runtime/adapters/fakeIdentity';
import { fakeEnv, req } from './helpers';

const TEST_NOW = 1773500400;
const MASTER = '11'.repeat(32);
const EXTERNAL_ID = 'anon:' + 'ab'.repeat(16);
const COOKIE_PATH = '/api/offline/snapshot';
const normalise = (header: string) =>
  header.replace(/expires=[^;]+/, 'expires=<now>');

let env: Env;
let c: Composition;

beforeEach(() => {
  env = fakeEnv({ PREP_KEY_ENCRYPTION_SECRET: MASTER });
  c = composeWith(env, { identity: new NoIdentityProvider() });
});

async function signedCookie(issuedAt = TEST_NOW): Promise<string> {
  const signer = await c.signer();
  if (!signer) throw new Error('cookie signer is disabled');
  const payload = cookiePayload(EXTERNAL_ID, issuedAt);
  return assembleCookie(payload, await signer.sign(payload));
}

interface RunOptions {
  cookie?: string;
  now?: number;
  ask?: string;
  status?: number;
  method?: string;
  path?: string;
}

async function run({
  cookie,
  now = TEST_NOW,
  ask,
  status = 200,
  method = 'GET',
  path = COOKIE_PATH,
}: RunOptions = {}): Promise<string[]> {
  const headers: Record<string, string> = {};
  if (cookie) headers['cookie'] = `prep_anon=${cookie}`;
  const request = req(path, { method, headers });
  const resolution = await resolveIdentity(request, {
    provider: c.identity,
    signer: await c.signer(),
    nowUnix: now,
    cookieValue: cookie ?? null,
  });
  const response = new Response(null, { status });
  if (ask) response.headers.set(ANON_COOKIE_HEADER, ask);
  const withClock = req(path, {
    method,
    headers: {
      ...headers,
      'x-prep-now': new Date(now * 1000).toISOString(),
    },
  });
  return (await cookieHooks(c, withClock, resolution.cookie, response))
    .headers.getSetCookie();
}

const cookieValue = (header: string): string => {
  const match = /^prep_anon=([^;]+)/.exec(header);
  if (!match) throw new Error(`missing anonymous cookie: ${header}`);
  return match[1]!;
};

const cleared =
  'prep_anon=""; expires=<now>; HttpOnly; Max-Age=0; Path=/; ' +
  'SameSite=lax; Secure';

describe('the anonymous-cookie lifecycle', () => {
  it('mints a durable cookie that the resolver accepts', async () => {
    const [header] = await run({ ask: `mint=${EXTERNAL_ID}` });
    expect(header).toMatch(
      /^prep_anon=v1\.[A-Za-z0-9_-]+\.1773500400\.[A-Za-z0-9_-]+; /,
    );
    expect(header).toContain(
      'HttpOnly; Max-Age=15552000; Path=/; SameSite=lax; Secure',
    );
    expect(await run({ cookie: cookieValue(header!) })).toEqual([]);
  });

  it('leaves a fresh cookie alone', async () => {
    expect(await run({ cookie: await signedCookie() })).toEqual([]);
  });

  it('refreshes an old cookie once', async () => {
    const original = await signedCookie();
    const later = TEST_NOW + REFRESH_AFTER_SECONDS + 1;
    const [header] = await run({ cookie: original, now: later });
    const refreshed = cookieValue(header!);
    expect(refreshed).not.toBe(original);
    expect(await run({ cookie: refreshed, now: later })).toEqual([]);
  });

  it('clears future, forged and malformed cookies', async () => {
    const valid = await signedCookie();
    const invalid = [
      await signedCookie(TEST_NOW + REFRESH_AFTER_SECONDS + 1),
      valid.slice(0, -1) + (valid.endsWith('A') ? 'B' : 'A'),
      'not-a-cookie',
    ];
    for (const cookie of invalid) {
      expect((await run({ cookie, status: 401 })).map(normalise))
        .toEqual([cleared]);
    }
  });

  it('clears the cookie when the device is forgotten', async () => {
    expect((await run({
      cookie: await signedCookie(),
      ask: 'clear',
      status: 303,
      method: 'POST',
      path: '/forget-device',
    })).map(normalise)).toEqual([cleared]);
  });
});

describe('the hook precedence', () => {
  const stale: CookieVerdict = { kind: 'stale' };
  const refresh: CookieVerdict = { kind: 'refresh', externalId: 'anon:' + 'ab'.repeat(16) };
  const at = { 'x-prep-now': new Date(TEST_NOW * 1000).toISOString() };

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
    const out = await cookieHooks(c, new Request('http://prep.example.test/', { headers: at }), { kind: 'none' }, res);
    expect(out.headers.get('set-cookie')).not.toMatch(/Secure/);
  });

  it('honours a forwarded https scheme, because TLS ends at the ingress', async () => {
    const res = new Response(null, { headers: { [ANON_COOKIE_HEADER]: 'clear' } });
    const request = new Request('http://prep.example.test/', { headers: { ...at, 'x-forwarded-proto': 'https,http' } });
    expect((await cookieHooks(c, request, { kind: 'none' }, res)).headers.get('set-cookie')).toMatch(/Secure$/);
  });

  it('mints on the instant the router resolved on, not the composition default', async () => {
    // The hook sees the inbound request, which spells the pinned clock
    // `x-prep-test-now`; resolving a refresh on one clock and stamping it on
    // another re-refreshes the cookie on every later request.
    const advanced = TEST_NOW + REFRESH_AFTER_SECONDS + 1;
    const request = req('/', { headers: { 'x-prep-test-now': new Date(advanced * 1000).toISOString() } });
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
