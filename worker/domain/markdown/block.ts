// The block scanner: containers, the block rules and the table rules.
import { isAsciiPunct, isDigit, isSpace, cpAt, trim, unicodeRe, ASCII_WHITESPACE } from "./chars";
import { LINK_LABEL, parseLinkHref, parseLinkTitle, unescapeChar, refKey } from "./links";
import { LIST_PATTERN, parseList } from "./list";
import { NP_TABLE_PATTERN, TABLE_PATTERN, parseNpTable, parseTable } from "./table";
import { type Env, type Token, newEnv } from "./tokens";
import { escapeUrl } from "./url";

const MAX_NESTED_LEVEL = 20;

const HTML_TAGNAME = "[A-Za-z][A-Za-z0-9-]*";
const HTML_ATTRIBUTES =
  "(?:\\s+[A-Za-z_:][A-Za-z0-9_.:-]*" + "(?:\\s*=\\s*(?:[^ !\"'=<>`]+|'[^']*?'|\"[^\"]*?\"))?)*";
const BLOCK_TAGS = new Set(
  (
    "address article aside base basefont blockquote body caption center col colgroup dd details dialog dir div dl " +
    "dt fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hr html iframe legend " +
    "li link main menu menuitem meta nav noframes ol optgroup option p param section source summary table tbody " +
    "td tfoot th thead title tr track ul"
  ).split(" "),
);
const PRE_TAGS = new Set(["pre", "script", "style", "textarea"]);
const BLOCK_TAGS_PATTERN = "(" + [...BLOCK_TAGS, ...PRE_TAGS].join("|") + ")";

const SPECIFICATION: Record<string, string> = {
  blank_line: "(^[ \\t\\v\\f]*\\n)+",
  atx_heading: "^ {0,3}(?<atx_1>#{1,6})(?!#+)(?<atx_2>[ \\t]*|[ \\t]+.*?)$",
  setex_heading: "^ {0,3}(?<setext_1>=|-){1,}[ \\t]*$",
  fenced_code: "^(?<fenced_1> {0,3})(?<fenced_2>`{3,}|~{3,})[ \\t]*(?<fenced_3>.*?)$",
  indent_code: "^(?: {4}| *\\t)[^\\n]+(?:\\n+|$)((?:(?: {4}| *\\t)[^\\n]+(?:\\n+|$))|\\s)*",
  thematic_break: "^ {0,3}((?:-[ \\t]*){3,}|(?:_[ \\t]*){3,}|(?:\\*[ \\t]*){3,})$",
  ref_link: "^ {0,3}\\[(?<reflink_1>" + LINK_LABEL + ")\\]:",
  block_quote: "^ {0,3}>(?<quote_1>.*?)$",
  list: LIST_PATTERN,
  block_html:
    "^ {0,3}(?:(?:</?" + BLOCK_TAGS_PATTERN + "(?:[ \\t]+|\\n|$))|<!--|<\\?|<![A-Z]|<!\\[CDATA\\[)",
  raw_html: "^ {0,3}(</?" + HTML_TAGNAME + "|<!--|<\\?|<![A-Z]|<!\\[CDATA\\[)",
  table: TABLE_PATTERN,
  nptable: NP_TABLE_PATTERN,
};

const DEFAULT_RULES = [
  "fenced_code",
  "indent_code",
  "atx_heading",
  "setex_heading",
  "thematic_break",
  "block_quote",
  "list",
  "ref_link",
  "raw_html",
  "blank_line",
];

export interface ScanMatch {
  name: string;
  text: string;
  start: number;
  end: number;
  groups: Record<string, string | undefined>;
}

interface Rule {
  name: string;
  sticky: RegExp;
  global: RegExp;
}

/** An ordered alternation of rules: first rule wins at a position. */
export class Scanner {
  constructor(private readonly rules: Rule[]) {}

  matchAt(src: string, pos: number): ScanMatch | null {
    for (const rule of this.rules) {
      rule.sticky.lastIndex = pos;
      const m = rule.sticky.exec(src);
      if (m) return { name: rule.name, text: m[0], start: pos, end: pos + m[0].length, groups: m.groups ?? {} };
    }
    return null;
  }

  search(src: string, pos: number): ScanMatch | null {
    let best: ScanMatch | null = null;
    for (const rule of this.rules) {
      rule.global.lastIndex = pos;
      const m = rule.global.exec(src);
      if (m && (best === null || m.index < best.start)) {
        best = { name: rule.name, text: m[0], start: m.index, end: m.index + m[0].length, groups: m.groups ?? {} };
      }
    }
    return best;
  }
}

const INDENT_CODE_TRIM = unicodeRe("^ {1,4}", "g", true);
const ATX_HEADING_TRIM = unicodeRe("(\\s+|^)#+\\s*$", "g");
const BLOCK_QUOTE_TRIM = unicodeRe("^ ?", "g", true);
const BLANK_TO_LINE = unicodeRe("[ \\t]*\\n", "y");
const BLOCK_QUOTE_LINE = unicodeRe("^ {0,3}>([^\\n]*(?:\\n|$))", "y", true);
const BLANK_LINE = unicodeRe("(^[ \\t\\v\\f]*\\n)+", "g", true);
const EXPAND_TAB = unicodeRe("^( {0,3})\\t", "g", true);
const OPEN_TAG_END = unicodeRe(HTML_ATTRIBUTES + "[ \\t]*>[ \\t]*(?:\\n|$)", "y");
const CLOSE_TAG_END = unicodeRe("[ \\t]*>[ \\t]*(?:\\n|$)", "y");

export function expandLeadingTab(text: string, width = 4): string {
  return text.replace(EXPAND_TAB, (_m, s: string) => s + " ".repeat(width - s.length));
}

function expandTab(text: string): string {
  return text.replace(EXPAND_TAB, "$1    ");
}

export class BlockState {
  src = "";
  tokens: Token[] = [];
  cursor = 0;
  cursorMax = 0;
  listTight = true;
  parent: BlockState | null;
  env: Env;
  lazyLineStarts = new Set<number>();

  constructor(parent: BlockState | null = null) {
    this.parent = parent;
    this.env = parent ? parent.env : newEnv();
  }

  childState(src: string, lazyLineStarts?: Set<number>): BlockState {
    const child = new BlockState(this);
    child.process(src);
    if (lazyLineStarts && lazyLineStarts.size) child.lazyLineStarts = lazyLineStarts;
    return child;
  }

  process(src: string): void {
    this.src = src;
    this.cursorMax = src.length;
  }

  findLineEnd(): number {
    return this.findLineEndAt(this.cursor);
  }

  findLineEndAt(pos: number): number {
    const i = this.src.indexOf("\n", pos);
    return i < 0 ? this.src.length : i + 1;
  }

  getText(endPos: number): string {
    return this.src.slice(this.cursor, endPos);
  }

  getLine(startPos: number): string {
    return this.src.slice(startPos, this.findLineEndAt(startPos));
  }

  lastToken(): Token | undefined {
    return this.tokens[this.tokens.length - 1];
  }

  prependToken(token: Token): void {
    this.tokens.splice(Math.max(0, this.tokens.length - 1), 0, token);
  }

  appendToken(token: Token): void {
    this.tokens.push(token);
  }

  addParagraph(text: string): void {
    const last = this.lastToken();
    if (last && last.type === "paragraph") last.text += text;
    else this.tokens.push({ type: "paragraph", text });
  }

  appendParagraph(): number | null {
    const last = this.lastToken();
    if (last && last.type === "paragraph") {
      const pos = this.findLineEnd();
      last.text += this.getText(pos);
      return pos;
    }
    return null;
  }

  depth(): number {
    let d = 0;
    let p = this.parent;
    while (p) {
      d++;
      p = p.parent;
    }
    return d;
  }
}

export class BlockParser {
  readonly rules: string[] = [...DEFAULT_RULES, "table", "nptable"];
  readonly blockQuoteRules: string[] = [...DEFAULT_RULES];
  readonly listRules: string[] = [...DEFAULT_RULES];
  readonly maxNestedLevel = MAX_NESTED_LEVEL;
  private readonly compiled = new Map<string, Rule>();
  private readonly scanners = new Map<string, Scanner>();
  private readonly breakScanners = new Map<number, Scanner>();

  private rule(name: string, pattern = SPECIFICATION[name]!): Rule {
    const key = name + "\0" + pattern;
    let r = this.compiled.get(key);
    if (!r) {
      r = { name, sticky: unicodeRe(pattern, "y", true), global: unicodeRe(pattern, "g", true) };
      this.compiled.set(key, r);
    }
    return r;
  }

  compileSc(rules: string[] = this.rules): Scanner {
    const key = rules.join("|");
    let sc = this.scanners.get(key);
    if (!sc) {
      sc = new Scanner(rules.map((n) => this.rule(n)));
      this.scanners.set(key, sc);
    }
    return sc;
  }

  // list_parser._compile_list_break_sc: the rules that end a list item,
  // with the indent bound relaxed to the item's own width.
  listBreakScanner(width: number): Scanner {
    let sc = this.breakScanners.get(width);
    if (!sc) {
      const rules = ["thematic_break", "fenced_code", "atx_heading", "block_quote", "block_html", "list"].map((name) => {
        let p = SPECIFICATION[name]!;
        if (width < 3) p = p.replace(" {0,3}", " {0," + width + "}");
        return this.rule(name, "(?<=\\n)" + p);
      });
      sc = new Scanner(rules);
      this.breakScanners.set(width, sc);
    }
    return sc;
  }

  parseMethod(m: ScanMatch, state: BlockState): number | null {
    switch (m.name) {
      case "blank_line":
        state.appendToken({ type: "blank_line" });
        return m.end;
      case "thematic_break":
        state.appendToken({ type: "thematic_break" });
        return m.end + 1;
      case "indent_code":
        return this.parseIndentCode(m, state);
      case "fenced_code":
        return this.parseFencedCode(m, state);
      case "atx_heading":
        return this.parseAtxHeading(m, state);
      case "setex_heading":
        return this.parseSetexHeading(m, state);
      case "ref_link":
        return this.parseRefLink(m, state);
      case "block_quote":
        return this.parseBlockQuote(m, state);
      case "list":
        return parseList(this, m, state);
      case "block_html":
      case "raw_html":
        return this.parseRawHtml(m, state);
      case "table":
        return parseTable(m, state);
      case "nptable":
        return parseNpTable(m, state);
      default:
        return null;
    }
  }

  parse(state: BlockState, rules?: string[]): void {
    const sc = this.compileSc(rules);
    while (state.cursor < state.cursorMax) {
      let m = sc.matchAt(state.src, state.cursor);
      if (!m && this.parsePlainParagraph(state, sc)) continue;
      if (!m) m = sc.search(state.src, state.cursor);
      if (!m) break;
      const endPos = m.start;
      if (endPos > state.cursor) {
        state.addParagraph(state.getText(endPos));
        state.cursor = endPos;
      }
      const endPos2 = this.parseMethod(m, state);
      if (endPos2) state.cursor = endPos2;
      else {
        const endPos3 = state.findLineEnd();
        state.addParagraph(state.getText(endPos3));
        state.cursor = endPos3;
      }
    }
    if (state.cursor < state.cursorMax) {
      state.addParagraph(state.src.slice(state.cursor));
      state.cursor = state.cursorMax;
    }
  }

  private parsePlainParagraph(state: BlockState, sc: Scanner): boolean {
    if (!isPlainParagraphStart(state.src, state.cursor)) return false;
    let pos = state.cursor;
    while (pos < state.cursorMax) {
      if (pos > state.cursor && sc.matchAt(state.src, pos)) break;
      const line = state.getLine(pos);
      if (!trim(line)) break;
      pos += line.length;
    }
    if (pos <= state.cursor) return false;
    state.addParagraph(state.getText(pos));
    state.cursor = pos;
    return true;
  }

  private parseIndentCode(m: ScanMatch, state: BlockState): number {
    const para = state.appendParagraph();
    if (para) return para;
    let code = m.text;
    const endPos = trimPartialNextLineIndent(code, m.end);
    if (endPos !== m.end) code = state.getText(endPos);
    code = expandLeadingTab(code);
    code = code.replace(INDENT_CODE_TRIM, "");
    code = trim(code, "\n");
    state.appendToken({ type: "block_code", raw: code, style: "indent" });
    return endPos;
  }

  private parseFencedCode(m: ScanMatch, state: BlockState): number | null {
    const spaces = m.groups.fenced_1!;
    const marker = m.groups.fenced_2!;
    let info = m.groups.fenced_3!;
    const c = marker[0]!;
    // An info string on a backtick fence cannot contain a backtick.
    if (info && c === "`" && info.includes(c)) return null;
    const end = unicodeRe("^ {0,3}" + c + "{" + marker.length + ",}[ \\t]*(?:\\n|$)", "g", true);
    const cursorStart = m.end + 1;
    let code: string;
    let endPos: number;
    end.lastIndex = cursorStart;
    const m2 = cursorStart <= state.src.length ? end.exec(state.src) : null;
    if (m2) {
      code = state.src.slice(cursorStart, m2.index);
      endPos = m2.index + m2[0].length;
    } else {
      code = state.src.slice(cursorStart);
      endPos = state.cursorMax;
    }
    if (spaces && code) code = code.replace(unicodeRe("^ {0," + spaces.length + "}", "g", true), "");
    const token: Token = { type: "block_code", raw: code, style: "fenced", marker };
    if (info) {
      info = unescapeChar(info);
      token.attrs = { info: trim(info) };
    }
    state.appendToken(token);
    return endPos;
  }

  private parseAtxHeading(m: ScanMatch, state: BlockState): number {
    const level = m.groups.atx_1!.length;
    let text = trim(m.groups.atx_2!, ASCII_WHITESPACE);
    if (text) text = text.replace(ATX_HEADING_TRIM, "");
    state.appendToken({ type: "heading", text, attrs: { level }, style: "atx" });
    return m.end + 1;
  }

  private parseSetexHeading(m: ScanMatch, state: BlockState): number | null {
    if (state.lazyLineStarts.has(state.cursor)) return null;
    const last = state.lastToken();
    if (last && last.type === "paragraph") {
      last.type = "heading";
      last.style = "setext";
      last.attrs = { level: m.groups.setext_1 === "=" ? 1 : 2 };
      return m.end + 1;
    }
    const m2 = this.compileSc(["thematic_break", "list"]).matchAt(state.src, state.cursor);
    if (m2) return this.parseMethod(m2, state);
    return null;
  }

  private parseRefLink(m: ScanMatch, state: BlockState): number | null {
    const para = state.appendParagraph();
    if (para) return para;
    const label = m.groups.reflink_1!;
    const key = refKey(label);
    if (!key) return null;
    let [href, hrefPos] = parseLinkHref(state.src, m.end, true);
    if (href === null) return null;
    const blankPos = findNextBlankLine(state, hrefPos!);
    const maxPos = blankPos === null ? state.cursorMax : blankPos;
    let [title, titlePos] = parseLinkTitle(state.src, hrefPos!, maxPos);
    if (titlePos) {
      BLANK_TO_LINE.lastIndex = titlePos;
      const m2 = BLANK_TO_LINE.exec(state.src);
      if (m2) titlePos = titlePos + m2[0].length;
      else {
        titlePos = null;
        title = null;
      }
    }
    let hrefEnd: number | null = hrefPos;
    if (titlePos === null) {
      BLANK_TO_LINE.lastIndex = hrefPos!;
      const m3 = BLANK_TO_LINE.exec(state.src);
      if (m3) hrefEnd = hrefPos! + m3[0].length;
      else {
        hrefEnd = null;
        href = null;
      }
    }
    const endPos = titlePos || hrefEnd;
    if (!endPos) return null;
    if (!state.env.refLinks.has(key)) {
      const url = escapeUrl(unescapeChar(href!));
      state.env.refLinks.set(key, title ? { url, label, title } : { url, label });
    }
    return endPos;
  }

  private extractBlockQuote(state: BlockState): [string, number | null, Set<number>] {
    let text = parseBlockQuoteLine(state.getLine(state.cursor))!;
    const lazyLineStarts = new Set<number>();
    const requireMarker = this.compileSc(["blank_line", "indent_code", "fenced_code"]).matchAt(text, 0) !== null;
    state.cursor += state.getLine(state.cursor).length;
    let endPos: number | null = null;
    if (requireMarker) {
      while (state.cursor < state.cursorMax) {
        const quote = parseBlockQuoteLine(state.getLine(state.cursor));
        if (quote === null) break;
        text += quote;
        state.cursor += state.getLine(state.cursor).length;
      }
    } else {
      let prevBlankLine = false;
      const breakSc = this.compileSc(["blank_line", "thematic_break", "fenced_code", "list", "block_html"]);
      while (state.cursor < state.cursorMax) {
        const quote = parseBlockQuoteLine(state.getLine(state.cursor));
        if (quote !== null) {
          text += quote;
          state.cursor += state.getLine(state.cursor).length;
          prevBlankLine = !trim(quote);
          continue;
        }
        // A blank line is required between a quote and a following paragraph.
        if (prevBlankLine) break;
        const m4 = breakSc.matchAt(state.src, state.cursor);
        if (m4) {
          endPos = this.parseMethod(m4, state);
          if (endPos) break;
        }
        const line = state.getLine(state.cursor);
        lazyLineStarts.add(text.length);
        text += expandLeadingTab(line, 3);
        state.cursor += line.length;
      }
    }
    return [expandTab(text), endPos, lazyLineStarts];
  }

  private parseBlockQuote(_m: ScanMatch, state: BlockState): number {
    const [text, endPos, lazyLineStarts] = this.extractBlockQuote(state);
    const child = state.childState(text, lazyLineStarts);
    const rules =
      state.depth() >= this.maxNestedLevel - 1
        ? this.blockQuoteRules.filter((r) => r !== "block_quote" && r !== "list")
        : this.blockQuoteRules;
    this.parse(child, rules);
    const token: Token = { type: "block_quote", children: child.tokens };
    if (endPos) {
      state.prependToken(token);
      return endPos;
    }
    state.appendToken(token);
    return state.cursor;
  }

  private parseRawHtml(m: ScanMatch, state: BlockState): number | null {
    const marker = trim(m.text);
    if (marker === "<!--") return parseHtmlToEnd(state, "-->", m.end);
    if (marker === "<?") return parseHtmlToEnd(state, "?>", m.end);
    if (marker === "<![CDATA[") return parseHtmlToEnd(state, "]]>", m.end);
    if (marker.startsWith("<!")) return parseHtmlToEnd(state, ">", m.end);
    let closeTag: string | null = null;
    let openTag: string | null = null;
    if (marker.startsWith("</")) {
      closeTag = marker.slice(2).toLowerCase();
      if (BLOCK_TAGS.has(closeTag)) return parseHtmlToNewline(state);
    } else {
      openTag = marker.slice(1).toLowerCase();
      if (PRE_TAGS.has(openTag)) return parseHtmlToEnd(state, "</" + openTag + ">", m.end);
      if (BLOCK_TAGS.has(openTag)) return parseHtmlToNewline(state);
    }
    // Type 7 blocks may not interrupt a paragraph.
    const para = state.appendParagraph();
    if (para) return para;
    const line = state.src.slice(0, state.findLineEnd());
    OPEN_TAG_END.lastIndex = m.end;
    CLOSE_TAG_END.lastIndex = m.end;
    if ((openTag && OPEN_TAG_END.exec(line)) || (closeTag && CLOSE_TAG_END.exec(line))) return parseHtmlToNewline(state);
    return null;
  }
}

function parseHtmlToEnd(state: BlockState, endMarker: string, startPos: number): number {
  const markerPos = state.src.indexOf(endMarker, startPos);
  let text: string;
  let endPos: number;
  if (markerPos === -1) {
    text = state.src.slice(state.cursor);
    endPos = state.cursorMax;
  } else {
    text = state.getText(markerPos);
    state.cursor = markerPos;
    endPos = state.findLineEnd();
    text += state.getText(endPos);
  }
  state.appendToken({ type: "block_html", raw: text });
  return endPos;
}

function parseHtmlToNewline(state: BlockState): number {
  BLANK_LINE.lastIndex = state.cursor;
  const m = BLANK_LINE.exec(state.src);
  let text: string;
  let endPos: number;
  if (m) {
    endPos = m.index;
    text = state.getText(endPos);
  } else {
    text = state.src.slice(state.cursor);
    endPos = state.cursorMax;
  }
  state.appendToken({ type: "block_html", raw: text });
  return endPos;
}

function parseBlockQuoteLine(line: string): string | null {
  BLOCK_QUOTE_LINE.lastIndex = 0;
  const m = BLOCK_QUOTE_LINE.exec(line);
  if (!m) return null;
  return expandLeadingTab(m[1]!, 3).replace(BLOCK_QUOTE_TRIM, "");
}

function findNextBlankLine(state: BlockState, pos: number): number | null {
  let cache = state.env.blankLineStarts;
  if (!cache || cache.src !== state.src) {
    const starts: number[] = [];
    BLANK_LINE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = BLANK_LINE.exec(state.src))) starts.push(m.index);
    cache = { src: state.src, starts };
    state.env.blankLineStarts = cache;
  }
  for (const start of cache.starts) if (start >= pos) return start;
  return null;
}

function trimPartialNextLineIndent(text: string, endPos: number): number {
  const lineStart = text.lastIndexOf("\n") + 1;
  if (lineStart === 0) return endPos;
  const suffix = text.slice(lineStart);
  if (suffix && trim(suffix, " \t") === "" && expandTabsWidth(suffix) < 4) return endPos - suffix.length;
  return endPos;
}

function expandTabsWidth(s: string): number {
  let col = 0;
  for (const c of s) col += c === "\t" ? 4 - (col % 4) : 1;
  return col;
}

function isPlainParagraphStart(src: string, pos: number): boolean {
  if (pos >= src.length) return false;
  const c = cpAt(src, pos);
  return !isSpace(c) && !isDigit(c) && !isAsciiPunct(c);
}
