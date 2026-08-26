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

/**
 * str.isspace() as class body text: 0x09-0x0D, 0x1C-0x1F, space, NEL, NBSP
 * and the Zs block. Not JS trim() or \s: U+FEFF stays out, U+0085 and
 * U+001C-U+001F are in.
 */
export const PY_SPACE = '\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
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

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

function pyJsonString(s: string): string {
  let out = '"';
  for (const ch of s) {
    const cp = ch.codePointAt(0)!;
    if (ch === '"') out += '\\"';
    else if (ch === '\\') out += '\\\\';
    else if (ch === '\n') out += '\\n';
    else if (ch === '\r') out += '\\r';
    else if (ch === '\t') out += '\\t';
    else if (ch === '\b') out += '\\b';
    else if (ch === '\f') out += '\\f';
    else if (cp < 0x20 || cp > 0x7e) {
      // ensure_ascii: one \uXXXX per UTF-16 unit, so astral code points become a pair.
      for (const unit of ch) out += `\\u${unit.charCodeAt(0).toString(16).padStart(4, '0')}`;
      if (cp > 0xffff) {
        out = out.slice(0, -12);
        const hi = 0xd800 + ((cp - 0x10000) >> 10);
        const lo = 0xdc00 + ((cp - 0x10000) & 0x3ff);
        out += `\\u${hi.toString(16)}\\u${lo.toString(16)}`;
      }
    } else out += ch;
  }
  return out + '"';
}

/**
 * `json.dumps(value)` with CPython's defaults: `, ` and `: ` separators,
 * ASCII escapes, insertion-ordered keys. Integral numbers print as ints.
 */
export function pyJsonDumps(value: JsonValue): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return value > 0 ? 'Infinity' : value < 0 ? '-Infinity' : 'NaN';
    return Number.isInteger(value) ? String(value) : String(value);
  }
  if (typeof value === 'string') return pyJsonString(value);
  if (Array.isArray(value)) return `[${value.map(pyJsonDumps).join(', ')}]`;
  return `{${Object.entries(value)
    .map(([k, v]) => `${pyJsonString(k)}: ${pyJsonDumps(v)}`)
    .join(', ')}}`;
}

/**
 * `json.dumps(value, indent=n, sort_keys=True)`: the indented form uses the
 * `','` item separator, so a line never ends in a trailing space.
 */
export function pyJsonDumpsIndent(value: JsonValue, indent: number, sortKeys = false): string {
  const pad = (depth: number) => '\n' + ' '.repeat(indent * depth);
  const walk = (v: JsonValue, depth: number): string => {
    if (Array.isArray(v)) {
      if (!v.length) return '[]';
      return `[${v.map((item) => pad(depth + 1) + walk(item, depth + 1)).join(',')}${pad(depth)}]`;
    }
    if (v !== null && typeof v === 'object') {
      let keys = Object.keys(v);
      if (sortKeys) keys = keys.sort();
      if (!keys.length) return '{}';
      return `{${keys.map((k) => `${pad(depth + 1)}${pyJsonString(k)}: ${walk(v[k]!, depth + 1)}`).join(',')}${pad(depth)}}`;
    }
    return pyJsonDumps(v);
  };
  return walk(value, 0);
}

/**
 * C's `%.<precision>g`, which is what Python's format spec compiles to:
 * significant digits, trailing zeros dropped, and the exponent form outside
 * `-4 <= exp < precision` with at least two exponent digits.
 */
export function pyFormatG(x: number, precision = 6): string {
  if (!Number.isFinite(x)) return x > 0 ? 'inf' : x < 0 ? '-inf' : 'nan';
  const p = precision === 0 ? 1 : precision;
  if (x === 0) return '0';
  // The exponent of the value already rounded to `p` digits, so a carry
  // (9.999995 at p=6) picks the form its rounded self belongs in.
  const exp = Number(x.toExponential(p - 1).split('e')[1]);
  if (exp < -4 || exp >= p) {
    const [mantissa] = x.toExponential(p - 1).split('e');
    const trimmed = mantissa!.includes('.') ? mantissa!.replace(/\.?0+$/, '') : mantissa!;
    const sign = exp < 0 ? '-' : '+';
    return `${trimmed}e${sign}${String(Math.abs(exp)).padStart(2, '0')}`;
  }
  const fixed = x.toFixed(Math.max(0, p - 1 - exp));
  return fixed.includes('.') ? fixed.replace(/\.?0+$/, '') : fixed;
}
