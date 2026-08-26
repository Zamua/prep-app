import { describe, expect, it } from 'vitest';
import { b64uEncodeText } from '../domain/base64';
import { BAD_SCHEME, MISSING_HEADER, SECRET_BYTES, TOKEN_PREFIX, assembleToken, bearerValue, maskToken, parseToken } from '../domain/pat';
import { WebCryptoHasher } from '../runtime/adapters/hash';
import { PatIssuer, tokenRouting } from '../runtime/adapters/pat';
import { SeededRandom } from '../runtime/adapters/random';
import { pythonJson } from './pyoracle';

const hasher = new WebCryptoHasher();
const SUBJECT = 'user_2abcDEF';

describe('the token format', () => {
  it('names its owner and keeps the secret half separate', () => {
    const token = assembleToken(SUBJECT, new Uint8Array(SECRET_BYTES).fill(7));
    expect(token.startsWith(`${TOKEN_PREFIX}${b64uEncodeText(SUBJECT)}.`)).toBe(true);
    expect(parseToken(token)).toEqual({ subject: SUBJECT, secret: token.split('.')[1] });
  });

  it('carries a subject with an @ and non-ASCII through unchanged', () => {
    for (const subject of ['parity@example.com', 'user+tag@example.com', 'zoë@example.com', 'anon:' + 'ab'.repeat(16)]) {
      expect(parseToken(assembleToken(subject, new Uint8Array(SECRET_BYTES)))?.subject).toBe(subject);
    }
  });

  it('parses nothing out of a legacy or malformed value', () => {
    const legacy = `${TOKEN_PREFIX}Pa5rTyToKeN0000000000000000000000000000000`;
    expect(parseToken(legacy)).toBeNull();
    for (const bad of ['', '   ', 'prep_pat_', 'prep_pat_.abc', 'prep_pat_abc.', 'prep_pat_a.b.c', 'prep_pat_!!.abc', 'sk-ant-api03-x', null, undefined]) {
      expect(parseToken(bad), String(bad)).toBeNull();
    }
  });

  it('trims the presented value, the way the Python lookup does', () => {
    const token = assembleToken(SUBJECT, new Uint8Array(SECRET_BYTES).fill(3));
    expect(parseToken(`  ${token}\n`)?.subject).toBe(SUBJECT);
  });
});

describe('the display mask', () => {
  it('matches the Python `_mask` on both formats', () => {
    const cases = [
      'prep_pat_ParityCliToken0000000000000000000000',
      'prep_pat_dXNlcl8yYWJj.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      'prep_pat_short',
      '',
    ];
    const python = pythonJson<{ masks: string[] }>(
      `import json
from prep.api.repo import _mask
print(json.dumps({"masks": [_mask(t) for t in ${JSON.stringify(cases)}]}))`,
    );
    expect(cases.map(maskToken)).toEqual(python.masks);
  });

  it('keeps the reader seed token recognisable in the settings golden', () => {
    expect(maskToken('prep_pat_ParityCliToken0000000000000000000000')).toBe('prep_pat_Pa…0000');
  });
});

describe('the bearer header', () => {
  it('answers the exact detail strings Python answers', () => {
    expect(bearerValue(null)).toEqual({ refusal: MISSING_HEADER });
    expect(bearerValue('')).toEqual({ refusal: MISSING_HEADER });
    expect(bearerValue('Bearer')).toEqual({ refusal: BAD_SCHEME });
    expect(bearerValue('Bearer ')).toEqual({ refusal: BAD_SCHEME });
    expect(bearerValue('Basic abc')).toEqual({ refusal: BAD_SCHEME });
    expect(bearerValue('bearer abc')).toEqual({ token: 'abc' });
    expect(bearerValue('BEARER abc def')).toEqual({ token: 'abc def' });
  });
});

describe('issuing and routing', () => {
  it('hashes the whole token with SHA-256, as the repo stores it', async () => {
    const issuer = new PatIssuer(new SeededRandom(20260314), hasher);
    const issued = await issuer.issue(SUBJECT);
    const python = pythonJson<{ hash: string }>(
      `import json, hashlib
print(json.dumps({"hash": hashlib.sha256(${JSON.stringify(issued.token)}.encode("utf-8")).hexdigest()}))`,
    );
    expect(issued.hash).toBe(python.hash);
    expect(issued.mask).toBe(maskToken(issued.token));
    expect(await tokenRouting(hasher, issued.token)).toEqual({ subject: SUBJECT, hash: issued.hash });
  });

  it('draws a fresh secret every time', async () => {
    const issuer = new PatIssuer(new SeededRandom(20260314), hasher);
    const seen = new Set<string>();
    for (let i = 0; i < 8; i++) seen.add((await issuer.issue(SUBJECT)).token);
    expect(seen.size).toBe(8);
  });

  it('routes nothing for a legacy token, which reads as unknown', async () => {
    expect(await tokenRouting(hasher, 'prep_pat_LegacyTokenWithNoDot')).toBeNull();
  });
});
