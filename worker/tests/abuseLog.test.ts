// The entry worker logs abuse events and nothing else: no normal request, no
// path, header, body or user id, and the client only as its subnet.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import worker from '../runtime/worker';
import { composeWith } from '../runtime/compose';
import type { Env } from '../runtime/env';
import { signSvix } from '../runtime/adapters/svix';
import { NoIdentityProvider } from '../runtime/adapters/fakeIdentity';
import { FakeDirectory, FakeUserCells } from './fakes/cells';
import { fakeEnv, req } from './helpers';

const SECRET = 'whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw';
const CLIENT = '203.0.113.77';

let env: Env;
let lines: string[];

beforeEach(() => {
  env = fakeEnv({ CLERK_WEBHOOK_SECRET: SECRET, PREP_CLIENT_IP_HEADER: 'cf-connecting-ip' });
  composeWith(env, { identity: new NoIdentityProvider(), directory: new FakeDirectory(), userCells: new FakeUserCells(env) });
  lines = [];
  vi.spyOn(console, 'log').mockImplementation((...args) => void lines.push(args.join(' ')));
  vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => vi.restoreAllMocks());

const send = (path: string, init: RequestInit = {}, e: Env = env) =>
  worker.fetch(req(path, { ...init, headers: { 'cf-connecting-ip': CLIENT, 'user-agent': 'secret-agent', cookie: 'secret=cookie', ...init.headers } }), e);
const abuse = () => lines.filter((l) => l.startsWith('{"event"')).map((l) => JSON.parse(l));

describe('the entry worker logs abuse events', () => {
  it('a bearer-only route with no token is an auth failure on the pat route', async () => {
    const res = await send('/api/v1/decks');
    expect(res.status).toBe(401);
    expect(abuse()).toEqual([{ event: 'auth-failure', route: 'pat', status: 401, subnet: '203.0.113.0/24', country: null }]);
  });

  it('a signed-out htmx badge poll is an auth failure on the badge route', async () => {
    const res = await send('/api/active-workflows-badge', { headers: { 'hx-request': 'true' } });
    expect(res.status).toBe(401);
    expect(abuse()).toEqual([{ event: 'auth-failure', route: 'badge', status: 401, subnet: '203.0.113.0/24', country: null }]);
  });

  it('a webhook with a bad signature is rejected from its /24', async () => {
    const res = await send('/webhooks/clerk', {
      method: 'POST',
      body: '{}',
      headers: { 'svix-id': 'msg_1', 'svix-timestamp': '1', 'svix-signature': 'v1,bad' },
    });
    expect(res.status).toBe(400);
    expect(abuse()).toEqual([{ event: 'webhook-rejected', route: 'webhook', status: 400, subnet: '203.0.113.0/24', country: null }]);
  });

  it('a webhook that verifies and then fails to parse is not abuse', async () => {
    const raw = '{"nope":1}';
    const ts = String(Math.floor(new Date('2026-03-14T15:00:00Z').getTime() / 1000));
    const res = await send('/webhooks/clerk', {
      method: 'POST',
      body: raw,
      headers: { 'svix-id': 'msg_2', 'svix-timestamp': ts, 'svix-signature': await signSvix(SECRET, 'msg_2', ts, raw) },
    });
    expect(res.status).toBe(422);
    expect(abuse()).toEqual([]);
  });

  it('an IPv6 client is logged as its /40', async () => {
    await send('/api/v1/decks', { headers: { 'cf-connecting-ip': '2001:db8:abcd:1234::1' } });
    expect(abuse().map((l) => l.subnet)).toEqual(['2001:db8:ab00::/40']);
  });

  it('a request on Workers without the configured header falls back to the edge-set one', async () => {
    const e = fakeEnv({ CLERK_WEBHOOK_SECRET: SECRET });
    composeWith(e, { identity: new NoIdentityProvider(), directory: new FakeDirectory(), userCells: new FakeUserCells(e) });
    const request = req('/api/v1/decks', { headers: { 'cf-connecting-ip': CLIENT } });
    Object.defineProperty(request, 'cf', { value: { country: 'de' } });
    await worker.fetch(request, e);
    expect(abuse()).toEqual([{ event: 'auth-failure', route: 'pat', status: 401, subnet: '203.0.113.0/24', country: 'DE' }]);
  });

  it('a request off Workers does not trust a client-set edge header', async () => {
    const e = fakeEnv({ CLERK_WEBHOOK_SECRET: SECRET });
    composeWith(e, { identity: new NoIdentityProvider(), directory: new FakeDirectory(), userCells: new FakeUserCells(e) });
    await send('/api/v1/decks', {}, e);
    expect(abuse().map((l) => l.subnet)).toEqual(['unknown']);
  });
});

describe('the entry worker logs nothing else', () => {
  it('stays silent for normal traffic', async () => {
    for (const path of ['/', '/privacy', '/offline', '/healthz', '/no-such-page-unknown']) await send(path);
    expect(abuse()).toEqual([]);
  });

  it('puts no path, header, cookie or agent in a line', async () => {
    await send('/api/v1/decks/secret-deck-id', { headers: { authorization: 'Bearer secret-token' } });
    expect(abuse().length).toBe(1);
    expect(lines.join('\n')).not.toMatch(/secret|203\.0\.113\.77|decks/);
  });

  it('a failing log write never fails the request', async () => {
    (console.log as ReturnType<typeof vi.fn>).mockImplementation(() => {
      throw new Error('sink down');
    });
    expect((await send('/api/v1/decks')).status).toBe(401);
  });
});
