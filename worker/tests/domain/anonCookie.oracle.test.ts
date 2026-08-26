import { describe, expect, it } from 'vitest';
import { createHmac } from 'node:crypto';
import {
  BadExternalId,
  COOKIE_NAME,
  EXTERNAL_ID_PREFIX,
  FUTURE_SKEW_SECONDS,
  ID_BYTES,
  MAX_AGE_SECONDS,
  REFRESH_AFTER_SECONDS,
  SECRET_BYTES,
  SIG_BYTES,
  assembleCookie,
  b64u,
  b64uDecode,
  cookiePayload,
  externalIdFromBytes,
  idBytes,
  needsRefresh,
  parseCookie,
  verifyCookie,
  type AnonCookie,
} from '../../domain/anonCookie';
import { pythonJson } from '../pyoracle';

const SECRET = Buffer.from('22'.repeat(32), 'hex');
const sign = (payload: string): Uint8Array => new Uint8Array(createHmac('sha256', SECRET).update(payload, 'ascii').digest());

const NOW = 1773500400;
const ID = 'anon:' + 'ab'.repeat(16);
const ID2 = 'anon:0123456789abcdef0123456789abcdef';

function mint(externalId: string, issuedAt: number): string {
  const payload = cookiePayload(externalId, issuedAt);
  return assembleCookie(payload, sign(payload));
}

function verify(raw: string, now: number): { external_id: string; issued_at: number } | null {
  const parsed = parseCookie(raw);
  if (parsed === null) return null;
  const ok = verifyCookie(parsed, sign(parsed.payload), now);
  return ok && { external_id: ok.externalId, issued_at: ok.issuedAt };
}

/** A raw value signed over its own first three parts, whatever they hold. */
const forged = (idPart: string, iatPart: string, version = 'v1'): string => {
  const payload = `${version}.${idPart}.${iatPart}`;
  return `${payload}.${b64u(sign(payload).subarray(0, SIG_BYTES))}`;
};

const good = mint(ID, NOW);
const [, idPart, , sigPart] = good.split('.') as [string, string, string, string];
const flip = (s: string) => s.slice(0, -1) + (s.endsWith('A') ? 'B' : 'A');

const MINT: [string, number][] = [
  [ID, NOW],
  [ID2, 0],
  [ID, NOW + 0.9],
  ['anon:' + 'AB'.repeat(16), NOW],
];

const VERIFY: [string, number][] = [
  [good, NOW],
  [good, NOW + MAX_AGE_SECONDS],
  [good, NOW + MAX_AGE_SECONDS + 1],
  [good, NOW - FUTURE_SKEW_SECONDS],
  [good, NOW - FUTURE_SKEW_SECONDS - 1],
  [mint(ID2, NOW - 1000), NOW],
  [`${good.slice(0, -sigPart.length)}${flip(sigPart)}`, NOW],
  [good.slice(0, -1), NOW],
  [`${good}A`, NOW],
  [`v1.${idPart}.${NOW}.`, NOW],
  [`v1.${idPart}.${NOW}`, NOW],
  [`${good}.x`, NOW],
  [`v1.${flip(idPart)}.${NOW}.${sigPart}`, NOW],
  [forged(idPart, String(NOW), 'v2'), NOW],
  [forged(idPart, String(NOW), 'V1'), NOW],
  [forged(idPart, String(NOW), ''), NOW],
  [forged(idPart.slice(0, 20), String(NOW)), NOW],
  [forged(`${idPart}A`, String(NOW)), NOW],
  [forged(`${idPart}AA`, String(NOW)), NOW],
  [forged('', String(NOW)), NOW],
  [forged(idPart, ` ${NOW} `), NOW],
  [forged(idPart, `+${NOW}`), NOW],
  [forged(idPart, `-${NOW}`), NOW],
  [forged(idPart, `00${NOW}`), NOW],
  [forged(idPart, `${String(NOW).slice(0, 3)}_${String(NOW).slice(3)}`), NOW],
  [forged(idPart, `${NOW}_`), NOW],
  [forged(idPart, `1__2`), NOW],
  [forged(idPart, `${NOW}abc`), NOW],
  [forged(idPart, `${NOW}.0`), NOW],
  [forged(idPart, '1e9'), NOW],
  [forged(idPart, ''), NOW],
  [forged(idPart, ' '), NOW],
  [forged(idPart, `\t${NOW}\n`), NOW],
  [forged(idPart, `\x0b${NOW}`), NOW],
  [forged(idPart, `\x1c${NOW}`), NOW],
  [forged(idPart, '9'.repeat(40)), NOW],
  [forged(idPart, '-' + '9'.repeat(40)), NOW],
  [`v1.${idPart}.${NOW}.${sigPart.slice(0, -1)}é`, NOW],
  [`v1.${idPart}.١${NOW}.${sigPart}`, NOW],
  ['', NOW],
  ['v1', NOW],
  ['...', NOW],
  ['v1...', NOW],
];

const REFRESH: [string, number, number][] = [
  [ID, NOW, NOW + REFRESH_AFTER_SECONDS],
  [ID, NOW, NOW + REFRESH_AFTER_SECONDS + 1],
  [ID, NOW, NOW],
  [ID, NOW + 100, NOW],
];

interface Oracle {
  mint: string[];
  verify: ({ external_id: string; issued_at: number } | null)[];
  refresh: boolean[];
}

const payload = Buffer.from(JSON.stringify({ mint: MINT, verify: VERIFY, refresh: REFRESH })).toString('base64');
const oracle = pythonJson<Oracle>(`
import base64, json, os
os.environ["PREP_ANON_COOKIE_SECRET"] = "22" * 32
from prep.auth.anon_cookie import AnonCookie, mint_cookie, needs_refresh, verify_cookie
c = json.loads(base64.b64decode("${payload}"))
verify = []
for raw, now in c["verify"]:
    r = verify_cookie(raw, now=now)
    verify.append(None if r is None else {"external_id": r.external_id, "issued_at": r.issued_at})
print(json.dumps({
  "mint": [mint_cookie(eid, issued_at=iat) for eid, iat in c["mint"]],
  "verify": verify,
  "refresh": [needs_refresh(AnonCookie(eid, iat), now=now) for eid, iat, now in c["refresh"]],
}))
`);

describe('anon cookie matches the reference', () => {
  it('mint_cookie', () => {
    expect(MINT.map(([eid, iat]) => mint(eid, iat))).toEqual(oracle.mint);
  });

  it('verify_cookie, every rejection branch included', () => {
    expect(VERIFY.map(([raw, now]) => verify(raw, now))).toEqual(oracle.verify);
    expect(oracle.verify.filter((x) => x === null).length).toBeGreaterThanOrEqual(25);
    expect(oracle.verify.filter((x) => x !== null).length).toBeGreaterThanOrEqual(8);
  });

  it('needs_refresh', () => {
    const got = REFRESH.map(([eid, iat, now]) => needsRefresh({ externalId: eid, issuedAt: iat } satisfies AnonCookie, now));
    expect(got).toEqual(oracle.refresh);
    expect(oracle.refresh).toEqual([false, true, false, false]);
  });
});

describe('anon cookie shapes', () => {
  it('pins the constants', () => {
    expect([COOKIE_NAME, EXTERNAL_ID_PREFIX, ID_BYTES, SIG_BYTES, SECRET_BYTES]).toEqual(['prep_anon', 'anon:', 16, 16, 32]);
    expect([MAX_AGE_SECONDS, REFRESH_AFTER_SECONDS, FUTURE_SKEW_SECONDS]).toEqual([15552000, 2592000, 60]);
  });

  it('external ids round-trip through bytes', () => {
    expect(externalIdFromBytes(idBytes(ID))).toBe(ID);
    expect(idBytes('anon:' + 'AB'.repeat(16))).toEqual(idBytes(ID));
    expect(() => idBytes('user:' + 'ab'.repeat(16))).toThrow(BadExternalId);
    expect(() => idBytes('anon:' + 'ab'.repeat(15))).toThrow(BadExternalId);
    expect(() => idBytes('anon:' + 'zz'.repeat(16))).toThrow(BadExternalId);
  });

  it('parseCookie keeps the payload bytes the MAC covers', () => {
    const parsed = parseCookie(forged(idPart, ` 12 `))!;
    expect(parsed.payload).toBe(`v1.${idPart}. 12 `);
    expect(parsed.issuedAt).toBe(12);
  });

  it('the tag compare rejects a MAC of the wrong length', () => {
    const parsed = parseCookie(good)!;
    expect(verifyCookie(parsed, sign(parsed.payload).subarray(0, 15), NOW)).toBeNull();
    expect(verifyCookie(parsed, sign(parsed.payload), NOW)).not.toBeNull();
  });

  // Accepted divergences: Python's base64 decoder tolerates padding and
  // discards characters outside the alphabet.
  it('base64url decoding is strict', () => {
    expect(b64u(new Uint8Array([0, 1, 2, 250, 251, 252]))).toBe('AAEC-vv8');
    expect(b64uDecode('AAEC-vv8', 6)).toEqual(new Uint8Array([0, 1, 2, 250, 251, 252]));
    expect(b64uDecode('AAEC-vv8', 5)).toBeNull();
    expect(b64uDecode(`${idPart}==`, ID_BYTES)).toBeNull();
    expect(b64uDecode(`${idPart.slice(0, 10)}!${idPart.slice(10)}`, ID_BYTES)).toBeNull();
    expect(b64uDecode('AAEC+vv8', 6)).toBeNull();
    expect(b64uDecode('AAEC/vv8', 6)).toBeNull();
    expect(verify(forged(`${idPart}==`, String(NOW)), NOW)).toBeNull();
  });
});
