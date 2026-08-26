// mistune.helpers link grammar and mistune._inline.links: destinations,
// titles, labels, and the inline `[text](url)` / `![alt](url)` rule.
import { ASCII_PUNCTUATION, PUNCTUATION_CLASS, pySplit, pyre } from "./chars";
import type { InlineParser, InlineState, RuleMatch } from "./inline";
import type { RefLink, Token } from "./tokens";
import { escapeUrl } from "./url";

export const LINK_LABEL = "(?:[^\\\\\\[\\]]|\\\\.){0,500}";
// helpers.ASCII_WHITESPACE: no vertical tab.
const LINK_WHITESPACE = " \t\n\r\f";

const INLINE_LINK_LABEL_RE = pyre(LINK_LABEL + "\\]", "y");
const ESCAPE_CHAR_RE = pyre("\\\\(" + PUNCTUATION_CLASS + ")", "g");

export function unescapeChar(text: string): string {
  return text.replace(ESCAPE_CHAR_RE, "$1");
}

export function parseLinkLabel(src: string, start: number): [string, number] | [null, null] {
  INLINE_LINK_LABEL_RE.lastIndex = start;
  const m = INLINE_LINK_LABEL_RE.exec(src);
  if (m) return [m[0].slice(0, -1), start + m[0].length];
  return [null, null];
}

export function parseLinkHref(src: string, start: number, block = false): [string, number] | [null, null] {
  const [href, hrefPos] = parseLinkHrefWithEnd(src, start, block);
  if (href === null) return [null, null];
  return [href, hrefPos!];
}

function parseLinkHrefWithEnd(src: string, startPos: number, block: boolean): [string | null, number | null, number] {
  let pos = skipLinkStartWhitespace(src, startPos);
  if (pos >= src.length) return [null, null, pos];
  if (src[pos] === "<") {
    const [href, hrefPos] = parseAngleLinkHref(src, pos);
    if (href === null) return [null, null, pos];
    return [href, hrefPos!, hrefPos!];
  }
  if (block && LINK_WHITESPACE.includes(src[pos]!)) return [null, null, pos];
  const start = pos;
  let level = 0;
  while (pos < src.length) {
    const c = src[pos]!;
    if (LINK_WHITESPACE.includes(c)) break;
    if (c === "\x00") return [null, null, pos];
    if (c === "\\" && pos + 1 < src.length && ASCII_PUNCTUATION.includes(src[pos + 1]!)) {
      pos = Math.min(pos + 2, src.length);
      continue;
    }
    if (!block) {
      if (c === "(") level += 1;
      else if (c === ")") {
        if (level === 0) break;
        level -= 1;
      }
    }
    pos += 1;
  }
  if (!block && level !== 0) return [null, null, pos];
  return [src.slice(start, pos), pos, pos];
}

export function parseLinkTitle(src: string, startPos: number, maxPos: number): [string, number] | [null, null] {
  let pos = startPos;
  if (pos >= maxPos || !LINK_WHITESPACE.includes(src[pos]!)) return [null, null];
  pos = skipAsciiWhitespace(src, pos, maxPos);
  if (pos >= maxPos) return [null, null];
  const opener = src[pos]!;
  const closer = opener === "'" ? "'" : opener === '"' ? '"' : opener === "(" ? ")" : null;
  if (closer === null) return [null, null];
  pos += 1;
  let title = "";
  while (pos < maxPos) {
    const c = src[pos]!;
    if (c === "\x00") return [null, null];
    if (c === "\\") {
      if (pos + 1 < maxPos) {
        title += src.slice(pos, pos + 2);
        pos += 2;
        continue;
      }
      return [null, null];
    }
    if (c === closer) return [unescapeChar(title), pos + 1];
    title += c;
    pos += 1;
  }
  return [null, null];
}

export interface LinkAttrs {
  url: string;
  title?: string;
}

export function parseLinkDestination(src: string, pos: number): [LinkAttrs, number] | [null, null] {
  const [attrs, nextPos] = parseLinkWithEnd(src, pos);
  if (attrs === null) return [null, null];
  return [attrs, nextPos!];
}

export function parseLinkWithEnd(src: string, pos: number): [LinkAttrs | null, number | null, number] {
  const [rawHref, hrefPos, scanEnd] = parseLinkHrefWithEnd(src, pos, false);
  if (rawHref === null) return [null, null, scanEnd];
  const [title, titlePos] = parseLinkTitle(src, hrefPos!, src.length);
  let nextPos = titlePos || hrefPos!;
  nextPos = skipAsciiWhitespace(src, nextPos, src.length);
  if (nextPos >= src.length || src[nextPos] !== ")") return [null, null, nextPos];
  const attrs: LinkAttrs = { url: escapeUrl(unescapeChar(rawHref)) };
  if (title) attrs.title = title;
  return [attrs, nextPos + 1, nextPos + 1];
}

function skipAsciiWhitespace(src: string, pos: number, maxPos: number): number {
  while (pos < maxPos && LINK_WHITESPACE.includes(src[pos]!)) pos += 1;
  return pos;
}

function skipLinkStartWhitespace(src: string, pos: number): number {
  while (pos < src.length && (src[pos] === " " || src[pos] === "\t")) pos += 1;
  if (pos < src.length && (src[pos] === "\n" || src[pos] === "\r")) {
    if (src[pos] === "\r" && pos + 1 < src.length && src[pos + 1] === "\n") pos += 2;
    else pos += 1;
    while (pos < src.length && (src[pos] === " " || src[pos] === "\t")) pos += 1;
  }
  return pos;
}

function parseAngleLinkHref(src: string, pos: number): [string, number] | [null, null] {
  const start = pos + 1;
  pos = start;
  while (pos < src.length) {
    const c = src[pos]!;
    if (c === ">") return [src.slice(start, pos), pos + 1];
    if ("<\\\n\r\x00".includes(c)) return [null, null];
    pos += 1;
  }
  return [null, null];
}

// util.unikey: the reference label's lookup key.
export function unikey(s: string): string {
  return pySplit(s).join(" ").toLowerCase().toUpperCase();
}

export function parseInlineLink(inline: InlineParser, m: RuleMatch, state: InlineState): number | null {
  let pos = m.end;
  const marker = m.text;
  const isImage = marker[0] === "!";
  if (isImage && inline.maxImageDepth > 0 && state.imageDepth >= inline.maxImageDepth) {
    state.appendToken({ type: "text", raw: marker + state.src.slice(pos) });
    return state.src.length;
  }
  if (!isImage && state.inLink) {
    state.appendToken({ type: "text", raw: marker });
    return pos;
  }
  if (!isImage && pos <= state.noLinkBefore) {
    state.appendToken({ type: "text", raw: marker });
    return pos;
  }
  if (isImage && pos <= state.noImageBefore) {
    state.appendToken({ type: "text", raw: marker });
    return pos;
  }

  let text: string | null = null;
  let textStart = pos;
  let textEnd = pos;
  let [label, endPos] = parseLinkLabel(state.src, pos);
  if (label === null) {
    if (pos <= state.noCloseBracketBefore) {
      state.appendToken({ type: "text", raw: marker });
      return pos;
    }
    const closePos = closingBracketMap(state).get(pos);
    if (closePos === undefined) {
      if (state.src.length > state.noCloseBracketBefore) state.noCloseBracketBefore = state.src.length;
      return null;
    }
    textStart = pos;
    textEnd = closePos;
    endPos = closePos + 1;
  } else {
    text = label;
    textStart = pos;
    textEnd = endPos! - 1;
  }
  const bodyEndPos = endPos!;

  if (!isImage && labelContainsLink(state, textStart, textEnd)) return null;
  if (endPos! >= state.src.length && label === null) {
    markNoLinkBefore(state, bodyEndPos);
    return null;
  }

  if (!isImage) {
    const precPos = inline.precedenceScan(m, state, endPos!, ["codespan", "prec_auto_link", "prec_inline_html"]);
    if (precPos) return precPos;
  }

  if (endPos! < state.src.length) {
    const c = state.src[endPos!];
    if (c === "(") {
      const [attrs, pos2, scanEnd] = parseLinkWithEnd(state.src, endPos! + 1);
      if (pos2) {
        if (text === null) text = state.src.slice(textStart, textEnd);
        state.appendToken(buildLinkToken(inline, isImage, text, attrs!, state));
        return pos2;
      }
      if (scanEnd > bodyEndPos) {
        if (isImage) markNoImageBefore(state, scanEnd);
        else markNoLinkBefore(state, scanEnd);
      }
    } else if (c === "[") {
      const [label2, pos2] = parseLinkLabel(state.src, endPos! + 1);
      if (pos2) {
        endPos = pos2;
        if (label2) label = label2;
      }
    }
  }

  const refLinks = state.env.refLinks;
  if (label === null) {
    if (refLinks.size === 0) {
      markNoLinkBefore(state, bodyEndPos);
      return null;
    }
    if (text === null) text = state.src.slice(textStart, textEnd);
    label = text;
  }
  if (refLinks.size === 0) {
    markNoLinkBefore(state, bodyEndPos);
    return null;
  }
  const key = unikey(label);
  const ref = refLinks.get(key);
  if (ref) {
    if (text === null) text = state.src.slice(textStart, textEnd);
    const attrs: LinkAttrs = { url: ref.url };
    if (ref.title !== undefined) attrs.title = ref.title;
    const token = buildLinkToken(inline, isImage, text, attrs, state);
    token.ref = key;
    token.label = label;
    state.appendToken(token);
    return endPos!;
  }
  markNoLinkBefore(state, bodyEndPos);
  return null;
}

function buildLinkToken(inline: InlineParser, isImage: boolean, text: string, attrs: LinkAttrs, state: InlineState): Token {
  const child = state.copy();
  child.src = text;
  if (isImage) {
    child.inImage = true;
    child.imageDepth += 1;
    return { type: "image", children: inline.render(child), attrs: { ...attrs } };
  }
  child.inLink = true;
  return { type: "link", children: inline.render(child), attrs: { ...attrs } };
}

function markNoLinkBefore(state: InlineState, endPos: number): void {
  if (endPos > state.noLinkBefore) state.noLinkBefore = endPos;
}

function markNoImageBefore(state: InlineState, endPos: number): void {
  if (endPos > state.noImageBefore) state.noImageBefore = endPos;
}

function labelContainsLink(state: InlineState, start: number, end: number): boolean {
  if (start >= end) return false;
  const [starts, suffixMinEnds] = linkRangeIndex(state);
  const index = bisectLeft(starts, start);
  return index < starts.length && starts[index]! < end && suffixMinEnds[index]! <= end;
}

function bisectLeft(a: number[], x: number): number {
  let lo = 0;
  let hi = a.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (a[mid]! < x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function linkRangeIndex(state: InlineState): [number[], number[]] {
  const cached = state.linkRanges.get(state.src);
  if (cached) return cached;
  const ranges: [number, number][] = [];
  for (const [labelStart, closePos] of closingBracketMap(state)) {
    const opener = labelStart - 1;
    if (opener > 0 && state.src[opener - 1] === "!") continue;
    const linkEnd = findLinkRangeEnd(state.src, labelStart, closePos, state);
    if (linkEnd !== null) ranges.push([opener, linkEnd]);
  }
  ranges.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const starts = ranges.map((r) => r[0]);
  const suffixMinEnds = new Array<number>(ranges.length).fill(0);
  let minEnd = state.src.length + 1;
  for (let i = ranges.length - 1; i >= 0; i--) {
    const end = ranges[i]![1];
    if (end < minEnd) minEnd = end;
    suffixMinEnds[i] = minEnd;
  }
  const result: [number[], number[]] = [starts, suffixMinEnds];
  state.linkRanges.set(state.src, result);
  return result;
}

function closingBracketMap(state: InlineState): Map<number, number> {
  const cached = state.linkBrackets.get(state.src);
  if (cached) return cached;
  const pairs = buildClosingBracketMap(state.src);
  state.linkBrackets.set(state.src, pairs);
  return pairs;
}

function findLinkRangeEnd(src: string, labelStart: number, closePos: number, state: InlineState): number | null {
  const endPos = closePos + 1;
  const refLinks = state.env.refLinks;
  if (endPos < src.length) {
    const marker = src[endPos];
    if (marker === "(") {
      const [, newPos] = parseLinkDestination(src, endPos + 1);
      return newPos;
    }
    if (marker === "[") {
      const [label, newPos] = parseLinkLabel(src, endPos + 1);
      if (!newPos) return null;
      const refLabel = label || src.slice(labelStart, closePos);
      if (refLinks.size && refLinks.has(unikey(refLabel))) return newPos;
      return null;
    }
  }
  if (refLinks.size && refLinks.has(unikey(src.slice(labelStart, closePos)))) return endPos;
  return null;
}

function buildClosingBracketMap(src: string): Map<number, number> {
  const pairs = new Map<number, number>();
  const stack: number[] = [];
  let pos = 0;
  while (pos < src.length) {
    const c = src[pos];
    if (c === "\\") {
      pos += 2;
      continue;
    }
    if (c === "[") stack.push(pos + 1);
    else if (c === "]" && stack.length) pairs.set(stack.pop()!, pos);
    pos += 1;
  }
  return pairs;
}

export type { RefLink };
