// mistune.inline_parser with the strikethrough plugin: a rule scanner over
// the trigger characters, feeding the emphasis finalizer.
import { PUNCTUATION_CLASS, cpAt, pyStrip, pyre } from "./chars";
import { finalizeEmphasisTokens, isEntityBoundary } from "./emphasis";
import { parseInlineLink, unescapeChar } from "./links";
import type { Env, Token } from "./tokens";
import { escapeUrl } from "./url";

const HTML_TAGNAME = "[A-Za-z][A-Za-z0-9-]*";
const HTML_ATTRIBUTES =
  "(?:\\s+[A-Za-z_:][A-Za-z0-9_.:-]*" + "(?:\\s*=\\s*(?:[^ !\"'=<>`]+|'[^']*?'|\"[^\"]*?\"))?)*";

const AUTO_EMAIL =
  "<[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9]" +
  "(?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?" +
  "(?:\\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*>";

const INLINE_HTML =
  "<" + HTML_TAGNAME + HTML_ATTRIBUTES + "\\s*/?>|" +
  "</" + HTML_TAGNAME + "\\s*>|" +
  "<!--(?!>|->)(?:(?!--)[\\s\\S])+?(?<!-)-->|" +
  "<\\?[\\s\\S]+?\\?>|" +
  "<![A-Z][\\s\\S]+?>|" +
  "<!\\[CDATA[\\s\\S]+?\\]\\]>";

const SPECIFICATION: Record<string, string> = {
  escape: "(?:\\\\" + PUNCTUATION_CLASS + ")+",
  codespan: "`{1,}",
  emphasis: "\\*{1,3}(?=[^\\s*])|(?<![\\p{L}\\p{N}_])_{1,3}(?=[^\\s_])",
  strikethrough: "~~(?=[^\\s~])",
  link: "!?\\[",
  auto_link: "<[A-Za-z][A-Za-z0-9.+-]{1,31}:[^<>\\x00-\\x20]*>",
  auto_email: AUTO_EMAIL,
  inline_html: INLINE_HTML,
  linebreak: "(?:\\\\| {2,})\\n\\s*",
  softbreak: " *\\n\\s*",
  prec_auto_link: "<[A-Za-z][A-Za-z\\d.+-]{1,31}:",
  prec_inline_html: "</?" + HTML_TAGNAME + "|<!|<\\?",
};

const RULES = [
  "escape",
  "codespan",
  "emphasis",
  "strikethrough",
  "link",
  "auto_link",
  "auto_email",
  "inline_html",
  "linebreak",
  "softbreak",
];

interface Rule {
  name: string;
  sticky: RegExp;
  global: RegExp;
}

const COMPILED = new Map<string, Rule>();
for (const name of Object.keys(SPECIFICATION)) {
  const p = SPECIFICATION[name]!;
  COMPILED.set(name, { name, sticky: pyre(p, "y"), global: pyre(p, "g") });
}

export interface RuleMatch {
  name: string;
  text: string;
  start: number;
  end: number;
}

function matchAt(rules: string[], src: string, pos: number): RuleMatch | null {
  for (const name of rules) {
    const re = COMPILED.get(name)!.sticky;
    re.lastIndex = pos;
    const m = re.exec(src);
    if (m) return { name, text: m[0], start: pos, end: pos + m[0].length };
  }
  return null;
}

function searchIn(rules: string[], src: string, pos: number): RuleMatch | null {
  let best: RuleMatch | null = null;
  for (const name of rules) {
    const re = COMPILED.get(name)!.global;
    re.lastIndex = pos;
    const m = re.exec(src);
    if (m && (best === null || m.index < best.start)) {
      best = { name, text: m[0], start: m.index, end: m.index + m[0].length };
    }
  }
  return best;
}

export class InlineState {
  env: Env;
  src = "";
  tokens: Token[] = [];
  inImage = false;
  imageDepth = 0;
  inLink = false;
  noCloseBracketBefore = 0;
  noLinkBefore = 0;
  noImageBefore = 0;
  linkBrackets: Map<string, Map<number, number>>;
  linkRanges: Map<string, [number[], number[]]>;
  formattingNoEnd: Map<string, number>;

  constructor(env: Env) {
    this.env = env;
    this.linkBrackets = new Map();
    this.linkRanges = new Map();
    this.formattingNoEnd = new Map();
  }

  appendToken(token: Token): void {
    this.tokens.push(token);
  }

  copy(): InlineState {
    const state = new InlineState(this.env);
    state.inImage = this.inImage;
    state.imageDepth = this.imageDepth;
    state.inLink = this.inLink;
    state.linkBrackets = this.linkBrackets;
    state.linkRanges = this.linkRanges;
    state.formattingNoEnd = this.formattingNoEnd;
    return state;
  }
}

const TRIGGER = /[\\`*_~![<\n]/u;
const MAX_EMPHASIS_DEPTH = 20;

export class InlineParser {
  readonly maxImageDepth = 20;

  parse(state: InlineState): Token[] {
    const src = state.src;
    let pos = 0;
    while (pos < src.length) {
      const fastEnd = this.findFastTextEnd(src, pos);
      if (fastEnd > pos) {
        this.processText(src.slice(pos, fastEnd), state);
        pos = fastEnd;
      }
      if (pos >= src.length) break;
      const m = matchAt(RULES, src, pos);
      if (!m) {
        const c = cpAt(src, pos);
        this.processText(c, state);
        pos += c.length;
        continue;
      }
      const newPos = this.parseMethod(m, state);
      if (!newPos) {
        this.processText(src.slice(m.start, m.start + 1), state);
        pos = m.start + 1;
      } else pos = newPos;
    }
    if (pos === 0) this.processText(src, state);
    else if (pos < src.length) this.processText(src.slice(pos), state);
    state.tokens = finalizeEmphasisTokens(state.tokens, true, MAX_EMPHASIS_DEPTH);
    return state.tokens;
  }

  render(state: InlineState): Token[] {
    return this.parse(state);
  }

  call(s: string, env: Env): Token[] {
    const state = new InlineState(env);
    state.src = s;
    return this.render(state);
  }

  private findFastTextEnd(src: string, pos: number): number {
    const idx = src.slice(pos).search(TRIGGER);
    if (idx < 0) return src.length;
    const at = pos + idx;
    if (src[at] === "\n") {
      let p = at;
      while (p > pos && src[p - 1] === " ") p--;
      if (p === at && p > pos && src[p - 1] === "\\") return p - 1;
      return p;
    }
    return at;
  }

  private parseMethod(m: RuleMatch, state: InlineState): number | null {
    switch (m.name) {
      case "escape":
        this.processText(unescapeChar(m.text), state, false);
        return m.end;
      case "codespan":
        return this.parseCodespan(m, state);
      case "emphasis":
        if (m.text.length === 1) state.appendToken({ type: "text", raw: m.text });
        else this.processText(m.text, state);
        return m.end;
      case "strikethrough":
        return this.parseToEnd(m, state, "strikethrough", "~~");
      case "link":
        return parseInlineLink(this, m, state);
      case "auto_link":
        if (state.inLink) {
          this.processText(m.text, state);
          return m.end;
        }
        this.addAutoLink(m.text.slice(1, -1), m.text.slice(1, -1), state);
        return m.end;
      case "auto_email":
        if (state.inLink) {
          this.processText(m.text, state);
          return m.end;
        }
        this.addAutoLink("mailto:" + m.text.slice(1, -1), m.text.slice(1, -1), state);
        return m.end;
      case "inline_html": {
        const html = m.text;
        state.appendToken({ type: "inline_html", raw: html });
        if (/^<[aA](?:[ >])/u.test(html)) state.inLink = true;
        else if (/^<\/[aA](?:[ >])/u.test(html)) state.inLink = false;
        return m.end;
      }
      case "linebreak":
        state.appendToken({ type: "linebreak" });
        return m.end;
      case "softbreak":
        state.appendToken({ type: "softbreak" });
        return m.end;
      default:
        return null;
    }
  }

  private addAutoLink(url: string, text: string, state: InlineState): void {
    state.appendToken({ type: "link", children: [{ type: "text", raw: text }], attrs: { url: escapeUrl(url) } });
  }

  private parseCodespan(m: RuleMatch, state: InlineState): number {
    const marker = m.text;
    const pattern = new RegExp("([^]*?[^`])" + marker + "(?!`)", "yu");
    pattern.lastIndex = m.end;
    const m2 = pattern.exec(state.src);
    if (m2) {
      let code = m2[1]!.replace(/\n/g, " ");
      if (pyStrip(code).length && code.startsWith(" ") && code.endsWith(" ")) code = code.slice(1, -1);
      state.appendToken({ type: "codespan", raw: code });
      return m.end + m2[0].length;
    }
    state.appendToken({ type: "text", raw: marker });
    return m.end;
  }

  // plugins.formatting._parse_to_end for a two-character marker.
  private parseToEnd(m: RuleMatch, state: InlineState, type: string, marker: string): number | null {
    const pos = m.end;
    const cacheKey = state.src + "\u0000" + marker;
    const noEnd = state.formattingNoEnd.get(cacheKey);
    if (noEnd !== undefined && pos <= noEnd) return null;
    const endPos = findEndMarker(state.src, pos, marker);
    if (endPos === null) {
      state.formattingNoEnd.set(cacheKey, state.src.length);
      return null;
    }
    const child = state.copy();
    child.src = state.src.slice(pos, endPos - 2);
    state.appendToken({ type, children: this.render(child) });
    return endPos;
  }

  processText(text: string, state: InlineState, parseEmphasis = true): void {
    const last = state.tokens[state.tokens.length - 1];
    if (parseEmphasis && last && last.type === "text" && last.emphasis !== false && !isEntityBoundary(last.raw!, text)) {
      last.raw += text;
      return;
    }
    const token: Token = { type: "text", raw: text };
    if (!parseEmphasis) token.emphasis = false;
    state.appendToken(token);
  }

  precedenceScan(m: RuleMatch, state: InlineState, endPos: number, rules: string[]): number | null {
    const m1 = searchIn(rules, state.src.slice(0, endPos), m.end);
    if (!m1) return null;
    const ruleName = m1.name.replace("prec_", "");
    const m2 = matchAt([ruleName], state.src, m1.start);
    if (!m2) return null;
    const child = state.copy();
    child.src = state.src;
    const m2Pos = this.parseMethod(m2, child);
    if (!m2Pos || m2Pos < endPos) return null;
    state.appendToken({ type: "text", raw: state.src.slice(m.start, m2.start) });
    for (const token of child.tokens) state.appendToken(token);
    return m2Pos;
  }
}

function findEndMarker(src: string, pos: number, marker: string): number | null {
  const c = marker[0]!;
  let end = src.indexOf(marker, pos);
  while (end !== -1) {
    const markerEnd = end + marker.length;
    if (markerEnd < src.length && src[markerEnd] === c && !src.startsWith(marker, markerEnd)) {
      end = src.indexOf(marker, end + 1);
      continue;
    }
    if (end > pos) {
      const prev = src[end - 1]!;
      const escapedMarkerBefore = prev === c && end >= pos + 2 && src[end - 2] === "\\" && !hasOddBackslashes(src, end - 2);
      if ((!isSpaceChar(prev) && prev !== c) || escapedMarkerBefore) return markerEnd;
    }
    end = src.indexOf(marker, end + 1);
  }
  return null;
}

function isSpaceChar(c: string): boolean {
  return pyStrip(c) === "";
}

function hasOddBackslashes(src: string, pos: number): boolean {
  let count = 0;
  pos -= 1;
  while (pos >= 0 && src[pos] === "\\") {
    count++;
    pos--;
  }
  return count % 2 === 1;
}
