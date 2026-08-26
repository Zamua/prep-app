import { generateKeyPairSync, sign as nodeSign, type KeyObject } from 'node:crypto';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { b64uEncode } from '../domain/base64';
import {
  bearerOrSessionCookie,
  CLOCK_SKEW_MS,
  ClerkConfigError,
  ClerkProvider,
  ClerkVerifier,
  clerkConfig,
  frontendApiHost,
  identityFromClaims,
  quotePlus,
  resetJwksCache,
  type ClerkConfig,
  type Jwk,
} from '../runtime/adapters/clerk';
import { FixedClock } from '../runtime/adapters/clock';
import { req } from './helpers';

const NOW = new Date('2026-03-14T15:00:00Z');
const clock = new FixedClock(NOW);
const unix = Math.floor(NOW.getTime() / 1000);

/** One keypair per run, exported as the JWKS the verifier fetches: the same
 * shape Clerk publishes, signed by a key only this file holds. */
function keypair(kid: string): { jwk: Jwk; sign: (data: string) => string } {
  const { publicKey, privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const jwk = publicKey.export({ format: 'jwk' }) as unknown as Jwk;
  return {
    jwk: { ...jwk, kid, alg: 'RS256', use: 'sig' },
    sign: (data: string) => b64uEncode(new Uint8Array(nodeSign('sha256', Buffer.from(data, 'ascii'), privateKey as KeyObject))),
  };
}

const primary = keypair('kid-1');
const rotated = keypair('kid-2');

const CONFIG: ClerkConfig = {
  issuer: 'https://clerk.example.test',
  jwksUrl: 'https://clerk.example.test/.well-known/jwks.json',
  authorizedParties: ['https://app.example.test', 'https://celld.app.example.test'],
  accountsUrl: 'https://accounts.example.test',
  publishableKey: 'pk_test_' + Buffer.from('clerk.example.test$').toString('base64'),
  secretKey: 'sk_test_secret',
};

const BASE = { iss: CONFIG.issuer, sub: 'user_2abc', exp: unix + 60, nbf: unix - 10 };

function jwt(claims: Record<string, unknown>, key = primary, header: Record<string, unknown> = {}): string {
  const head = b64uEncode(new TextEncoder().encode(JSON.stringify({ alg: 'RS256', typ: 'JWT', kid: key.jwk.kid, ...header })));
  const body = b64uEncode(new TextEncoder().encode(JSON.stringify(claims)));
  return `${head}.${body}.${key.sign(`${head}.${body}`)}`;
}

type JwksFetch = typeof fetch & { calls(): number; serve(keys: Jwk[]): void };

function jwksFetch(keys: Jwk[]): JwksFetch {
  let current = keys;
  const impl = vi.fn(async () => new Response(JSON.stringify({ keys: current }), { headers: { 'content-type': 'application/json' } }));
  const f = impl as unknown as JwksFetch;
  f.calls = () => impl.mock.calls.length;
  f.serve = (next: Jwk[]) => {
    current = next;
  };
  return f;
}

beforeEach(() => resetJwksCache());

describe('the configuration', () => {
  it('names the first var that is missing', () => {
    expect(() => clerkConfig({})).toThrow(ClerkConfigError);
    expect(() => clerkConfig({ CLERK_ISSUER: 'x' })).toThrow(/CLERK_JWKS_URL/);
    expect(() => clerkConfig({ CLERK_ISSUER: 'x', CLERK_JWKS_URL: 'y', CLERK_AUTHORIZED_PARTIES: ' , ' })).toThrow(/at least one origin/);
  });

  it('splits the parties and trims the accounts URL', () => {
    const config = clerkConfig({
      CLERK_ISSUER: 'https://i',
      CLERK_JWKS_URL: 'https://j',
      CLERK_AUTHORIZED_PARTIES: 'https://a, https://b ,',
      CLERK_ACCOUNTS_URL: 'https://accounts.example.test//',
      CLERK_PUBLISHABLE_KEY: 'pk_test_aQ',
    });
    expect(config.authorizedParties).toEqual(['https://a', 'https://b']);
    expect(config.accountsUrl).toBe('https://accounts.example.test');
    expect(config.publishableKey).toBe('pk_test_aQ');
  });

  // Without it ClerkJS never loads, so a signed-in user whose token expired
  // has no path back and the reauth page cannot close its loop.
  it('refuses to boot without the publishable key', () => {
    expect(() =>
      clerkConfig({
        CLERK_ISSUER: 'https://i',
        CLERK_JWKS_URL: 'https://j',
        CLERK_AUTHORIZED_PARTIES: 'https://a',
        CLERK_ACCOUNTS_URL: 'https://accounts.example.test',
      }),
    ).toThrow(/CLERK_PUBLISHABLE_KEY/);
  });
});

describe('the verifier', () => {
  const verifierOn = (keys: Jwk[]) => {
    const f = jwksFetch(keys);
    return { verifier: new ClerkVerifier(CONFIG, clock, f), fetch: f };
  };

  it('accepts a well-formed session token and returns its claims', async () => {
    const { verifier } = verifierOn([primary.jwk]);
    const payload = await verifier.verify(jwt({ ...BASE, azp: 'https://app.example.test', email: 'a@b.test', name: 'A B' }));
    expect(payload?.['sub']).toBe('user_2abc');
    expect(identityFromClaims(payload!)).toEqual({
      subject: 'user_2abc',
      kind: 'clerk',
      email: 'a@b.test',
      displayName: 'A B',
      profilePicUrl: null,
    });
  });

  it('reads the claim aliases Python reads, in its order', () => {
    expect(identityFromClaims({ sub: 's', primary_email: 'p@b.test', username: 'u', image_url: 'i' })).toEqual({
      subject: 's',
      kind: 'clerk',
      email: 'p@b.test',
      displayName: 'u',
      profilePicUrl: 'i',
    });
    expect(identityFromClaims({ sub: 's', email: 'e', primary_email: 'p', full_name: 'f', username: 'u', picture: 'pic', image_url: 'i' })).toEqual({
      subject: 's',
      kind: 'clerk',
      email: 'e',
      displayName: 'f',
      profilePicUrl: 'pic',
    });
    expect(identityFromClaims({ sub: 's' })).toEqual({ subject: 's', kind: 'clerk', email: null, displayName: null, profilePicUrl: null });
  });

  it('refuses every token that fails a claim check', async () => {
    const { verifier } = verifierOn([primary.jwk]);
    const cases: [string, string][] = [
      ['a foreign issuer', jwt({ ...BASE, iss: 'https://evil.test' })],
      ['no subject', jwt({ ...BASE, sub: '' })],
      ['no expiry', jwt({ iss: CONFIG.issuer, sub: 'u' })],
      ['expired past the skew', jwt({ ...BASE, exp: unix - 10 })],
      ['not yet valid', jwt({ ...BASE, nbf: unix + 60 })],
      ['an unauthorized party', jwt({ ...BASE, azp: 'https://evil.test' })],
      ['the wrong algorithm', jwt({ ...BASE }, primary, { alg: 'HS256' })],
    ];
    for (const [why, token] of cases) expect(await verifier.verify(token), why).toBeNull();
  });

  it('allows the clock to drift by the tolerance Clerk itself uses', async () => {
    const { verifier } = verifierOn([primary.jwk]);
    const justExpired = Math.floor((NOW.getTime() - CLOCK_SKEW_MS / 2) / 1000);
    expect(await verifier.verify(jwt({ ...BASE, exp: justExpired }))).not.toBeNull();
    expect(await verifier.verify(jwt({ ...BASE, nbf: unix + Math.floor(CLOCK_SKEW_MS / 2000) }))).not.toBeNull();
  });

  it('refuses a tampered payload and a malformed token', async () => {
    const { verifier } = verifierOn([primary.jwk]);
    const token = jwt({ ...BASE });
    const [head, , sig] = token.split('.');
    const forged = b64uEncode(new TextEncoder().encode(JSON.stringify({ ...BASE, sub: 'user_someone_else' })));
    expect(await verifier.verify(`${head}.${forged}.${sig}`)).toBeNull();
    expect(await verifier.verify('nonsense')).toBeNull();
    expect(await verifier.verify('a.b.c')).toBeNull();
    expect(await verifier.verify('')).toBeNull();
  });

  it('signs off on a rotated key by refetching the JWKS exactly once', async () => {
    const f = jwksFetch([primary.jwk]);
    const verifier = new ClerkVerifier(CONFIG, clock, f);
    expect(await verifier.verify(jwt({ ...BASE }))).not.toBeNull();
    expect(f.calls()).toBe(1);
    // Clerk rotated: the new kid is unknown, so one refetch is warranted.
    f.serve([rotated.jwk]);
    expect(await verifier.verify(jwt({ ...BASE }, rotated))).not.toBeNull();
    expect(f.calls()).toBe(2);
  });

  it('does not refetch for a known kid that simply failed', async () => {
    const f = jwksFetch([primary.jwk]);
    const verifier = new ClerkVerifier(CONFIG, clock, f);
    const token = jwt({ ...BASE });
    const [head, body] = token.split('.');
    expect(await verifier.verify(`${head}.${body}.${b64uEncode(new Uint8Array(256))}`)).toBeNull();
    expect(f.calls()).toBe(1);
  });

  it('answers null when the JWKS cannot be read', async () => {
    const failing = (async () => new Response('nope', { status: 500 })) as unknown as typeof fetch;
    expect(await new ClerkVerifier(CONFIG, clock, failing).verify(jwt({ ...BASE }))).toBeNull();
    const throwing = (async () => {
      throw new Error('offline');
    }) as unknown as typeof fetch;
    expect(await new ClerkVerifier(CONFIG, clock, throwing).verify(jwt({ ...BASE }))).toBeNull();
  });

  it('fetches the JWKS once for concurrent verifications', async () => {
    const f = jwksFetch([primary.jwk]);
    const verifier = new ClerkVerifier(CONFIG, clock, f);
    const tokens = [jwt({ ...BASE }), jwt({ ...BASE, sub: 'user_2xyz' }), jwt({ ...BASE, sub: 'user_2def' })];
    const results = await Promise.all(tokens.map((t) => verifier.verify(t)));
    expect(results.every((r) => r !== null)).toBe(true);
    expect(f.calls()).toBe(1);
  });
});

describe('the provider', () => {
  const provider = () => new ClerkProvider(CONFIG, new ClerkVerifier(CONFIG, clock, jwksFetch([primary.jwk])));

  it('takes the token from the Authorization header or the __session cookie', async () => {
    const token = jwt({ ...BASE });
    expect(bearerOrSessionCookie(req('/', { headers: { authorization: `Bearer ${token}` } }))).toBe(token);
    expect(bearerOrSessionCookie(req('/', { headers: { cookie: `__session=${token}; other=x` } }))).toBe(token);
    // The header wins, which is how a scripted caller overrides a stale tab.
    expect(bearerOrSessionCookie(req('/', { headers: { authorization: `Bearer ${token}`, cookie: '__session=stale' } }))).toBe(token);
    expect(bearerOrSessionCookie(req('/', { headers: { authorization: 'Basic xyz' } }))).toBeNull();
    expect(bearerOrSessionCookie(req('/'))).toBeNull();
  });

  it('identifies through either source and refuses a forged one', async () => {
    const token = jwt({ ...BASE, email: 'a@b.test' });
    expect((await provider().identify(req('/', { headers: { authorization: `Bearer ${token}` } })))?.subject).toBe('user_2abc');
    expect((await provider().identify(req('/', { headers: { cookie: `__session=${token}` } })))?.kind).toBe('clerk');
    expect(await provider().identify(req('/', { headers: { cookie: '__session=forged' } }))).toBeNull();
    expect(await provider().identify(req('/'))).toBeNull();
  });

  it('reads __client_uat as the dormant-session flag', () => {
    const p = provider();
    expect(p.hasDormantSession(req('/', { headers: { cookie: '__client_uat=1771000000' } }))).toBe(true);
    expect(p.hasDormantSession(req('/', { headers: { cookie: '__client_uat=0' } }))).toBe(false);
    expect(p.hasDormantSession(req('/', { headers: { cookie: '__client_uat=' } }))).toBe(false);
    expect(p.hasDormantSession(req('/'))).toBe(false);
  });

  it('builds the hosted URLs against the first authorized party', () => {
    expect(provider().urls()).toEqual({
      sign_in: 'https://accounts.example.test/sign-in?redirect_url=https%3A%2F%2Fapp.example.test%2F',
      sign_up: 'https://accounts.example.test/sign-up?redirect_url=https%3A%2F%2Fapp.example.test%2F',
      sign_out: 'https://accounts.example.test/sign-out?redirect_url=https%3A%2F%2Fapp.example.test%2F',
      account: 'https://accounts.example.test/user',
    });
  });

  it('encodes the redirect the way quote_plus does', () => {
    expect(quotePlus('https://a.test/')).toBe('https%3A%2F%2Fa.test%2F');
    expect(quotePlus('a b')).toBe('a+b');
    expect(quotePlus('~_.-')).toBe('~_.-');
    expect(quotePlus('é')).toBe('%C3%A9');
  });

  it('decodes the frontend API host out of the publishable key', () => {
    expect(frontendApiHost(CONFIG.publishableKey)).toBe('clerk.example.test');
    expect(provider().frontendApiHost).toBe('clerk.example.test');
    expect(frontendApiHost('pk_test_' + Buffer.from('clerk.example.test$').toString('base64').replace(/=+$/, ''))).toBe('clerk.example.test');
    expect(frontendApiHost('')).toBeNull();
    expect(frontendApiHost('no-underscore')).toBeNull();
    expect(frontendApiHost('pk_test_!!!not-base64!!!')).toBeNull();
  });
});
