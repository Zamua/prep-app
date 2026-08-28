// How a list of answer values is ordered and spelled in the multi-select
// feedback the learner reads.
import type { Scalar } from './answerJson';

export class GradingError extends Error {}

type Kind = 'str' | 'num' | 'none';

function kindOf(v: Scalar): Kind {
  if (typeof v === 'string') return 'str';
  if (v === null) return 'none';
  return 'num';
}

/** Code point order, which is not UTF-16 unit order above U+FFFF. */
function cmpStr(a: string, b: string): number {
  const xs = Array.from(a), ys = Array.from(b);
  const n = Math.min(xs.length, ys.length);
  for (let i = 0; i < n; i++) {
    const d = xs[i]!.codePointAt(0)! - ys[i]!.codePointAt(0)!;
    if (d !== 0) return d;
  }
  return xs.length - ys.length;
}

/** Exact int-versus-float comparison, so a large int never loses to
 * rounding; bool is 0 or 1. */
function cmpNum(a: boolean | bigint | number, b: boolean | bigint | number): number {
  const x = typeof a === 'boolean' ? BigInt(a ? 1 : 0) : a;
  const y = typeof b === 'boolean' ? BigInt(b ? 1 : 0) : b;
  if (typeof x === 'bigint' && typeof y === 'bigint') return x < y ? -1 : x > y ? 1 : 0;
  if (typeof x === 'number' && typeof y === 'number') return x < y ? -1 : x > y ? 1 : 0;
  if (typeof x === 'number') return -cmpNum(y, x);
  const i = x as bigint, f = y as number;
  if (Number.isNaN(f)) return 0;
  if (!Number.isFinite(f)) return f > 0 ? -1 : 1;
  const floor = BigInt(Math.floor(f));
  if (i < floor) return -1;
  if (i > floor) return 1;
  return f > Math.floor(f) ? -1 : 0;
}

/**
 * Ascending order. Strings, numbers (int, float, bool) and null are the
 * comparable classes; two elements of different classes cannot be ordered,
 * which is a grading error rather than an arbitrary result.
 */
export function sortedValues(items: Scalar[]): Scalar[] {
  const out = items.slice();
  if (out.length < 2) return out;
  const kind = kindOf(out[0]!);
  if (out.some((v) => kindOf(v) !== kind)) throw new GradingError("'<' not supported between instances");
  if (kind === 'str') return (out as string[]).sort(cmpStr);
  if (kind === 'num') return (out as (boolean | bigint | number)[]).sort(cmpNum);
  return out;
}

// Unprintable: these categories and no other character. Escaped rather
// than emitted, so an answer of invisible characters is still readable.
const UNPRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;

function stringLiteral(s: string): string {
  const q = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = q;
  for (const ch of s) {
    const cp = ch.codePointAt(0)!;
    if (ch === q || ch === '\\') out += '\\' + ch;
    else if (ch === '\n') out += '\\n';
    else if (ch === '\r') out += '\\r';
    else if (ch === '\t') out += '\\t';
    else if (cp === 0x20 || (cp > 0x20 && cp < 0x7f)) out += ch;
    else if (cp < 0x7f || UNPRINTABLE.test(ch)) {
      const width = cp < 0x100 ? 2 : cp < 0x10000 ? 4 : 8;
      out += (width === 8 ? '\\U' : width === 4 ? '\\u' : '\\x') + cp.toString(16).padStart(width, '0');
    } else out += ch;
  }
  return out + q;
}

/**
 * A float as a source literal: the shortest round-trip digits, fixed
 * notation for exponents in [-4, 16), else scientific with a two-digit
 * exponent, and an integral value keeps its `.0` so it does not read as an
 * int.
 */
export function floatLiteral(x: number): string {
  if (Number.isNaN(x)) return 'nan';
  if (x === Infinity) return 'inf';
  if (x === -Infinity) return '-inf';
  if (x === 0) return Object.is(x, -0) ? '-0.0' : '0.0';
  const [mant, e] = Math.abs(x).toExponential().split('e') as [string, string];
  const exp = Number(e);
  const digits = mant.replace('.', '');
  let body: string;
  if (exp < -4 || exp >= 16) {
    body = digits.length === 1 ? digits : `${digits[0]}.${digits.slice(1)}`;
    body += `e${exp < 0 ? '-' : '+'}${String(Math.abs(exp)).padStart(2, '0')}`;
  } else if (exp >= 0) {
    body = `${digits.slice(0, exp + 1).padEnd(exp + 1, '0')}.${digits.slice(exp + 1) || '0'}`;
  } else {
    body = `0.${'0'.repeat(-exp - 1)}${digits}`;
  }
  return (x < 0 ? '-' : '') + body;
}

/** One answer value, spelled the way the feedback shows it. */
export function literal(v: Scalar): string {
  if (typeof v === 'string') return stringLiteral(v);
  if (v === null) return 'None';
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  if (typeof v === 'bigint') return v.toString();
  return floatLiteral(v);
}

/** A list of answer values, spelled the way the feedback shows it. */
export function literalList(items: Scalar[]): string {
  return `[${items.map(literal).join(', ')}]`;
}
