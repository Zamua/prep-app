// mistune.list_parser: list items, continuation indents, tight and loose
// lists, and the break rules that end an item.
import { cpBefore, isSpace, pyStrip, pyre } from "./chars";
import type { BlockParser, BlockState, Scanner, ScanMatch } from "./block";
import type { Token } from "./tokens";

export const LIST_PATTERN = "^(?P<list_1> {0,3})(?P<list_2>[\\*\\+-]|\\d{1,9}[.)])(?P<list_3>[ \\t]*|[ \\t].+)$";

const LINE_HAS_TEXT = pyre("(\\s*)\\S", "y");
const BLANK_LINE = pyre("(^[ \\t\\v\\f]*\\n)+", "y", true);

interface ListMarker {
  spaces: string;
  marker: string;
  text: string;
}

interface ItemLines {
  src: string;
  nextItem: ListMarker | null;
  loose: boolean;
  endPos: number | null;
  tokenIndex: number | null;
}

function leadingWidth(item: ListMarker): number {
  return item.spaces.length + item.marker.length;
}

function createListMarker(groups: Record<string, string | undefined>, prefix: string): ListMarker {
  return { spaces: groups[prefix + "_1"]!, marker: groups[prefix + "_2"]!, text: groups[prefix + "_3"]! };
}

export function parseList(block: BlockParser, m: ScanMatch, state: BlockState): number | null {
  const item = createListMarker(m.groups, "list");
  const text = item.text;
  if (!pyStrip(text)) {
    // An empty item cannot interrupt a paragraph.
    const endPos = state.appendParagraph();
    if (endPos) return endPos;
  }
  const marker = item.marker;
  const depth = state.depth();
  const ordered = marker.length > 1;
  const token: Token = { type: "list", children: [], tight: true, bullet: marker[marker.length - 1], attrs: { depth, ordered } };
  if (ordered) {
    const start = parseInt(marker.slice(0, -1), 10);
    if (start !== 1) {
      // Only a list starting at 1 interrupts a paragraph.
      const endPos = state.appendParagraph();
      if (endPos) return endPos;
      token.attrs!.start = start;
    }
  }
  state.cursor = m.end + 1;
  const rules =
    depth >= block.maxNestedLevel - 1
      ? block.listRules.filter((r) => r !== "list" && r !== "block_quote")
      : block.listRules;
  const bullet = listBullet(item.marker[item.marker.length - 1]!);
  const ctx = { endPos: null as number | null, tokenIndex: null as number | null };
  let current: ListMarker | null = item;
  while (current) current = parseListItem(block, bullet, current, token, state, rules, ctx);
  transformTightList(token);
  if (ctx.endPos) {
    state.tokens.splice(ctx.tokenIndex!, 0, token);
    return ctx.endPos;
  }
  state.appendToken(token);
  return state.cursor;
}

function transformTightList(token: Token): void {
  if (!token.tight) return;
  for (const item of token.children!) {
    for (const tok of item.children!) {
      if (tok.type === "paragraph") tok.type = "block_text";
      else if (tok.type === "list") transformTightList(tok);
    }
  }
}

function parseListItem(
  block: BlockParser,
  bullet: string,
  item: ListMarker,
  token: Token,
  state: BlockState,
  rules: string[],
  ctx: { endPos: number | null; tokenIndex: number | null },
): ListMarker | null {
  const width = leadingWidth(item);
  const [text, continueWidth] = compileContinueWidth(item.text, width);
  const listItemRe = compileListItemPattern(bullet, width);
  const breakSc = block.listBreakScanner(width);
  const lines = collectListItemLines(block, listItemRe, breakSc, state, text, continueWidth);
  if (lines.loose) token.tight = false;
  if (lines.endPos !== null) {
    ctx.tokenIndex = lines.tokenIndex;
    ctx.endPos = lines.endPos;
  }
  const child = state.childState(buildListItemSource(text, lines.src, continueWidth));
  block.parse(child, rules);
  if (token.tight && isLooseList(child.tokens)) token.tight = false;
  token.children!.push({ type: "list_item", children: child.tokens });
  return lines.nextItem;
}

function collectListItemLines(
  block: BlockParser,
  listItemRe: RegExp,
  breakSc: Scanner,
  state: BlockState,
  text: string,
  continueWidth: number,
): ItemLines {
  let src = "";
  let prevBlankLine = false;
  const result: ItemLines = { src: "", nextItem: null, loose: false, endPos: null, tokenIndex: null };
  while (state.cursor < state.cursorMax) {
    const rawLine = state.getLine(state.cursor);
    const nextPos = state.cursor + rawLine.length;
    BLANK_LINE.lastIndex = 0;
    if (BLANK_LINE.test(rawLine)) {
      src += "\n";
      prevBlankLine = true;
      state.cursor = nextPos;
      continue;
    }
    const hasContinuation = hasContinuationIndent(rawLine, continueWidth);
    if (hasContinuation) {
      // An item can begin with at most one blank line.
      if (prevBlankLine && !text && !pyStrip(src)) break;
      src += rawLine;
      prevBlankLine = false;
      state.cursor = nextPos;
      continue;
    }
    const line = expandLeadingTabs(rawLine);
    const lineBreak = matchListItemBreak(listItemRe, breakSc, state, line);
    if (lineBreak) {
      const [tokType, m] = lineBreak;
      if (tokType === "list_item") {
        result.src = src;
        result.nextItem = createListMarker((m as RegExpExecArray).groups!, "listitem");
        result.loose = prevBlankLine;
        state.cursor = nextPos;
        return result;
      }
      if (tokType === "list") break;
      const tokIndex = state.tokens.length;
      const endPos = block.parseMethod(m as ScanMatch, state);
      if (endPos) {
        result.src = src;
        result.endPos = endPos;
        result.tokenIndex = tokIndex;
        return result;
      }
    }
    if (prevBlankLine && !hasContinuation) break;
    src += rawLine;
    state.cursor = nextPos;
  }
  result.src = src;
  return result;
}

function buildListItemSource(text: string, src: string, continueWidth: number): string {
  return stripEnd(text + cleanListItemText(src, continueWidth));
}

// util.strip_end: trailing whitespace after the final line break.
function stripEnd(src: string): string {
  let end = src.length;
  while (end) {
    const c = cpBefore(src, end);
    if (!isSpace(c)) break;
    end -= c.length;
  }
  const newline = src.indexOf("\n", end);
  if (newline >= 0) return src.slice(0, newline) + "\n";
  return src;
}

function matchListItemBreak(
  listItemRe: RegExp,
  breakSc: Scanner,
  state: BlockState,
  line: string,
): [string, ScanMatch | RegExpExecArray] | null {
  const m = breakSc.matchAt(state.src, state.cursor);
  if (m && m.name === "thematic_break") return ["thematic_break", m];
  listItemRe.lastIndex = 0;
  const m2 = listItemRe.exec(line);
  if (m2) return ["list_item", m2];
  if (m) return [m.name, m];
  return null;
}

function listBullet(c: string): string {
  if (c === ".") return "\\d{0,9}\\.";
  if (c === ")") return "\\d{0,9}\\)";
  if (c === "*") return "\\*";
  if (c === "+") return "\\+";
  return "-";
}

const ITEM_PATTERNS = new Map<string, RegExp>();

function compileListItemPattern(bullet: string, width: number): RegExp {
  if (width > 3) width = 3;
  const key = bullet + width;
  let re = ITEM_PATTERNS.get(key);
  if (!re) {
    re = pyre("^(?P<listitem_1> {0," + width + "})(?P<listitem_2>" + bullet + ")(?P<listitem_3>[ \\t]*|[ \\t][^\\n]+)$", "y");
    ITEM_PATTERNS.set(key, re);
  }
  return re;
}

function compileContinueWidth(text: string, width: number): [string, number] {
  text = expandLeadingTabs(text, width);
  LINE_HAS_TEXT.lastIndex = 0;
  let spaceWidth: number;
  if (LINE_HAS_TEXT.test(text)) {
    const indent = countIndent(text);
    spaceWidth = indent >= 5 ? 1 : indent;
    text = text.slice(spaceWidth) + "\n";
  } else {
    spaceWidth = 1;
    text = "";
  }
  return [text, width + spaceWidth];
}

function cleanListItemText(src: string, continueWidth: number): string {
  return src
    .split("\n")
    .map((line) =>
      hasContinuationIndent(line, continueWidth) ? stripContinuationIndent(line, continueWidth) : expandLeadingTabs(line),
    )
    .join("\n");
}

function hasContinuationIndent(line: string, columns: number): boolean {
  return countIndent(line) >= columns;
}

function stripContinuationIndent(line: string, columns: number): string {
  const expanded = expandLeadingTabs(line);
  return expanded.length >= columns ? expanded.slice(columns) : "";
}

function expandLeadingTabs(line: string, startColumn = 0): string {
  let column = startColumn;
  let out = "";
  let index = 0;
  while (index < line.length) {
    const c = line[index];
    if (c === " ") {
      out += " ";
      column += 1;
    } else if (c === "\t") {
      const size = 4 - (column % 4);
      out += " ".repeat(size);
      column += size;
    } else break;
    index++;
  }
  return out + line.slice(index);
}

function countIndent(text: string): number {
  let column = 0;
  for (const c of text) {
    if (c === " ") column += 1;
    else if (c === "\t") column += 4 - (column % 4);
    else break;
  }
  return column;
}

function isLooseList(tokens: Token[]): boolean {
  let paragraphs = 0;
  for (const tok of tokens) {
    if (tok.type === "blank_line") return true;
    if (tok.type === "paragraph") {
      paragraphs++;
      if (paragraphs > 1) return true;
    }
  }
  return false;
}
