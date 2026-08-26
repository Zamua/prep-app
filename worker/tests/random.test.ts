import { describe, expect, it } from 'vitest';
import { SLUG_ALPHABET } from '../app/entities.js';
import { hex, ParitySessionIds, RandomSessionIds, SeededRandom, WebCryptoRandom } from '../runtime/adapters/random.js';
import { pythonJson } from './pyoracle.js';

// The harness's SeededSecrets over random.Random(seed): the draws the
// instant, merge and token paths make, in order.
const PY = `
import json, random
def draws(seed):
    r = random.Random(seed)
    out = {"bytes16": [r.getrandbits(8) for _ in range(16)]}
    out["choices"] = [r.choice("${SLUG_ALPHABET}") for _ in range(24)]
    out["bytes32"] = [r.getrandbits(8) for _ in range(32)]
    out["choice_small"] = [r.choice([0, 1, 2]) for _ in range(10)]
    out["bits32"] = [r.getrandbits(32) for _ in range(4)]
    return out
print(json.dumps({"s0": draws(20260314), "s1": draws(20260315), "big": draws(2**40 + 7), "zero": draws(0)}))
`;

interface Draws {
  bytes16: number[];
  choices: string[];
  bytes32: number[];
  choice_small: number[];
  bits32: number[];
}

function draws(seed: number): Draws {
  const r = new SeededRandom(seed);
  const alphabet = Array.from(SLUG_ALPHABET);
  return {
    bytes16: Array.from(r.bytes(16)),
    choices: Array.from({ length: 24 }, () => r.choice(alphabet)),
    bytes32: Array.from(r.bytes(32)),
    choice_small: Array.from({ length: 10 }, () => r.choice([0, 1, 2])),
    bits32: Array.from({ length: 4 }, () => r.getrandbits(32)),
  };
}

describe('SeededRandom is random.Random(int)', () => {
  const py = pythonJson<Record<'s0' | 's1' | 'big' | 'zero', Draws>>(PY);

  it.each([
    ['the parity seed', 20260314, 's0'],
    ['the next seed', 20260315, 's1'],
    ['a seed over 32 bits', 2 ** 40 + 7, 'big'],
    ['seed zero', 0, 'zero'],
  ] as const)('matches Python on %s', (_name, seed, key) => {
    expect(draws(seed)).toEqual(py[key]);
  });

  it('refuses an empty choice and out-of-range bit counts', () => {
    const r = new SeededRandom(1);
    expect(() => r.choice([])).toThrow(RangeError);
    expect(() => r.getrandbits(33)).toThrow(RangeError);
  });
});

describe('WebCryptoRandom', () => {
  it('draws bytes and choices from the alphabet', () => {
    const r = new WebCryptoRandom();
    expect(r.bytes(16)).toHaveLength(16);
    for (let i = 0; i < 50; i++) expect(SLUG_ALPHABET).toContain(r.choice(Array.from(SLUG_ALPHABET)));
    expect(r.choice(['only'])).toBe('only');
    expect(() => r.choice([])).toThrow(RangeError);
  });
});

describe('session ids', () => {
  it('parity ids are sha1("parity-session-<n>")[:16] over a counter', async () => {
    let n = 0;
    const ids = new ParitySessionIds({ get: async () => n, set: async (v) => void (n = v) });
    expect(await ids.next()).toBe('81426e386f04220d');
    expect(await ids.next()).toBe('951ddc296f6f0ff5');
    expect(n).toBe(2);
    n = 0;
    expect(await ids.next()).toBe('81426e386f04220d');
  });

  it('production ids are token_hex(8)', async () => {
    const id = await new RandomSessionIds(new WebCryptoRandom()).next();
    expect(id).toMatch(/^[0-9a-f]{16}$/);
    expect(hex(new Uint8Array([0, 255, 16]))).toBe('00ff10');
  });
});
