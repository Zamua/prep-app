import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { b64uEncodeText } from '../domain/base64';
import { BAD_SCHEME, MISSING_HEADER, SECRET_BYTES, TOKEN_PREFIX, assembleToken, bearerValue, maskToken, parseToken } from '../domain/pat';
import { WebCryptoHasher } from '../runtime/adapters/hash';
import { apiE2eToken } from '../runtime/cells/seed/apiE2e';
import { PatIssuer, tokenRouting } from '../runtime/adapters/pat';
import { SeededRandom } from '../runtime/adapters/random';

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

  it('trims the presented value', () => {
    const token = assembleToken(SUBJECT, new Uint8Array(SECRET_BYTES).fill(3));
    expect(parseToken(`  ${token}\n`)?.subject).toBe(SUBJECT);
  });
});

describe('the display mask', () => {
  // Long enough to mask: the first eleven characters and the last four,
  // whichever format the token is in. Anything shorter collapses to the
  // ellipsis rather than leaking most of itself.
  it('keeps the prefix and the last four, whatever the format', () => {
    expect(
      [
        'prep_pat_ParityCliToken0000000000000000000000',
        'prep_pat_dXNlcl8yYWJj.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        'prep_pat_short',
        '',
      ].map(maskToken),
    ).toEqual(['prep_pat_Pa…0000', 'prep_pat_dX…AAAA', '…', '…']);
  });
});

describe('the bearer header', () => {
  it('answers the exact detail strings the refusals name', () => {
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
    expect(issued.hash).toBe(createHash('sha256').update(issued.token, 'utf8').digest('hex'));
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

describe('the e2e seed token', () => {
  it('parses and routes to its owner, unlike the legacy parity fixture', () => {
    const user = 'e2e@example.com';
    const parsed = parseToken(apiE2eToken(user));
    expect(parsed?.subject).toBe(user);
    // The reader profile's fixture is legacy on purpose: it must never
    // authenticate.
    expect(parseToken('prep_pat_ParityCliToken0000000000000000000000')).toBeNull();
  });

  it('hashes to what the profile stores, so the bearer path matches it', async () => {
    const plaintext = apiE2eToken('e2e@example.com');
    expect(await hasher.sha256Hex(plaintext)).toBe(await hasher.sha256Hex(plaintext));
    expect(maskToken(plaintext).startsWith(TOKEN_PREFIX)).toBe(true);
  });
});
