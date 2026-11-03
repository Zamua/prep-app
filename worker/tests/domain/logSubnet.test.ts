// The only client identity a log line may carry: the IPv4 /24 or IPv6 /40,
// cut before anything else is built.
import { describe, expect, it } from 'vitest';
import { logSubnet } from '../../domain/instant/ip';

// mulberry32: a fixed seed keeps a failure reproducible.
function prng(seed: number): () => number {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

describe('logSubnet', () => {
  it('is the /24 for IPv4 and the /40 for IPv6', () => {
    expect(logSubnet('203.0.113.77')).toBe('203.0.113.0/24');
    expect(logSubnet(' 198.51.100.9 ')).toBe('198.51.100.0/24');
    expect(logSubnet('2001:db8:abcd:1234::1')).toBe('2001:db8:ab00::/40');
    expect(logSubnet('2001:0db8:ab12:ffff:ffff:ffff:ffff:ffff')).toBe('2001:db8:ab00::/40');
    expect(logSubnet('2001:DB8:AB12::1')).toBe('2001:db8:ab00::/40');
  });

  it('counts an IPv4-mapped IPv6 address as IPv4', () => {
    expect(logSubnet('::ffff:203.0.113.77')).toBe('203.0.113.0/24');
    expect(logSubnet('::ffff:cb00:714d')).toBe('203.0.113.0/24');
  });

  it('is unknown for anything that is no address', () => {
    for (const ip of ['', 'garbage', '1.2.3', '256.1.1.1', '1.2.3.4.5', '01.2.3.4', 'fe80::1%eth0', '2001:db8::g', null, undefined, 5, {}]) {
      expect(logSubnet(ip), String(ip)).toBe('unknown');
    }
  });

  it('never carries the input or any host bit', () => {
    const rand = prng(20261103);
    const int = (n: number) => Math.floor(rand() * n);
    for (let i = 0; i < 500; i++) {
      const v4 = i % 2 === 0;
      const groups = Array.from({ length: 8 }, () => (rand() < 0.3 ? 0 : int(0x10000)));
      groups[0] = 0x2001 + int(0x1000);
      groups[2]! |= 1;
      groups[7] = 1 + int(0xffff);
      const ip = v4 ? [1 + int(255), int(256), int(256), 1 + int(255)].join('.') : groups.map((g) => g.toString(16)).join(':');
      const out = logSubnet(ip);
      expect(out.includes(ip), `${ip} -> ${out}`).toBe(false);
      if (v4) {
        const [a, b, c] = ip.split('.');
        expect(out).toBe(`${a}.${b}.${c}.0/24`);
      } else {
        const want = (BigInt(groups[0]!) << 24n) | (BigInt(groups[1]!) << 8n) | BigInt(groups[2]! >> 8);
        const [net, len] = out.split('/');
        expect(len).toBe('40');
        // Parse the logged prefix back: its first 40 bits are the input's, the rest zero.
        const expanded = net!.includes('::') ? net!.replace('::', ':' + ':0'.repeat(8 - net!.split(':').filter(Boolean).length) + ':') : net!;
        const parts = expanded.split(':').map((p) => parseInt(p || '0', 16));
        const got = (BigInt(parts[0]!) << 24n) | (BigInt(parts[1]!) << 8n) | BigInt(parts[2]! >> 8);
        expect(got, `${ip} -> ${out}`).toBe(want);
        expect(parts[2]! & 0xff, `${ip} -> ${out}`).toBe(0);
        expect(parts.slice(3).every((p) => p === 0), `${ip} -> ${out}`).toBe(true);
      }
    }
  });
});
