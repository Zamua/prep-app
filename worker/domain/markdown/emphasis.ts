// mistune._inline.emphasis: the CommonMark delimiter-run algorithm over
// the flat inline token list.
import { cpAt, cpBefore, isAlnum, isSpace } from "./chars";
import type { Token } from "./tokens";
import { CHARREF_PREFIX } from "./url";

export function isEntityBoundary(left: string, right: string): boolean {
  return left.endsWith("&") && CHARREF_PREFIX.test(right);
}

interface Delimiter {
  index: number;
  marker: string;
  length: number;
  canOpen: boolean;
  canClose: boolean;
  origLength: number;
  order: number;
}

// Part positions after splices, without rewriting every delimiter.
class DelimiterIndex {
  private readonly tree: number[];
  private readonly next: number[];

  constructor(delimiters: Delimiter[], partCount: number) {
    this.tree = new Array<number>(partCount + 1).fill(0);
    this.next = Array.from({ length: delimiters.length + 1 }, (_v, i) => i);
  }

  current(d: Delimiter): number {
    let total = 0;
    let cursor = d.index + 1;
    while (cursor) {
      total += this.tree[cursor]!;
      cursor -= cursor & -cursor;
    }
    return d.index - total;
  }

  collapse(closer: Delimiter, removed: number): void {
    if (!removed) return;
    let cursor = closer.index + 1;
    while (cursor < this.tree.length) {
      this.tree[cursor]! += removed;
      cursor += cursor & -cursor;
    }
  }

  deactivate(order: number): void {
    this.next[order] = this.find(order + 1);
  }

  deactivateRange(delimiters: Delimiter[], start: number, end: number): void {
    let order = this.find(start);
    while (order < end) {
      delimiters[order]!.length = 0;
      this.next[order] = this.find(order + 1);
      order = this.find(order);
    }
  }

  private find(order: number): number {
    let root = order;
    while (this.next[root] !== root) root = this.next[root]!;
    while (this.next[order] !== order) {
      const parent = this.next[order]!;
      this.next[order] = root;
      order = parent;
    }
    return root;
  }
}

export function finalizeEmphasisTokens(tokens: Token[], enabled: boolean, maxDepth: number): Token[] {
  if (!enabled || !containsEmphasisMarker(tokens)) return tokens.map(cleanToken);
  const parts: Token[] = [];
  const delimiters: Delimiter[] = [];
  const source = emphasisSourceText(tokens);
  let sourcePos = 0;
  for (const token of tokens) {
    if (token.type === "text" && token.emphasis !== false) splitTextToken(token, source, sourcePos, parts, delimiters);
    else parts.push(cleanToken(token));
    sourcePos += token.type === "text" ? token.raw!.length : 1;
  }
  if (processDenseEmphasis(parts, delimiters)) return mergeTextTokens(parts);
  processEmphasisDelimiters(parts, delimiters, maxDepth);
  return mergeTextTokens(parts);
}

// A flat run such as `*a*a*a*a`, without repeated list scans.
function processDenseEmphasis(parts: Token[], delimiters: Delimiter[]): boolean {
  if (delimiters.length < 4) return false;
  const marker = delimiters[0]!.marker;
  if ((marker !== "*" && marker !== "_") || parts.length !== delimiters.length * 2) return false;
  for (let index = 0; index < delimiters.length; index++) {
    const d = delimiters[index]!;
    if (
      d.marker !== marker ||
      d.length !== 1 ||
      d.index !== index * 2 ||
      parts[d.index]!.type !== "text" ||
      parts[d.index + 1]!.type !== "text"
    ) {
      return false;
    }
  }
  const pairCount = Math.floor(delimiters.length / 2);
  const processed: Token[] = [];
  for (let pair = 0; pair < pairCount; pair++) {
    const opener = delimiters[pair * 2]!;
    const closer = delimiters[pair * 2 + 1]!;
    if (
      !opener.canOpen ||
      !closer.canClose ||
      !canMatchDelimiters(opener, closer) ||
      !hasEmphasisContent(parts, opener.index + 1, closer.index)
    ) {
      return false;
    }
    if (pair) processed.push(parts[opener.index - 1]!);
    processed.push({ type: "emphasis", children: [parts[opener.index + 1]!] });
  }
  if (pairCount) {
    const lastClose = delimiters[pairCount * 2 - 1]!.index;
    parts.splice(0, parts.length, ...processed, ...parts.slice(lastClose + 1));
  }
  return true;
}

function containsEmphasisMarker(tokens: Token[]): boolean {
  return tokens.some(
    (t) => t.type === "text" && t.emphasis !== false && (t.raw!.includes("*") || t.raw!.includes("_")),
  );
}

function cleanToken(token: Token): Token {
  if (token.emphasis === undefined) return token;
  const copy = { ...token };
  delete copy.emphasis;
  return copy;
}

function emphasisSourceText(tokens: Token[]): string {
  let out = "";
  for (const t of tokens) {
    if (t.type === "text") out += t.raw!;
    else if (t.type === "softbreak" || t.type === "linebreak") out += "\n";
    else out += "￼";
  }
  return out;
}

function splitTextToken(token: Token, source: string, sourceStart: number, parts: Token[], delimiters: Delimiter[]): void {
  const text = token.raw!;
  let pos = 0;
  while (pos < text.length) {
    const c = text[pos]!;
    if (c !== "*" && c !== "_") {
      let end = pos;
      while (end < text.length && text[end] !== "*" && text[end] !== "_") end++;
      parts.push({ type: "text", raw: text.slice(pos, end) });
      pos = end;
      continue;
    }
    let end = pos;
    while (end < text.length && text[end] === c) end++;
    const length = end - pos;
    const absolute = sourceStart + pos;
    const canOpen = canOpenEmphasis(source, absolute, length, c);
    const canClose = canCloseEmphasis(source, absolute, length, c);
    const index = parts.length;
    parts.push({ type: "text", raw: text.slice(pos, end) });
    if (canOpen || canClose) {
      delimiters.push({ index, marker: c, length, canOpen, canClose, origLength: length, order: delimiters.length });
    }
    pos = end;
  }
}

function processEmphasisDelimiters(parts: Token[], delimiters: Delimiter[], maxDepth: number): void {
  const indexMap = new DelimiterIndex(delimiters, parts.length);
  let closerPos = 0;
  const openersBottom = new Map<string, number>();
  while (closerPos < delimiters.length) {
    const closer = delimiters[closerPos]!;
    if (!closer.canClose || closer.length === 0) {
      closerPos++;
      continue;
    }
    const openerKey = `${closer.marker}:${closer.length % 3}:${closer.canOpen}`;
    let openerPos = closerPos - 1;
    const openerBottom = openersBottom.get(openerKey) ?? 0;
    let opener: Delimiter | null = null;
    while (openerPos >= openerBottom) {
      const candidate = delimiters[openerPos]!;
      if (
        candidate.marker === closer.marker &&
        candidate.canOpen &&
        candidate.length > 0 &&
        canMatchDelimiters(candidate, closer)
      ) {
        opener = candidate;
        break;
      }
      openerPos--;
    }
    if (opener === null) {
      openersBottom.set(openerKey, closerPos);
      closerPos++;
      continue;
    }
    const openerIndex = indexMap.current(opener);
    const closerIndex = indexMap.current(closer);
    let useLength = opener.length >= 2 && closer.length >= 2 ? 2 : 1;
    if (useLength === 2 && !(textRaw(parts[openerIndex]!).length >= 2 && textRaw(parts[closerIndex]!).length >= 2)) {
      useLength = 1;
    }
    if (useLength === 1 && !(textRaw(parts[openerIndex]!) && textRaw(parts[closerIndex]!))) {
      closerPos++;
      continue;
    }
    if (!hasEmphasisContent(parts, openerIndex + 1, closerIndex)) {
      closerPos++;
      continue;
    }
    const openerText = parts[openerIndex]!;
    const closerText = parts[closerIndex]!;
    if (openerText.type !== "text" || closerText.type !== "text") {
      closerPos++;
      continue;
    }
    const children = parts.slice(openerIndex + 1, closerIndex);
    if (maxDepth > 0 && emphasisDepth(children) >= maxDepth) {
      closerPos++;
      continue;
    }
    openerText.raw = openerText.raw!.slice(0, -useLength);
    closerText.raw = closerText.raw!.slice(useLength);
    const node: Token = { type: useLength === 2 ? "strong" : "emphasis", children };
    parts.splice(openerIndex + 1, closerIndex - openerIndex - 1, node);
    const removed = closerIndex - openerIndex - 2;
    if (removed) {
      indexMap.deactivateRange(delimiters, openerPos + 1, closerPos);
      indexMap.collapse(closer, removed);
    }
    opener.length -= useLength;
    closer.length -= useLength;
    if (opener.length === 0) {
      opener.canOpen = false;
      indexMap.deactivate(opener.order);
    }
    if (closer.length === 0) {
      closer.canClose = false;
      indexMap.deactivate(closer.order);
    }
    if (opener.canOpen || closer.canClose) closerPos = Math.max(openerPos, openersBottom.get(openerKey) ?? 0);
    else closerPos++;
  }
}

function emphasisDepth(tokens: Token[]): number {
  let max = 0;
  const stack: [Token, number][] = tokens.map((t) => [t, 0]);
  while (stack.length) {
    const [token, d] = stack.pop()!;
    let depth = d;
    if (token.type === "emphasis" || token.type === "strong") {
      depth += 1;
      if (depth > max) max = depth;
    }
    for (const child of token.children ?? []) stack.push([child, depth]);
  }
  return max;
}

function textRaw(token: Token): string {
  return token.type === "text" ? token.raw! : "";
}

function hasEmphasisContent(parts: Token[], start: number, end: number): boolean {
  for (const part of parts.slice(start, end)) {
    if (part.type !== "text" || part.raw !== "") return true;
  }
  return false;
}

function canMatchDelimiters(opener: Delimiter, closer: Delimiter): boolean {
  if (opener.canClose || closer.canOpen) {
    const o = opener.origLength;
    const c = closer.origLength;
    return (o + c) % 3 !== 0 || (o % 3 === 0 && c % 3 === 0);
  }
  return true;
}

function isPunctuation(c: string): boolean {
  return !isSpace(c) && !isAlnum(c);
}

function canOpenEmphasis(text: string, start: number, size: number, marker: string): boolean {
  const previous = cpBefore(text, start) || "\n";
  const next = cpAt(text, start + size) || "\n";
  if (marker === "_" && isAlnum(previous) && isAlnum(next)) return false;
  if (isSpace(next)) return false;
  if (isPunctuation(next) && !isSpace(previous) && !isPunctuation(previous)) return false;
  return true;
}

function canCloseEmphasis(text: string, start: number, size: number, marker: string): boolean {
  const previous = cpBefore(text, start) || "\n";
  const next = cpAt(text, start + size) || "\n";
  if (marker === "_" && isAlnum(previous) && isAlnum(next)) return false;
  if (isSpace(previous)) return false;
  if (isPunctuation(previous) && !isSpace(next) && !isPunctuation(next)) return false;
  return true;
}

function mergeTextTokens(tokens: Token[]): Token[] {
  const result: Token[] = [];
  for (const token of tokens) {
    if (token.type === "text" && token.raw === "") continue;
    const last = result[result.length - 1];
    if (token.type === "text" && last && last.type === "text" && !isEntityBoundary(last.raw!, token.raw!)) {
      last.raw += token.raw!;
      continue;
    }
    result.push(cleanToken(token));
  }
  return result;
}
