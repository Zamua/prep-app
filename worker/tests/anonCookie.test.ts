import { describe, expect, it, vi } from 'vitest';
import { COOKIE_NAME, MAX_AGE_SECONDS, REFRESH_AFTER_SECONDS, parseCookie, verifyCookie } from '../domain/anonCookie';
import { bytesToHex } from '../domain/base64';
import { httpDate, parseCookieHeader, quoteCookieValue, setCookie, unquoteCookieValue } from '../domain/cookies';
import { deleteCookieHeader, HmacSigner, mintCookie, resolveCookieSecret, setCookieHeader } from '../runtime/adapters/anonCookie';
import { hkdfSha256 } from '../runtime/adapters/hkdf';
import { pythonJson } from './pyoracle';

// The parity harness runs with this master key and no explicit cookie
// secret, so the corpus values below were signed with the HKDF of it.
const MASTER = '11'.repeat(32);
const CORPUS_COOKIE = 'v1.dWZyiO8f3Lr4uyP5QvgdZQ.1773500400.8K0b9kK4zvX9lzrnDSUDuA';
const CORPUS_REFRESHED = 'v1.dWZyiO8f3Lr4uyP5QvgdZQ.1776092401.0xkIqBaG9OEzb1mGp6XSWg';
const CORPUS_ID = 'anon:75667288ef1fdcbaf8bb23f942f81d65';
const PARITY_NOW = 1773500400;

const signerFor = async (env: Record<string, string>) => {
  const secret = await resolveCookieSecret(env);
  return secret ? new HmacSigner(secret) : null;
};

describe('the signing key', () => {
  it('is HKDF-SHA256 of the master key, byte for byte with Python', async () => {
    const derived = await hkdfSha256(Uint8Array.from(Buffer.from(MASTER, 'hex')), new TextEncoder().encode('prep-anon-cookie-v1'), 32);
    const python = pythonJson<{ key: string }>(
      `import json
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"prep-anon-cookie-v1").derive(bytes.fromhex("${MASTER}"))
print(json.dumps({"key": key.hex()}))`,
    );
    expect(bytesToHex(derived)).toBe(python.key);
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

describe('the cookie the parity corpus recorded', () => {
  const signer = () => signerFor({ PREP_KEY_ENCRYPTION_SECRET: MASTER });

  it('verifies, and names the id the snapshot answered with', async () => {
    const parsed = parseCookie(CORPUS_COOKIE)!;
    const verified = verifyCookie(parsed, await (await signer())!.sign(parsed.payload), PARITY_NOW);
    expect(verified).toEqual({ externalId: CORPUS_ID, issuedAt: PARITY_NOW });
  });

  it('re-mints to the exact value the refresh pair carries', async () => {
    const at = PARITY_NOW + REFRESH_AFTER_SECONDS + 1;
    expect(await mintCookie((await signer())!, CORPUS_ID, at)).toBe(CORPUS_REFRESHED);
  });

  it('rejects a flipped signature, a future iat and an expired one', async () => {
    const s = (await signer())!;
    const check = async (raw: string, now: number) => {
      const parsed = parseCookie(raw);
      return parsed && verifyCookie(parsed, await s.sign(parsed.payload), now);
    };
    expect(await check(CORPUS_COOKIE.slice(0, -1) + 'B', PARITY_NOW)).toBeNull();
    expect(await check('not-a-cookie', PARITY_NOW)).toBeNull();
    // The refreshed value replayed at the pinned clock is beyond the skew.
    expect(await check(CORPUS_REFRESHED, PARITY_NOW)).toBeNull();
    expect(await check(CORPUS_COOKIE, PARITY_NOW + MAX_AGE_SECONDS + 1)).toBeNull();
  });

  it('signs with the explicit secret when one is set, so the HKDF value stops verifying', async () => {
    const other = (await signerFor({ PREP_ANON_COOKIE_SECRET: '22'.repeat(32), PREP_KEY_ENCRYPTION_SECRET: MASTER }))!;
    const parsed = parseCookie(CORPUS_COOKIE)!;
    expect(verifyCookie(parsed, await other.sign(parsed.payload), PARITY_NOW)).toBeNull();
  });
});

describe('the Set-Cookie bytes', () => {
  it('are the ones Starlette sets, attributes in sorted order', () => {
    expect(setCookieHeader(CORPUS_COOKIE, true)).toBe(
      `prep_anon=${CORPUS_COOKIE}; HttpOnly; Max-Age=${MAX_AGE_SECONDS}; Path=/; SameSite=lax; Secure`,
    );
    expect(setCookieHeader(CORPUS_COOKIE, false)).toBe(`prep_anon=${CORPUS_COOKIE}; HttpOnly; Max-Age=${MAX_AGE_SECONDS}; Path=/; SameSite=lax`);
  });

  it('delete an empty quoted value with an expiry stamped now', () => {
    const at = new Date('2026-08-25T20:38:46Z');
    expect(deleteCookieHeader(at, true)).toBe('prep_anon=""; expires=Tue, 25 Aug 2026 20:38:46 GMT; HttpOnly; Max-Age=0; Path=/; SameSite=lax; Secure');
    expect(deleteCookieHeader(at, false)).toBe('prep_anon=""; expires=Tue, 25 Aug 2026 20:38:46 GMT; HttpOnly; Max-Age=0; Path=/; SameSite=lax');
  });

  it('format the HTTP date as http.cookies does', () => {
    const python = pythonJson<{ dates: string[] }>(
      `import json, time
stamps = [1774000726, 0, 1804000000, 946684800]
print(json.dumps({"dates": [time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(s)) for s in stamps]}))`,
    );
    expect([1774000726, 0, 1804000000, 946684800].map((s) => httpDate(new Date(s * 1000)))).toEqual(python.dates);
  });

  it('quote a value only when http.cookies would', () => {
    expect(quoteCookieValue(CORPUS_COOKIE)).toBe(CORPUS_COOKIE);
    expect(quoteCookieValue('')).toBe('""');
    expect(quoteCookieValue('a b')).toBe('"a b"');
    expect(quoteCookieValue('a"b\\c')).toBe('"a\\"b\\\\c"');
    expect(unquoteCookieValue(quoteCookieValue('a b'))).toBe('a b');
    expect(unquoteCookieValue(quoteCookieValue('a"b\\c'))).toBe('a"b\\c');
  });

  it('read back out of a Cookie header', () => {
    expect(parseCookieHeader(`${COOKIE_NAME}=${CORPUS_COOKIE}; __client_uat=1771000000; other=x`)).toEqual({
      prep_anon: CORPUS_COOKIE,
      __client_uat: '1771000000',
      other: 'x',
    });
    expect(parseCookieHeader(null)).toEqual({});
    expect(parseCookieHeader('novalue')).toEqual({});
  });

  it('name the same attributes Python prints for an arbitrary cookie', () => {
    const python = pythonJson<{ header: string }>(
      `import json
from http.cookies import SimpleCookie
c = SimpleCookie()
c["prep_or_pkce"] = "abc.def-ghi_"
m = c["prep_or_pkce"]
m["max-age"] = 600
m["path"] = "/"
m["samesite"] = "lax"
m["secure"] = True
m["httponly"] = True
print(json.dumps({"header": c.output(header="").strip()}))`,
    );
    expect(setCookie('prep_or_pkce', 'abc.def-ghi_', { maxAge: 600, path: '/', sameSite: 'lax', secure: true, httpOnly: true })).toBe(python.header);
  });
});
