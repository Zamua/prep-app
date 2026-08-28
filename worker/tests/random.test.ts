import { describe, expect, it } from 'vitest';
import { SLUG_ALPHABET } from '../app/entities.js';
import { hex, SeededSessionIds, RandomSessionIds, SeededRandom, WebCryptoRandom } from '../runtime/adapters/random.js';

/** The draws the instant, merge and token paths make, in order. A seeded
 * node has to reproduce them exactly: an e2e run seeds a cell and then
 * addresses the ids it expects that seed to have produced. */
interface Draws {
  bytes16: number[];
  choices: string;
  bytes32: number[];
  choiceSmall: number[];
  bits32: number[];
}

const EXPECTED: Record<'s0' | 'zero', Draws> = {
  s0: {
    bytes16: [108, 194, 112, 81, 239, 245, 108, 255, 208, 3, 72, 208, 197, 92, 229, 236],
    choices: "z736hish3mgazg67f3wxv9rz",
    bytes32: [238, 185, 39, 22, 198, 9, 202, 212, 165, 217, 24, 250, 38, 182, 109, 210, 125, 6, 211, 239, 252, 1, 40, 215, 92, 108, 96, 30, 78, 180, 144, 164],
    choiceSmall: [0, 1, 2, 1, 2, 0, 0, 0, 2, 0],
    bits32: [1378762811, 2660071704, 3765794545, 2941058209],
  },
  zero: {
    bytes16: [216, 98, 194, 227, 107, 10, 66, 247, 130, 124, 103, 235, 200, 212, 77, 247],
    choices: "8ypiuigsjvgex8gy5wp86sda",
    bytes32: [23, 184, 215, 102, 181, 211, 200, 171, 160, 0, 156, 126, 211, 222, 85, 62, 186, 83, 180, 222, 16, 48, 234, 145, 56, 61, 205, 247, 36, 205, 139, 114],
    choiceSmall: [0, 0, 1, 2, 1, 0, 1, 2, 1, 2],
    bits32: [536057929, 2351240810, 1429152570, 3498108526],
  },
};

function draws(seed: number): Draws {
  const r = new SeededRandom(seed);
  const alphabet = Array.from(SLUG_ALPHABET);
  return {
    bytes16: Array.from(r.bytes(16)),
    choices: Array.from({ length: 24 }, () => r.choice(alphabet)).join(''),
    bytes32: Array.from(r.bytes(32)),
    choiceSmall: Array.from({ length: 10 }, () => r.choice([0, 1, 2])),
    bits32: Array.from({ length: 4 }, () => r.getrandbits(32)),
  };
}

describe('SeededRandom', () => {
  it.each([
    ['the seed profiles use', 20260314, 's0'],
    ['seed zero', 0, 'zero'],
  ] as const)('draws the same sequence for %s', (_name, seed, key) => {
    expect(draws(seed)).toEqual(EXPECTED[key]);
  });

  it('gives two seeds two sequences, whatever their width', () => {
    expect(draws(20260315)).not.toEqual(draws(20260314));
    expect(draws(2 ** 40 + 7)).not.toEqual(draws(7));
    expect(draws(2 ** 40 + 7)).toEqual(draws(2 ** 40 + 7));
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
  it('seeded ids are sha1("seed-session-<n>")[:16] over a counter', async () => {
    let n = 0;
    const ids = new SeededSessionIds({ get: async () => n, set: async (v) => void (n = v) });
    expect(await ids.next()).toBe('06d904444c991b8d');
    expect(await ids.next()).toBe('e08536edd0fb14f4');
    expect(n).toBe(2);
    n = 0;
    expect(await ids.next()).toBe('06d904444c991b8d');
  });

  it('production ids are token_hex(8)', async () => {
    const id = await new RandomSessionIds(new WebCryptoRandom()).next();
    expect(id).toMatch(/^[0-9a-f]{16}$/);
    expect(hex(new Uint8Array([0, 255, 16]))).toBe('00ff10');
  });
});
