// CPython semantics the ports need: round(), str.strip(), str indexing and
// datetime.isoformat(). Each function pins one behavior of the reference.

export class IsoFormatError extends Error {}

/**
 * Python `round(x, nd)` for nd >= 0: the exact binary value rounded to nd
 * decimals, ties to even. `toFixed` is exact on the binary value but rounds
 * ties up, so a tie is detected first: x is a tie at nd decimals iff
 * x * 2^(nd+1) is an odd integer.
 */
export function pyRound(x: number, nd = 0): number {
  if (!Number.isFinite(x) || Math.abs(x) >= 2 ** 52 || nd >= 100) return x;
  const t = x * 2 ** (nd + 1);
  if (Number.isInteger(t) && Math.abs(t) % 2 === 1) {
    const m = BigInt(Math.abs(t)) * 5n ** BigInt(nd);
    const k = (m - 1n) / 2n;
    const r = k % 2n === 0n ? k : k + 1n;
    return Math.sign(x) * (Number(r) / 10 ** nd);
  }
  return Number(x.toFixed(nd));
}

// str.isspace(): 0x09-0x0D, 0x1C-0x1F, space, NEL, NBSP and the Zs block.
// Not JS trim(): U+FEFF stays, U+0085 and U+001C-U+001F go.
const PY_SPACE = '\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const STRIP = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, 'g');

/** Python `str.strip()`. */
export function pyStrip(s: string): string {
  return s.replace(STRIP, '');
}

/** The string as Python indexes it: one element per code point. */
export function codePoints(s: string): string[] {
  return Array.from(s);
}

const pad = (n: number, w: number) => String(n).padStart(w, '0');

/** `datetime.isoformat()` of an aware UTC datetime; microseconds only when non-zero. */
export function isoUtc(d: Date): string {
  const ms = d.getUTCMilliseconds();
  const frac = ms === 0 ? '' : `.${pad(ms * 1000, 6)}`;
  return (
    `${pad(d.getUTCFullYear(), 4)}-${pad(d.getUTCMonth() + 1, 2)}-${pad(d.getUTCDate(), 2)}` +
    `T${pad(d.getUTCHours(), 2)}:${pad(d.getUTCMinutes(), 2)}:${pad(d.getUTCSeconds(), 2)}${frac}+00:00`
  );
}

const ISO =
  /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?(Z|[+-]\d{2}(?::?\d{2})?)?$/;

/**
 * `datetime.fromisoformat` over the extended format `isoformat()` emits:
 * date, optional time, optional fraction (truncated to milliseconds),
 * optional `Z` or `+HH[:MM]` offset. Naive parses as UTC.
 */
export function parseIso(s: string): Date {
  const m = ISO.exec(s);
  if (!m) throw new IsoFormatError(`invalid isoformat string: ${JSON.stringify(s)}`);
  const [, y, mo, d, h = '0', mi = '0', sec = '0', frac = '', off = ''] = m;
  const year = Number(y), month = Number(mo), day = Number(d);
  const hour = Number(h), minute = Number(mi), second = Number(sec);
  const ms = Number(frac.padEnd(6, '0').slice(0, 3));
  const utc = Date.UTC(year, month - 1, day, hour, minute, second, ms);
  const back = new Date(utc);
  const valid =
    back.getUTCFullYear() === year && back.getUTCMonth() === month - 1 && back.getUTCDate() === day &&
    hour < 24 && minute < 60 && second < 60;
  if (!valid) throw new IsoFormatError(`out of range: ${JSON.stringify(s)}`);
  let offsetMin = 0;
  if (off && off !== 'Z') {
    const sign = off[0] === '-' ? -1 : 1;
    const digits = off.slice(1).replace(':', '');
    const oh = Number(digits.slice(0, 2)), om = Number(digits.slice(2) || '0');
    if (oh > 23 || om > 59) throw new IsoFormatError(`offset out of range: ${JSON.stringify(s)}`);
    offsetMin = sign * (oh * 60 + om);
  }
  return new Date(utc - offsetMin * 60_000);
}
