// Stored answer patterns are authored in a dialect of their own, so they
// run through a u-flag RegExp only after a pass that rewrites what the two
// spell differently and refuses (null) what they would grade differently.
// The dialect is fixed by the patterns already on people's cards; it is
// the reader that adapts, never the stored value.

export const MAX_REGEX_LEN = 500;

// Escapes the u flag accepts outside a class; the stored dialect reads any
// other escaped punctuation as the character itself.
const JS_SYNTAX = new Set([...'^$\\.*+?()[]{}|/']);
const QUANTIFIER = new Set(['?', '*', '+', '{']);

const isAsciiAlnum = (c: string) => /^[A-Za-z0-9]$/.test(c);
const isDigit = (c: string | undefined) => c !== undefined && c >= '0' && c <= '9';
// `\uD83D\uDE00`: two lone code points in the stored dialect, one astral
// code point under the u flag.
const isSurrogateHex = (hex: string[]) => {
  const v = parseInt(hex.join(''), 16);
  return hex.length === 4 && hex.every((h) => /^[0-9A-Fa-f]$/.test(h)) && v >= 0xd800 && v <= 0xdfff;
};

interface Scanned {
  source: string;
  /** \w \W \b \B \d \D present: ASCII-only in JS, Unicode in the stored
   * dialect. */
  shorthand: boolean;
  /** \B present: the stored dialect never matches it in an empty subject,
   * JS does. */
  emptyDiverges: boolean;
}

// A backreference is translated only to a group that is always set once
// the reference runs: closed, top level, unquantified, not cut off by a
// top-level `|`. The stored dialect fails a reference to an unset group,
// JS matches empty.
interface Group {
  closed: boolean;
  reliable: boolean;
}

/**
 * One pass over the pattern. Translated: `(?P<n>` to `(?<n>`, `(?P=n)` to
 * `\k<n>`, escaped punctuation to bare, one leading `(?i)`/`(?s)` group
 * dropped (both flags are always on). Refused: JS-only syntax the stored
 * dialect rejects
 * (`(?<n>`, `\k`, `\p`, `\P`, `\c`, `\u{`, `[]`, `[^]`), scoped flag
 * groups, lookbehind (fixed-width only in the stored dialect), a reused
 * group name, a
 * reference to a group that may be unset, a multi-digit `\NN`, an escaped
 * surrogate, and every other `(?` extension, which one engine or the other
 * lacks.
 */
function scan(pattern: string): Scanned | null {
  const cps = [...pattern];
  let out = '';
  let shorthand = false;
  let emptyDiverges = false;
  let inClass = false;
  let classOpen = false; // just after "[" or "[^", where "]" is literal
  const groups: Group[] = [];
  const named = new Map<string, Group>();
  const open: (Group | null)[] = [];
  const refOk = (g: Group | undefined) => g !== undefined && g.closed && g.reliable;
  for (let i = 0; i < cps.length; i++) {
    const c = cps[i]!;
    if (c === '\\') {
      const next = cps[i + 1];
      if (next === undefined) return null;
      i++;
      classOpen = false;
      if (isDigit(next) && next !== '0') {
        if (isDigit(cps[i + 1]) || !refOk(groups[Number(next) - 1])) return null;
        out += '\\' + next;
      } else if (isAsciiAlnum(next)) {
        if ('pPkc'.includes(next) || (next === 'u' && (cps[i + 1] === '{' || isSurrogateHex(cps.slice(i + 1, i + 5))))) return null;
        if ('wWbBdD'.includes(next)) shorthand = true;
        if (next === 'B') emptyDiverges = true;
        out += '\\' + next;
      } else if (JS_SYNTAX.has(next) || (inClass && next === '-')) {
        out += '\\' + next;
      } else {
        out += next;
      }
      continue;
    }
    if (inClass) {
      if (c === ']') {
        if (classOpen) return null;
        inClass = false;
      }
      classOpen = false;
      out += c;
      continue;
    }
    if (c === '[') {
      inClass = true;
      classOpen = true;
      out += '[';
      if (cps[i + 1] === '^') {
        out += '^';
        i++;
      }
      continue;
    }
    if (c === '(') {
      if (cps[i + 1] !== '?') {
        const g = { closed: false, reliable: open.length === 0 };
        groups.push(g);
        open.push(g);
        out += '(';
        continue;
      }
      const k = cps[i + 2];
      if (k === ':' || k === '=' || k === '!') {
        open.push(null);
        out += '(?' + k;
        i += 2;
      } else if (k === 'P' && cps[i + 3] === '<') {
        const end = cps.indexOf('>', i + 4);
        if (end < 0) return null;
        const name = cps.slice(i + 4, end).join('');
        if (named.has(name)) return null;
        const g = { closed: false, reliable: open.length === 0 };
        groups.push(g);
        named.set(name, g);
        open.push(g);
        out += `(?<${name}>`;
        i = end;
      } else if (k === 'P' && cps[i + 3] === '=') {
        const end = cps.indexOf(')', i + 4);
        if (end < 0) return null;
        const name = cps.slice(i + 4, end).join('');
        if (!refOk(named.get(name))) return null;
        out += `\\k<${name}>`;
        i = end;
      } else if (i === 0) {
        const end = cps.indexOf(')', 2);
        if (end <= 2 || !cps.slice(2, end).every((f) => f === 'i' || f === 's')) return null;
        i = end;
      } else {
        return null;
      }
      continue;
    }
    if (c === ')') {
      const g = open.pop();
      if (g === undefined) return null;
      if (g) {
        g.closed = true;
        if (QUANTIFIER.has(cps[i + 1] ?? '')) g.reliable = false;
      }
      out += ')';
      continue;
    }
    if (c === '|' && open.length === 0) for (const g of groups) g.reliable = false;
    out += c;
  }
  if (inClass) return null;
  return { source: out, shorthand, emptyDiverges };
}

/** The JS source for a stored pattern, or null when it cannot be trusted
 * to grade the same. */
export function translatePattern(pattern: string): string | null {
  return scan(pattern)?.source ?? null;
}

// The bare source is compiled first so an unbalanced pattern cannot be
// repaired by the fullmatch wrapper.
function compileFull(source: string): RegExp | null {
  try {
    new RegExp(source, 'isu');
    return new RegExp(`^(?:${source})$`, 'isu');
  } catch {
    return null;
  }
}

function isAscii(s: string): boolean {
  for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) > 127) return false;
  return true;
}

/** False when the engines could disagree on `pattern` over these subjects. */
function trusted(scanned: Scanned, pattern: string, subjects: string[]): boolean {
  if (scanned.shorthand && !(isAscii(pattern) && subjects.every(isAscii))) return false;
  if (scanned.emptyDiverges && subjects.some((s) => s === '')) return false;
  return true;
}

/**
 * The whole stripped answer against the pattern, case-insensitive and
 * dot-all. true or false is a verdict; null means the pattern is missing,
 * over MAX_REGEX_LEN, invalid, or one this engine might grade differently.
 */
export function matchRegex(pattern: unknown, given: unknown): boolean | null {
  if (typeof pattern !== 'string' || pattern === '') return null;
  if ([...pattern].length > MAX_REGEX_LEN) return null;
  const answer = String(given ?? '').trim();
  const scanned = scan(pattern);
  if (!scanned || !trusted(scanned, pattern, [answer])) return null;
  const re = compileFull(scanned.source);
  return re ? re.test(answer) : null;
}

/**
 * The stripped pattern when it compiles, fits the cap, and matches the whole
 * of the canonical answer and (when given) the prior answer; else null. The
 * trust rule of `matchRegex` runs over both subjects.
 */
export function validateRegexUpdate(
  pattern: unknown,
  expectedLiteral: string | null,
  priorGiven: string | null = null,
): string | null {
  if (typeof pattern !== 'string' || pattern === '') return null;
  const stripped = pattern.trim();
  if (stripped === '' || [...stripped].length > MAX_REGEX_LEN) return null;
  const subjects = [(expectedLiteral ?? '').trim()];
  if (priorGiven !== null) subjects.push(priorGiven.trim());
  const scanned = scan(stripped);
  if (!scanned || !trusted(scanned, stripped, subjects)) return null;
  const re = compileFull(scanned.source);
  if (!re) return null;
  return subjects.every((s) => re.test(s)) ? stripped : null;
}
