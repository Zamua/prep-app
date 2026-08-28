import { describe, expect, it, vi } from 'vitest';
import { COOKIE_NAME, MAX_AGE_SECONDS, REFRESH_AFTER_SECONDS, parseCookie, verifyCookie } from '../domain/anonCookie';
import { bytesToHex } from '../domain/base64';
import { httpDate, parseCookieHeader, quoteCookieValue, setCookie, unquoteCookieValue } from '../domain/cookies';
import { deleteCookieHeader, HmacSigner, mintCookie, resolveCookieSecret, setCookieHeader } from '../runtime/adapters/anonCookie';
import { hkdfSha256 } from '../runtime/adapters/hkdf';

// A cookie minted under this master key with no explicit cookie secret, so
// the signature is over the HKDF of it. Browsers hold values like this one
// for six months: the key derivation and the mint have to keep producing
// exactly these bytes or every anonymous session on the internet is dropped.
const MASTER = '11'.repeat(32);
const ISSUED_COOKIE = 'v1.dWZyiO8f3Lr4uyP5QvgdZQ.1773500400.8K0b9kK4zvX9lzrnDSUDuA';
const ISSUED_REFRESHED = 'v1.dWZyiO8f3Lr4uyP5QvgdZQ.1776092401.0xkIqBaG9OEzb1mGp6XSWg';
const ISSUED_ID = 'anon:75667288ef1fdcbaf8bb23f942f81d65';
const ISSUED_AT = 1773500400;

const signerFor = async (env: Record<string, string>) => {
  const secret = await resolveCookieSecret(env);
  return secret ? new HmacSigner(secret) : null;
};

describe('the signing key', () => {
  // RFC 5869 with an empty salt and this info string. The value is what
  // signs every live anonymous cookie, so it is pinned rather than derived
  // twice by the same code.
  it('is HKDF-SHA256 of the master key under a fixed info string', async () => {
    const derived = await hkdfSha256(Uint8Array.from(Buffer.from(MASTER, 'hex')), new TextEncoder().encode('prep-anon-cookie-v1'), 32);
    expect(bytesToHex(derived)).toBe('a60ae7dd318a3add48864ab099e2601d152011bdfbf0d3a824ff9e95c3bbaef7');
  });

  it('prefers an explicit 32-hex-byte secret and refuses anything else', async () => {
    const explicit = await resolveCookieSecret({ PREP_ANON_COOKIE_SECRET: '22'.repeat(32), PREP_KEY_ENCRYPTION_SECRET: MASTER });
    expect(bytesToHex(explicit!)).toBe('22'.repeat(32));
    const warn = vi.fn();
    expect(await resolveCookieSecret({ PREP_ANON_COOKIE_SECRET: 'nonsense', PREP_KEY_ENCRYPTION_SECRET: MASTER }, warn)).toBeNull();
    expect(await resolveCookieSecret({ PREP_ANON_COOKIE_SECRET: '22'.repeat(16) }, warn)).toBeNull();
    expect(warn).toHaveBeenCalledTimes(2);
  });

  it('is null with no secret at all, which turns anonymous accounts off', async () => {
    expect(await resolveCookieSecret({})).toBeNull();
    expect(await resolveCookieSecret({ PREP_KEY_ENCRYPTION_SECRET: 'not-hex' })).toBeNull();
    expect(await resolveCookieSecret({ PREP_KEY_ENCRYPTION_SECRET: '11'.repeat(16) })).toBeNull();
  });
});

describe('a cookie already in a browser', () => {
  const signer = () => signerFor({ PREP_KEY_ENCRYPTION_SECRET: MASTER });

  it('verifies, and names the id it was minted for', async () => {
    const parsed = parseCookie(ISSUED_COOKIE)!;
    const verified = verifyCookie(parsed, await (await signer())!.sign(parsed.payload), ISSUED_AT);
    expect(verified).toEqual({ externalId: ISSUED_ID, issuedAt: ISSUED_AT });
  });

  it('re-mints to the exact value a refresh has to produce', async () => {
    const at = ISSUED_AT + REFRESH_AFTER_SECONDS + 1;
    expect(await mintCookie((await signer())!, ISSUED_ID, at)).toBe(ISSUED_REFRESHED);
  });

  it('rejects a flipped signature, a future iat and an expired one', async () => {
    const s = (await signer())!;
    const check = async (raw: string, now: number) => {
      const parsed = parseCookie(raw);
      return parsed && verifyCookie(parsed, await s.sign(parsed.payload), now);
    };
    expect(await check(ISSUED_COOKIE.slice(0, -1) + 'B', ISSUED_AT)).toBeNull();
    expect(await check('not-a-cookie', ISSUED_AT)).toBeNull();
    // The refreshed value replayed at the earlier clock is beyond the skew.
    expect(await check(ISSUED_REFRESHED, ISSUED_AT)).toBeNull();
    expect(await check(ISSUED_COOKIE, ISSUED_AT + MAX_AGE_SECONDS + 1)).toBeNull();
  });

  it('signs with the explicit secret when one is set, so the HKDF value stops verifying', async () => {
    const other = (await signerFor({ PREP_ANON_COOKIE_SECRET: '22'.repeat(32), PREP_KEY_ENCRYPTION_SECRET: MASTER }))!;
    const parsed = parseCookie(ISSUED_COOKIE)!;
    expect(verifyCookie(parsed, await other.sign(parsed.payload), ISSUED_AT)).toBeNull();
  });
});

describe('the Set-Cookie bytes', () => {
  it('name the attributes in sorted order', () => {
    expect(setCookieHeader(ISSUED_COOKIE, true)).toBe(
      `prep_anon=${ISSUED_COOKIE}; HttpOnly; Max-Age=${MAX_AGE_SECONDS}; Path=/; SameSite=lax; Secure`,
    );
    expect(setCookieHeader(ISSUED_COOKIE, false)).toBe(`prep_anon=${ISSUED_COOKIE}; HttpOnly; Max-Age=${MAX_AGE_SECONDS}; Path=/; SameSite=lax`);
  });

  it('delete an empty quoted value with an expiry stamped now', () => {
    const at = new Date('2026-08-25T20:38:46Z');
    expect(deleteCookieHeader(at, true)).toBe('prep_anon=""; expires=Tue, 25 Aug 2026 20:38:46 GMT; HttpOnly; Max-Age=0; Path=/; SameSite=lax; Secure');
    expect(deleteCookieHeader(at, false)).toBe('prep_anon=""; expires=Tue, 25 Aug 2026 20:38:46 GMT; HttpOnly; Max-Age=0; Path=/; SameSite=lax');
  });

  // RFC 7231 IMF-fixdate, which is what an expiry attribute has to carry.
  it('format the HTTP date', () => {
    expect([1774000726, 0, 1804000000, 946684800].map((s) => httpDate(new Date(s * 1000)))).toEqual([
      'Fri, 20 Mar 2026 09:58:46 GMT',
      'Thu, 01 Jan 1970 00:00:00 GMT',
      'Tue, 02 Mar 2027 15:06:40 GMT',
      'Sat, 01 Jan 2000 00:00:00 GMT',
    ]);
  });

  it('quote a value only when it holds a character the grammar forbids', () => {
    expect(quoteCookieValue(ISSUED_COOKIE)).toBe(ISSUED_COOKIE);
    expect(quoteCookieValue('')).toBe('""');
    expect(quoteCookieValue('a b')).toBe('"a b"');
    expect(quoteCookieValue('a"b\\c')).toBe('"a\\"b\\\\c"');
    expect(unquoteCookieValue(quoteCookieValue('a b'))).toBe('a b');
    expect(unquoteCookieValue(quoteCookieValue('a"b\\c'))).toBe('a"b\\c');
  });

  it('read back out of a Cookie header', () => {
    expect(parseCookieHeader(`${COOKIE_NAME}=${ISSUED_COOKIE}; __client_uat=1771000000; other=x`)).toEqual({
      prep_anon: ISSUED_COOKIE,
      __client_uat: '1771000000',
      other: 'x',
    });
    expect(parseCookieHeader(null)).toEqual({});
    expect(parseCookieHeader('novalue')).toEqual({});
  });

  it('write an arbitrary cookie with its attributes in sorted order', () => {
    expect(setCookie('prep_or_pkce', 'abc.def-ghi_', { maxAge: 600, path: '/', sameSite: 'lax', secure: true, httpOnly: true })).toBe(
      'prep_or_pkce=abc.def-ghi_; HttpOnly; Max-Age=600; Path=/; SameSite=lax; Secure',
    );
  });
});
