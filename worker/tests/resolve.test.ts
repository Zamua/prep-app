import { beforeEach, describe, expect, it } from 'vitest';
import { ANON_DISPLAY_NAME, readCookie, resolveIdentity } from '../app/auth/resolve';
import type { Identity, IdentityProvider, Signer, SignInUrls } from '../app/ports';
import { MAX_AGE_SECONDS, REFRESH_AFTER_SECONDS } from '../domain/anonCookie';
import { HmacSigner, mintCookie, resolveCookieSecret } from '../runtime/adapters/anonCookie';
import { req } from './helpers';

const NOW = 1773500400;
const ANON = 'anon:' + 'ab'.repeat(16);
const OTHER = 'anon:' + 'cd'.repeat(16);
const SUBJECT = 'user_2abc';

const NO_URLS: SignInUrls = { sign_in: null, sign_up: null, sign_out: null, account: null };

/** A provider whose two answers a test sets directly, which is the whole
 * surface the precedence rule reads. */
class StubProvider implements IdentityProvider {
  readonly name = 'stub';
  constructor(
    private readonly who: Identity | null,
    private readonly dormant = false,
  ) {}
  async identify(): Promise<Identity | null> {
    return this.who;
  }
  hasDormantSession(): boolean {
    return this.dormant;
  }
  urls(): SignInUrls {
    return NO_URLS;
  }
}

const signedIn: Identity = { subject: SUBJECT, kind: 'clerk', displayName: 'A B', email: 'a@b.test', profilePicUrl: null };

let signer: Signer;

beforeEach(async () => {
  signer = new HmacSigner((await resolveCookieSecret({ PREP_KEY_ENCRYPTION_SECRET: '11'.repeat(32) }))!);
});

const resolve = (provider: IdentityProvider, cookieValue: string | null, nowUnix = NOW, s: Signer | null = signer) =>
  resolveIdentity(req('/'), { provider, signer: s, nowUnix, cookieValue });

describe('the precedence rule', () => {
  it('a signed-in provider identity wins over any cookie', async () => {
    const cookie = await mintCookie(signer, ANON, NOW);
    const r = await resolve(new StubProvider(signedIn), cookie);
    expect(r.identity).toEqual(signedIn);
    expect(r.merge).toBe(ANON);
    expect(r.cookie).toEqual({ kind: 'none' });
  });

  it('a dormant session stops the cookie from being consulted at all', async () => {
    const cookie = await mintCookie(signer, ANON, NOW);
    const r = await resolve(new StubProvider(null, true), cookie);
    expect(r.identity).toBeNull();
    expect(r.dormant).toBe(true);
    // Not stale: the cookie was never judged, so nothing is decided about it.
    expect(r.cookie).toEqual({ kind: 'none' });
    expect(r.merge).toBeNull();
  });

  it('a live cookie is an anonymous identity named Guest', async () => {
    const cookie = await mintCookie(signer, ANON, NOW);
    const r = await resolve(new StubProvider(null), cookie);
    expect(r.identity).toEqual({ subject: ANON, kind: 'anon', displayName: ANON_DISPLAY_NAME, email: null, profilePicUrl: null });
    expect(r.cookie).toEqual({ kind: 'none' });
  });

  it('no credential at all is a visitor', async () => {
    const r = await resolve(new StubProvider(null), null);
    expect(r).toEqual({ identity: null, dormant: false, cookie: { kind: 'none' }, merge: null });
  });
});

describe('what the cookie earns', () => {
  it('is re-minted once past the refresh window and not before', async () => {
    const cookie = await mintCookie(signer, ANON, NOW);
    expect((await resolve(new StubProvider(null), cookie, NOW + REFRESH_AFTER_SECONDS)).cookie).toEqual({ kind: 'none' });
    expect((await resolve(new StubProvider(null), cookie, NOW + REFRESH_AFTER_SECONDS + 1)).cookie).toEqual({ kind: 'refresh', externalId: ANON });
  });

  it('is deleted when it is forged, expired or from the future', async () => {
    const cookie = await mintCookie(signer, ANON, NOW);
    for (const [why, raw, at] of [
      ['forged', cookie.slice(0, -1) + (cookie.endsWith('A') ? 'B' : 'A'), NOW],
      ['garbage', 'not-a-cookie', NOW],
      ['expired', cookie, NOW + MAX_AGE_SECONDS + 1],
      ['from the future', cookie, NOW - 120],
    ] as const) {
      const r = await resolve(new StubProvider(null), raw, at);
      expect(r.identity, why).toBeNull();
      expect(r.cookie, why).toEqual({ kind: 'stale' });
    }
  });

  it('is deleted, not merged, when it names the account it is presented on', async () => {
    const self = await mintCookie(signer, SUBJECT.startsWith('anon:') ? SUBJECT : ANON, NOW);
    const provider = new StubProvider({ ...signedIn, subject: ANON });
    const r = await resolve(provider, self);
    expect(r.merge).toBeNull();
    expect(r.cookie).toEqual({ kind: 'stale' });
  });

  it('is deleted for a signed-in request too when it is forged', async () => {
    const r = await resolve(new StubProvider(signedIn), 'not-a-cookie');
    expect(r.identity).toEqual(signedIn);
    expect(r.cookie).toEqual({ kind: 'stale' });
    expect(r.merge).toBeNull();
  });

  it('names the id to merge when it names a different account', async () => {
    const r = await resolve(new StubProvider(signedIn), await mintCookie(signer, OTHER, NOW));
    expect(r.merge).toBe(OTHER);
  });
});

describe('with anonymous accounts off', () => {
  it('a cookie is neither honoured nor deleted, because it cannot be judged', async () => {
    const cookie = await mintCookie(signer, ANON, NOW);
    const r = await resolve(new StubProvider(null), cookie, NOW, null);
    expect(r.identity).toBeNull();
    expect(r.cookie).toEqual({ kind: 'none' });
    expect(await readCookie(cookie, null, NOW)).toEqual({ stale: false });
  });

  it('a signed-in request still resolves and merges nothing', async () => {
    const r = await resolve(new StubProvider(signedIn), await mintCookie(signer, ANON, NOW), NOW, null);
    expect(r.identity).toEqual(signedIn);
    expect(r.merge).toBeNull();
    expect(r.cookie).toEqual({ kind: 'none' });
  });
});
