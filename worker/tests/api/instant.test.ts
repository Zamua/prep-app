// Instant generation: the refusal ladder, the account it mints, and the
// bucket the limiter charges. Driven through the entry worker, because a
// visitor has no cell and the whole point is what happens before one.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ANON_MAX_DECKS } from '../../domain/limits.js';
import { clientIp, MAX_BODY_BYTES } from '../../runtime/routes/instant.js';
import type { Env } from '../../runtime/env.js';
import worker from '../../runtime/worker.js';
import { ORIGIN, SEED_USER, replayEnv, seed } from './harness.js';

const DECK = JSON.stringify([
  { q: 'Q1?', a: 'a1', r: 'a1' },
  { q: 'Q2?', a: 'a2', r: 'a2' },
  { q: 'Q3?', a: 'a3', r: 'a3' },
  { q: 'Q4?', a: 'a4', r: 'a4' },
  { q: 'Q5?', a: 'a5', r: 'a5' },
]);

function stubAgent(text: string = DECK): void {
  vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (url.includes('/chat/completions')) return Response.json({ choices: [{ message: { content: text } }] });
    throw new Error(`unexpected outbound fetch to ${url}`);
  });
}

async function generate(env: Env, body: unknown, headers: Record<string, string> = {}): Promise<{ status: number; json: Record<string, unknown>; res: Response }> {
  const payload = typeof body === 'string' ? body : JSON.stringify(body);
  const res = await worker.fetch(
    new Request(`${ORIGIN}/api/instant/generate`, {
      method: 'POST',
      headers: { accept: 'application/json', 'content-type': 'application/json', 'content-length': String(payload.length), ...headers },
      body: payload,
    }),
    env,
  );
  return { status: res.status, json: (await res.clone().json()) as Record<string, unknown>, res };
}

const SIGNED_IN = { 'tailscale-user-login': SEED_USER, 'tailscale-user-name': 'Seed', 'x-internal-token': 'test-internal-token' };

describe('the limiter bucket', () => {
  const at = (header: string | null, value?: string) =>
    clientIp(new Request(ORIGIN, { headers: value === undefined ? {} : { [header!]: value } }), { PREP_CLIENT_IP_HEADER: header ?? undefined });

  it('keys IPv4 exactly and IPv6 on the /64', () => {
    expect(at('x-real-ip', '198.51.100.7')).toBe('198.51.100.7');
    expect(at('x-real-ip', '2001:db8:1:2:3:4:5:6')).toBe('2001:db8:1:2::/64');
  });

  it('fails closed to the shared sentinel for a missing or unparsable value', () => {
    expect(at('x-real-ip')).toBe('unresolved');
    expect(at('x-real-ip', 'not-an-ip')).toBe('unresolved');
  });

  it('reads the last X-Forwarded-For entry when told to', () => {
    const request = new Request(ORIGIN, { headers: { 'x-forwarded-for': '10.0.0.1, 198.51.100.7' } });
    expect(clientIp(request, { PREP_CLIENT_IP_HEADER: 'x-forwarded-for-last' })).toBe('198.51.100.7');
  });
});

describe('POST /api/instant/generate', () => {
  beforeEach(() => stubAgent());

  it('refuses an unusable topic before it reaches the limiter', async () => {
    const { env } = replayEnv();
    for (const body of [{ topic: '' }, { topic: ' \n ' }, { topic: 'x'.repeat(501) }, { nope: 1 }, 'not json']) {
      const { status, json } = await generate(env, body);
      expect(status).toBe(422);
      expect(json).toEqual({ kind: 'invalid_topic', message: 'Describe your topic in 1 to 500 characters.' });
    }
  });

  it('refuses a body past the cap without reading it', async () => {
    const { env } = replayEnv();
    const res = await worker.fetch(
      new Request(`${ORIGIN}/api/instant/generate`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'content-length': String(MAX_BODY_BYTES + 1) },
        body: JSON.stringify({ topic: 'x'.repeat(MAX_BODY_BYTES) }),
      }),
      env,
    );
    expect(res.status).toBe(422);
  });

  it('refuses when the deploy funds no free tier', async () => {
    const { env } = replayEnv({ PREP_FREE_INFERENCE_API_KEY: '' });
    const { status, json } = await generate(env, { topic: 'the French Revolution' });
    expect(status).toBe(503);
    expect(json).toEqual({ kind: 'not_configured', message: "Instant decks aren't available on this deploy." });
  });

  it('refuses a visitor when anonymous accounts are off', async () => {
    const { env } = replayEnv({ PREP_KEY_ENCRYPTION_SECRET: '', PREP_ANON_COOKIE_SECRET: '' });
    const { status, json } = await generate(env, { topic: 'the French Revolution' });
    expect(status).toBe(503);
    expect(json['kind']).toBe('not_configured');
  });

  it('mints an account, stores the deck and asks for the cookie', async () => {
    const { env } = replayEnv();
    const { status, json, res } = await generate(env, { topic: '  Roman   emperors\n' }, { 'x-real-ip': '198.51.100.8' });
    expect(status).toBe(200);
    expect(json['kind']).toBe('ok');
    expect(String(json['redirect'])).toMatch(/^\/deck\/[a-z0-9]{8}$/);
    expect(res.headers.get('set-cookie')).toMatch(/^prep_anon=v1\./);
  });

  it('charges the same IP a minute-scoped refusal on the second try', async () => {
    const { env } = replayEnv();
    await seed(env, 'reader', SEED_USER);
    const first = await generate(env, { topic: 'the French Revolution' }, { ...SIGNED_IN, 'x-real-ip': '198.51.100.7' });
    expect(first.status).toBe(200);
    const second = await generate(env, { topic: 'the French Revolution' }, { ...SIGNED_IN, 'x-real-ip': '198.51.100.7' });
    expect(second.status).toBe(429);
    expect(second.json).toEqual({
      kind: 'rate_limited',
      scope: 'minute',
      message: 'One deck a minute. Try again shortly.',
      retry_after_s: 60,
    });
    expect(second.res.headers.get('retry-after')).toBe('60');
  });

  it('counts a degenerate answer as spend and reads as a failed generation', async () => {
    stubAgent(JSON.stringify([{ q: 'only', a: 'one' }]));
    const { env } = replayEnv();
    const { status, json } = await generate(env, { topic: 'thin topic' }, { 'x-real-ip': '203.0.113.5' });
    expect(status).toBe(502);
    expect(json).toEqual({ kind: 'generation_failed', message: "That didn't work. Try again." });
  });

  it('refuses a guest already at its deck cap', async () => {
    const { env, userStorage } = replayEnv();
    const minted = await generate(env, { topic: 'first topic' }, { 'x-real-ip': '203.0.113.9' });
    const cookie = minted.res.headers.get('set-cookie')!.split(';')[0]!;
    // Fill the account to its ceiling past the repository: the cap guard
    // would refuse the last write on the way in.
    for (let i = 0; i < ANON_MAX_DECKS; i++) {
      userStorage(anonIdOf(cookie)).sql.exec('INSERT OR IGNORE INTO decks (name, created_at) VALUES (?, ?)', `filler-${i}`, '2026-03-14T15:00:00+00:00');
    }
    const { status, json } = await generate(env, { topic: 'one more' }, { 'x-real-ip': '203.0.113.10', cookie });
    expect(status).toBe(429);
    expect(json['kind']).toBe('deck_limit');
  });
});

/** The account a minted cookie names, read back the way the router does. */
function anonIdOf(cookie: string): string {
  const value = cookie.slice('prep_anon='.length);
  const idPart = value.split('.')[1]!;
  const raw = Uint8Array.from(atob(idPart.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(idPart.length / 4) * 4, '=')), (c) => c.charCodeAt(0));
  return 'anon:' + Array.from(raw, (b) => b.toString(16).padStart(2, '0')).join('');
}
