// Randomness behind the `Random` and `SessionIds` ports. Production draws
// from WebCrypto; test mode draws from a seeded MT19937 so slugs, ids and
// tokens reproduce run to run.
import type { Random, SessionIds } from '../../app/ports.js';

export class WebCryptoRandom implements Random {
  bytes(n: number): Uint8Array {
    const out = new Uint8Array(n);
    crypto.getRandomValues(out);
    return out;
  }

  /** Rejection sampling over the smallest power of two, so every element is
   * equally likely. */
  choice<T>(seq: readonly T[]): T {
    if (seq.length === 0) throw new RangeError('choice on an empty sequence');
    const n = seq.length;
    const k = 32 - Math.clz32(n - 1 || 1);
    const mask = (1 << k) - 1;
    for (;;) {
      const r = crypto.getRandomValues(new Uint32Array(1))[0]! & mask;
      if (r < n) return seq[r]!;
    }
  }
}

const N = 624;
const M = 397;
const MATRIX_A = 0x9908b0df;
const UPPER = 0x80000000;
const LOWER = 0x7fffffff;

/**
 * MT19937 seeded from an int: `init_by_array` over the absolute value split
 * into 32-bit words, bit draws for k <= 32, and rejection over the bit
 * length for `choice`. The exact seeding matters only in that it must not
 * change: the seed profiles' ids are pinned to this stream.
 */
export class SeededRandom implements Random {
  private readonly mt = new Uint32Array(N);
  private index = N;

  constructor(seed: number | bigint) {
    let s = BigInt(seed);
    if (s < 0n) s = -s;
    const key: number[] = [];
    while (s > 0n) {
      key.push(Number(s & 0xffffffffn));
      s >>= 32n;
    }
    if (key.length === 0) key.push(0);
    this.initByArray(key);
  }

  private initGenrand(s: number): void {
    this.mt[0] = s >>> 0;
    for (let i = 1; i < N; i++) {
      const prev = this.mt[i - 1]! ^ (this.mt[i - 1]! >>> 30);
      this.mt[i] = (Math.imul(1812433253, prev) + i) >>> 0;
    }
    this.index = N;
  }

  private initByArray(key: number[]): void {
    this.initGenrand(19650218);
    let i = 1;
    let j = 0;
    for (let k = Math.max(N, key.length); k > 0; k--) {
      const prev = this.mt[i - 1]! ^ (this.mt[i - 1]! >>> 30);
      this.mt[i] = ((this.mt[i]! ^ Math.imul(prev, 1664525)) + key[j]! + j) >>> 0;
      i++;
      j++;
      if (i >= N) {
        this.mt[0] = this.mt[N - 1]!;
        i = 1;
      }
      if (j >= key.length) j = 0;
    }
    for (let k = N - 1; k > 0; k--) {
      const prev = this.mt[i - 1]! ^ (this.mt[i - 1]! >>> 30);
      this.mt[i] = ((this.mt[i]! ^ Math.imul(prev, 1566083941)) - i) >>> 0;
      i++;
      if (i >= N) {
        this.mt[0] = this.mt[N - 1]!;
        i = 1;
      }
    }
    this.mt[0] = 0x80000000;
  }

  private next32(): number {
    if (this.index >= N) {
      const mt = this.mt;
      let kk = 0;
      for (; kk < N - M; kk++) {
        const y = (mt[kk]! & UPPER) | (mt[kk + 1]! & LOWER);
        mt[kk] = mt[kk + M]! ^ (y >>> 1) ^ (y & 1 ? MATRIX_A : 0);
      }
      for (; kk < N - 1; kk++) {
        const y = (mt[kk]! & UPPER) | (mt[kk + 1]! & LOWER);
        mt[kk] = mt[kk + (M - N)]! ^ (y >>> 1) ^ (y & 1 ? MATRIX_A : 0);
      }
      const y = (mt[N - 1]! & UPPER) | (mt[0]! & LOWER);
      mt[N - 1] = mt[M - 1]! ^ (y >>> 1) ^ (y & 1 ? MATRIX_A : 0);
      this.index = 0;
    }
    let y = this.mt[this.index++]!;
    y ^= y >>> 11;
    y ^= (y << 7) & 0x9d2c5680;
    y ^= (y << 15) & 0xefc60000;
    y ^= y >>> 18;
    return y >>> 0;
  }

  /** `getrandbits(k)` for 0 < k <= 32. */
  getrandbits(k: number): number {
    if (k <= 0 || k > 32) throw new RangeError('getrandbits: 1 <= k <= 32');
    return k === 32 ? this.next32() : this.next32() >>> (32 - k);
  }

  /** `token_bytes(n)` as the seeded harness draws it: one `getrandbits(8)` per byte. */
  bytes(n: number): Uint8Array {
    const out = new Uint8Array(n);
    for (let i = 0; i < n; i++) out[i] = this.getrandbits(8);
    return out;
  }

  /** `_randbelow(n)`: reject draws of `n.bit_length()` bits until one is under n. */
  randbelow(n: number): number {
    if (n <= 0) throw new RangeError('randbelow: n must be positive');
    const k = 32 - Math.clz32(n);
    for (;;) {
      const r = this.getrandbits(k);
      if (r < n) return r;
    }
  }

  choice<T>(seq: readonly T[]): T {
    if (seq.length === 0) throw new RangeError('choice on an empty sequence');
    return seq[this.randbelow(seq.length)]!;
  }
}

export const hex = (bytes: Uint8Array): string => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');

/** `secrets.token_hex(8)`: sixteen hex characters. */
export class RandomSessionIds implements SessionIds {
  constructor(private readonly random: Random) {}
  async next(): Promise<string> {
    return hex(this.random.bytes(8));
  }
}

/** `sha1("seed-session-<n>")[:16]` over a per-cell counter, reset by the seed. */
export class SeededSessionIds implements SessionIds {
  constructor(private readonly counter: { get(): Promise<number>; set(n: number): Promise<void> }) {}

  async next(): Promise<string> {
    const n = (await this.counter.get()) + 1;
    await this.counter.set(n);
    const digest = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(`seed-session-${n}`));
    return hex(new Uint8Array(digest)).slice(0, 16);
  }
}
