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
} from '../../domain/anonCookie';

const SECRET = Buffer.from('22'.repeat(32), 'hex');
const sign = (payload: string): Uint8Array => new Uint8Array(createHmac('sha256', SECRET).update(payload, 'ascii').digest());

const NOW = 1773500400;
const ID = 'anon:' + 'ab'.repeat(16);

function mint(externalId: string, issuedAt: number): string {
  const payload = cookiePayload(externalId, issuedAt);
  return assembleCookie(payload, sign(payload));
}

/** A raw value signed over its own first three parts, whatever they hold. */
const forged = (idPart: string, iatPart: string, version = 'v1'): string => {
  const payload = `${version}.${idPart}.${iatPart}`;
  return `${payload}.${b64u(sign(payload).subarray(0, SIG_BYTES))}`;
};

const good = mint(ID, NOW);
const [, idPart, , sigPart] = good.split('.') as [string, string, string, string];
const flip = (s: string) => s.slice(0, -1) + (s.endsWith('A') ? 'B' : 'A');

const verify = (raw: string, now: number): { externalId: string; issuedAt: number } | null => {
  const parsed = parseCookie(raw);
  return parsed === null ? null : verifyCookie(parsed, sign(parsed.payload), now);
};

describe('the cookie value', () => {
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

  // Padding and any character outside the alphabet are refused rather than
  // discarded, so one id has exactly one spelling.
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

describe('verifyCookie', () => {
  const ID2 = 'anon:0123456789abcdef0123456789abcdef';

  it('accepts a genuine value inside its window', () => {
    expect(verify(good, NOW)).toEqual({ externalId: ID, issuedAt: NOW });
    expect(verify(good, NOW + MAX_AGE_SECONDS)).not.toBeNull();
    expect(verify(good, NOW - FUTURE_SKEW_SECONDS)).not.toBeNull();
    expect(verify(mint(ID2, NOW - 1000), NOW)).toEqual({ externalId: ID2, issuedAt: NOW - 1000 });
  });

  it('refuses one past its age and one issued beyond the skew', () => {
    expect(verify(good, NOW + MAX_AGE_SECONDS + 1)).toBeNull();
    expect(verify(good, NOW - FUTURE_SKEW_SECONDS - 1)).toBeNull();
  });

  it('refuses a tampered signature, id or shape', () => {
    for (const raw of [
      `${good.slice(0, -sigPart.length)}${flip(sigPart)}`,
      good.slice(0, -1),
      `${good}A`,
      `v1.${idPart}.${NOW}.`,
      `v1.${idPart}.${NOW}`,
      `${good}.x`,
      `v1.${flip(idPart)}.${NOW}.${sigPart}`,
      `v1.${idPart}.${NOW}.${sigPart.slice(0, -1)}\u00e9`,
      `v1.${idPart}.\u0661${NOW}.${sigPart}`,
      '',
      'v1',
      '...',
      'v1...',
    ]) {
      expect(verify(raw, NOW), JSON.stringify(raw)).toBeNull();
    }
  });

  // Each of these carries a signature over its own payload, so only the
  // parse can refuse it.
  it('refuses a self-signed value the parse cannot read', () => {
    for (const [idPartValue, iatPart, version] of [
      [idPart, String(NOW), 'v2'],
      [idPart, String(NOW), 'V1'],
      [idPart, String(NOW), ''],
      [idPart.slice(0, 20), String(NOW), 'v1'],
      [`${idPart}A`, String(NOW), 'v1'],
      [`${idPart}AA`, String(NOW), 'v1'],
      ['', String(NOW), 'v1'],
      [idPart, `-${NOW}`, 'v1'],
      [idPart, `${NOW}_`, 'v1'],
      [idPart, '1__2', 'v1'],
      [idPart, `${NOW}abc`, 'v1'],
      [idPart, `${NOW}.0`, 'v1'],
      [idPart, '1e9', 'v1'],
      [idPart, '', 'v1'],
      [idPart, ' ', 'v1'],
      [idPart, `\x1c${NOW}`, 'v1'],
      [idPart, '9'.repeat(40), 'v1'],
      [idPart, '-' + '9'.repeat(40), 'v1'],
      [`${idPart}==`, String(NOW), 'v1'],
    ] as const) {
      const raw = forged(idPartValue, iatPart, version);
      expect(verify(raw, NOW), raw).toBeNull();
    }
  });

  // Alternate spellings of the same instant that the timestamp parse
  // accepts. Each sits inside the signed payload, so producing one costs
  // the key: they are not second values an attacker can reach.
  it('reads the tolerated timestamp spellings as the same instant', () => {
    for (const iat of [` ${NOW} `, `\t${NOW}\n`, `\x0b${NOW}`, `+${NOW}`, `00${NOW}`, '1773_500_400']) {
      expect(verify(forged(idPart, iat), NOW), JSON.stringify(iat)).toEqual({ externalId: ID, issuedAt: NOW });
    }
  });

  it('needsRefresh turns over exactly one interval after issue', () => {
    expect(
      ([
        [ID, NOW, NOW + REFRESH_AFTER_SECONDS],
        [ID, NOW, NOW + REFRESH_AFTER_SECONDS + 1],
        [ID, NOW, NOW],
        [ID, NOW + 100, NOW],
      ] as const).map(([eid, iat, now]) => needsRefresh({ externalId: eid, issuedAt: iat }, now)),
    ).toEqual([false, true, false, false]);
  });
});
