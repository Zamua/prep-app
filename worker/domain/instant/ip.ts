// The per-IP limiter key. IPv4 keys on the exact address; IPv6 keys on the
// /64 prefix, since one host trivially owns a /64. Values that do not parse
// share one sentinel bucket. Header selection is the router's.

export const SENTINEL_BUCKET = 'unresolved';

const V4 = /^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$/;
const HEXTET = /^[0-9a-fA-F]{1,4}$/;

/** Strict dotted quad: no leading zeros, octets under 256. */
function parseV4(s: string): number[] | null {
  const m = V4.exec(s);
  if (!m) return null;
  const octets: number[] = [];
  for (const part of m.slice(1)) {
    if (part.length > 1 && part.startsWith('0')) return null;
    const n = Number(part);
    if (n > 255) return null;
    octets.push(n);
  }
  return octets;
}

/** Python's `ipaddress` IPv6 grammar; scoped (`%`) forms are refused. */
function parseV6(s: string): bigint | null {
  if (s.includes('%')) return null;
  const parts = s.split(':');
  if (parts.length < 3) return null;
  const last = parts[parts.length - 1]!;
  if (last.includes('.')) {
    const v4 = parseV4(last);
    if (!v4) return null;
    parts.pop();
    parts.push(((v4[0]! << 8) | v4[1]!).toString(16), ((v4[2]! << 8) | v4[3]!).toString(16));
  }
  if (parts.length > 9) return null;
  let skip: number | null = null;
  for (let i = 1; i < parts.length - 1; i++) {
    if (parts[i] === '') {
      if (skip !== null) return null;
      skip = i;
    }
  }
  let hi: number;
  let lo: number;
  let skipped: number;
  if (skip !== null) {
    hi = skip;
    lo = parts.length - skip - 1;
    if (parts[0] === '') {
      hi--;
      if (hi) return null;
    }
    if (parts[parts.length - 1] === '') {
      lo--;
      if (lo) return null;
    }
    skipped = 8 - (hi + lo);
    if (skipped < 1) return null;
  } else {
    if (parts.length !== 8 || parts[0] === '' || parts[parts.length - 1] === '') return null;
    hi = 8;
    lo = 0;
    skipped = 0;
  }
  let ip = 0n;
  const push = (part: string): boolean => {
    if (!HEXTET.test(part)) return false;
    ip = (ip << 16n) | BigInt(parseInt(part, 16));
    return true;
  };
  for (let i = 0; i < hi; i++) if (!push(parts[i]!)) return null;
  ip <<= BigInt(16 * skipped);
  for (let i = parts.length - lo; i < parts.length; i++) if (!push(parts[i]!)) return null;
  return ip;
}

const formatV4 = (n: bigint) => [24n, 16n, 8n, 0n].map((s) => String((n >> s) & 255n)).join('.');

/** The hextets with the longest zero run (leftmost on ties) as `::`. */
function formatV6(ip: bigint): string {
  const hextets: string[] = [];
  for (let i = 7; i >= 0; i--) hextets.push(((ip >> BigInt(16 * i)) & 0xffffn).toString(16));
  let bestStart = -1;
  let bestLen = 0;
  let start = -1;
  let len = 0;
  hextets.forEach((h, i) => {
    if (h === '0') {
      len++;
      if (start === -1) start = i;
      if (len > bestLen) {
        bestLen = len;
        bestStart = start;
      }
    } else {
      len = 0;
      start = -1;
    }
  });
  if (bestLen > 1) {
    const end = bestStart + bestLen;
    const out = [...hextets.slice(0, bestStart), '', ...hextets.slice(end)];
    if (end === hextets.length) out.push('');
    if (bestStart === 0) out.unshift('');
    return out.join(':');
  }
  return hextets.join(':');
}

export function limiterBucket(value: string): string {
  if (!value) return SENTINEL_BUCKET;
  if (parseV4(value)) return value;
  const ip = parseV6(value);
  if (ip === null) return SENTINEL_BUCKET;
  if (ip >> 32n === 0xffffn) return formatV4(ip & 0xffffffffn);
  return `${formatV6((ip >> 64n) << 64n)}/64`;
}
