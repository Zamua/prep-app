// Character predicates over code points. CommonMark classifies by Unicode
// category, which JavaScript's `\s`, `\w` and `\d` do not follow, so the
// sets are spelled out here and every pattern goes through `unicodeRe`.

// Whitespace: bidi WS, B and S, plus category Zs.
const SPACE_CHARS =
  " \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000";
// The same set as a character-class body.
const SPACE_CLASS = " \\t\\n\\r\\x0b\\x0c\\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
// ASCII whitespace only.
export const ASCII_WHITESPACE = " \t\n\r\x0b\x0c";
// ASCII punctuation only.
export const ASCII_PUNCTUATION = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";
export const PUNCTUATION_CLASS = "[!-/:-@\\[-`{-~]";

const ALNUM = /^[\p{L}\p{N}]$/u;
const DIGIT = /^\p{Nd}$/u;

export function isSpace(c: string): boolean {
  return c !== "" && SPACE_CHARS.includes(c);
}

export function isAlnum(c: string): boolean {
  return ALNUM.test(c);
}

export function isDigit(c: string): boolean {
  return DIGIT.test(c);
}

export function isAsciiPunct(c: string): boolean {
  return c.length === 1 && ASCII_PUNCTUATION.includes(c);
}

/** Code point starting at index i, or "" past either end. */
export function cpAt(s: string, i: number): string {
  if (i < 0 || i >= s.length) return "";
  const c = s.codePointAt(i)!;
  return c > 0xffff ? s.slice(i, i + 2) : s[i]!;
}

/** Code point ending just before index i, or "" at the start. */
export function cpBefore(s: string, i: number): string {
  if (i <= 0 || i > s.length) return "";
  const lo = s.charCodeAt(i - 1);
  if (lo >= 0xdc00 && lo <= 0xdfff && i >= 2) {
    const hi = s.charCodeAt(i - 2);
    if (hi >= 0xd800 && hi <= 0xdbff) return s.slice(i - 2, i);
  }
  return s[i - 1]!;
}

function trimmedStart(s: string, chars: string | null): number {
  let i = 0;
  while (i < s.length) {
    const c = cpAt(s, i);
    if (chars === null ? !isSpace(c) : !chars.includes(c)) break;
    i += c.length;
  }
  return i;
}

function trimmedEnd(s: string, chars: string | null): number {
  let i = s.length;
  while (i > 0) {
    const c = cpBefore(s, i);
    if (chars === null ? !isSpace(c) : !chars.includes(c)) break;
    i -= c.length;
  }
  return i;
}

/** Trims `chars` from both ends; null trims whitespace. */
export function trim(s: string, chars: string | null = null): string {
  const start = trimmedStart(s, chars);
  return s.slice(start, Math.max(start, trimmedEnd(s, chars)));
}

export function trimStart(s: string, chars: string | null = null): string {
  return s.slice(trimmedStart(s, chars));
}

export function trimEnd(s: string, chars: string | null = null): string {
  return s.slice(0, trimmedEnd(s, chars));
}

/** Splits on runs of whitespace, dropping empties. */
export function splitWhitespace(s: string): string[] {
  const out: string[] = [];
  let cur = "";
  for (const c of s) {
    if (isSpace(c)) {
      if (cur) out.push(cur);
      cur = "";
    } else cur += c;
  }
  if (cur) out.push(cur);
  return out;
}

/**
 * A pattern as a JavaScript RegExp with the classifications above: `\s`/`\S`
 * become the whitespace set, `.` excludes only `\n`, `\d` is ASCII, `^`/`$`
 * are per-line when `multiline`. Always unicode mode.
 */
export function unicodeRe(pattern: string, flags = "", multiline = false): RegExp {
  let out = "";
  let inClass = false;
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i]!;
    if (c === "[" && !inClass && pattern.startsWith("[\\s\\S]", i)) {
      out += "[^]";
      i += 5;
      continue;
    }
    if (c === "\\") {
      const n = pattern[i + 1] ?? "";
      i++;
      if (n === "s") out += inClass ? SPACE_CLASS : "[" + SPACE_CLASS + "]";
      else if (n === "S") out += "[^" + SPACE_CLASS + "]";
      else if (n === "d") out += inClass ? "0-9" : "[0-9]";
      else if (/[A-Za-z0-9]/.test(n) || "^$\\.*+?()[]{}|/".includes(n)) out += "\\" + n;
      else if (n === "-") out += inClass ? "\\-" : "-";
      else out += n;
      continue;
    }
    if (inClass) {
      if (c === "]") inClass = false;
      out += c;
      continue;
    }
    if (c === "[") {
      inClass = true;
      out += c;
    } else if (c === ".") out += "[^\\n]";
    else if (c === "^") out += multiline ? "(?<![^\\n])" : "^";
    else if (c === "$") out += multiline ? "(?=\\n|$)" : "(?=\\n?$)";
    else out += c;
  }
  return new RegExp(out, flags + "u");
}
