// Python `re` answer patterns run through a u-flag RegExp. A pattern is
// translated where the two dialects spell one thing differently, refused
// (null) where they would silently disagree, and otherwise left to the
// engine, which throws on the escapes only re knows.
import { codePoints, pyStrip } from '../py';

export const MAX_REGEX_LEN = 500;

// Escapes the u flag accepts outside a class; re reads any other escaped
// punctuation as the character itself.
const JS_SYNTAX = new Set([...'^$\\.*+?()[]{}|/']);

const isAsciiAlnum = (c: string) => /^[A-Za-z0-9]$/.test(c);

interface Scanned {
  source: string;
  /** \w \W \b \B \d \D present: ASCII-only in JS, Unicode in re. */
  shorthand: boolean;
}

/**
 * One pass over the pattern. Translated: `(?P<n>` to `(?<n>`, `(?P=n)` to
 * `\k<n>`, escaped punctuation to bare, one leading `(?i)`/`(?s)` group
 * dropped (both flags are always on). Refused: JS-only syntax re rejects
 * (`(?<n>`, `\k`, `\p`, `\P`, `\c`, `\u{`, `[]`, `[^]`), scoped flag groups,
 * and every other `(?` extension, which one engine or the other lacks.
 */
function scan(pattern: string): Scanned | null {
  const cps = codePoints(pattern);
  let out = '';
  let shorthand = false;
  let inClass = false;
  let classOpen = false; // just after "[" or "[^", where re takes "]" literally
  for (let i = 0; i < cps.length; i++) {
    const c = cps[i]!;
    if (c === '\\') {
      const next = cps[i + 1];
      if (next === undefined) return null;
      i++;
      classOpen = false;
      if (isAsciiAlnum(next)) {
        if ('pPkc'.includes(next) || (next === 'u' && cps[i + 1] === '{')) return null;
        if ('wWbBdD'.includes(next)) shorthand = true;
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
    if (c === '(' && cps[i + 1] === '?') {
      const k = cps[i + 2];
      if (k === ':' || k === '=' || k === '!') {
        out += '(?' + k;
        i += 2;
      } else if (k === '<' && (cps[i + 3] === '=' || cps[i + 3] === '!')) {
        out += '(?<' + cps[i + 3];
        i += 3;
      } else if (k === 'P' && cps[i + 3] === '<') {
        const end = cps.indexOf('>', i + 4);
        if (end < 0) return null;
        out += `(?<${cps.slice(i + 4, end).join('')}>`;
        i = end;
      } else if (k === 'P' && cps[i + 3] === '=') {
        const end = cps.indexOf(')', i + 4);
        if (end < 0) return null;
        out += `\\k<${cps.slice(i + 4, end).join('')}>`;
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
    out += c;
  }
  if (inClass) return null;
  return { source: out, shorthand };
}

/** The JS source for a re pattern, or null when it cannot be trusted to agree. */
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

/**
 * `match_regex`: fullmatch of the stripped answer, case-insensitive and
 * dot-all. true or false is a verdict; null means the pattern is missing,
 * over MAX_REGEX_LEN, invalid, or one this engine might grade differently.
 */
export function matchRegex(pattern: unknown, given: unknown): boolean | null {
  if (typeof pattern !== 'string' || pattern === '') return null;
  if (codePoints(pattern).length > MAX_REGEX_LEN) return null;
  const answer = pyStrip(String(given ?? ''));
  const scanned = scan(pattern);
  if (!scanned) return null;
  if (scanned.shorthand && !(isAscii(pattern) && isAscii(answer))) return null;
  const re = compileFull(scanned.source);
  return re ? re.test(answer) : null;
}

/**
 * `validate_regex_update`: the stripped pattern when it compiles, fits the
 * cap, fullmatches the canonical answer and (when given) the prior answer;
 * else null.
 */
export function validateRegexUpdate(
  pattern: unknown,
  expectedLiteral: string | null,
  priorGiven: string | null = null,
): string | null {
  if (typeof pattern !== 'string' || pattern === '') return null;
  const stripped = pyStrip(pattern);
  if (stripped === '' || codePoints(stripped).length > MAX_REGEX_LEN) return null;
  const scanned = scan(stripped);
  if (!scanned) return null;
  const re = compileFull(scanned.source);
  if (!re) return null;
  if (!re.test(pyStrip(expectedLiteral ?? ''))) return null;
  if (priorGiven !== null && !re.test(pyStrip(priorGiven))) return null;
  return stripped;
}
