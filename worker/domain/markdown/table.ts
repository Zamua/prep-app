// mistune.plugins.table: pipe tables and the pipe-less `nptable` form.
import { pyRstrip, pyStrip, pyre } from "./chars";
import type { BlockState, ScanMatch } from "./block";
import type { Token } from "./tokens";

export const TABLE_PATTERN = "^ {0,3}\\|[^\\n]*\\|[ \\t]*(?:\\n|$)";
export const NP_TABLE_PATTERN = "^ {0,3}\\S[^\\n]*\\|[^\\n]*(?:\\n|$)";

const ALIGN_CENTER = pyre("^ *:-+: *$");
const ALIGN_LEFT = pyre("^ *:-+ *$");
const ALIGN_RIGHT = pyre("^ *-+: *$");
const ALIGN_NONE = pyre("^ *-+ *$");

type Align = "center" | "left" | "right" | null;

export function parseTable(m: ScanMatch, state: BlockState): number | null {
  let pos = m.end;
  const header = stripPipeTableRow(m.text);
  if (header === null) return null;
  const alignLine = state.getLine(pos);
  const align = stripPipeTableRow(alignLine);
  if (align === null) return null;
  const [thead, aligns] = processThead(header, align);
  if (!thead) return parseInvalidPipeTable(state, pos + alignLine.length);
  pos += alignLine.length;
  const rows: Token[] = [];
  while (pos < state.cursorMax) {
    const line = state.getLine(pos);
    const text = stripPipeTableRow(line);
    if (text === null) break;
    const row = processRow(text, aligns!);
    if (!row) return parseInvalidPipeTable(state, pos + line.length);
    rows.push(row);
    pos += line.length;
  }
  state.appendToken({ type: "table", children: [thead, { type: "table_body", children: rows }] });
  return pos;
}

export function parseNpTable(m: ScanMatch, state: BlockState): number | null {
  let pos = m.end;
  const header = stripTableLine(m.text);
  if (header === null) return null;
  const alignLine = state.getLine(pos);
  const align = stripTableLine(alignLine);
  if (align === null) return null;
  const [thead, aligns] = processThead(header, align);
  if (!thead) return null;
  pos += alignLine.length;
  const rows: Token[] = [];
  while (pos < state.cursorMax) {
    const line = state.getLine(pos);
    const text = stripTableLine(line);
    if (text === null) break;
    const row = processRow(text, aligns!);
    if (!row) return null;
    rows.push(row);
    pos += line.length;
  }
  state.appendToken({ type: "table", children: [thead, { type: "table_body", children: rows }] });
  return pos;
}

function processThead(header: string, align: string): [Token, Align[]] | [null, null] {
  const headers = splitTableCells(header);
  const rawAligns = splitTableCells(align);
  if (headers.length !== rawAligns.length) return [null, null];
  const aligns: Align[] = [];
  for (const v of rawAligns) {
    if (ALIGN_CENTER.test(v)) aligns.push("center");
    else if (ALIGN_LEFT.test(v)) aligns.push("left");
    else if (ALIGN_RIGHT.test(v)) aligns.push("right");
    else if (ALIGN_NONE.test(v) || !pyStrip(v)) aligns.push(null);
    else return [null, null];
  }
  const children: Token[] = headers.map((text, i) => ({
    type: "table_cell",
    text: pyStrip(text),
    attrs: { align: aligns[i], head: true },
  }));
  return [{ type: "table_head", children }, aligns];
}

function processRow(text: string, aligns: Align[]): Token | null {
  const cells = splitTableCells(text);
  if (cells.length !== aligns.length) return null;
  const children: Token[] = cells.map((cell, i) => ({
    type: "table_cell",
    text: pyStrip(cell),
    attrs: { align: aligns[i], head: false },
  }));
  return { type: "table_row", children };
}

function stripPipeTableRow(line: string): string | null {
  let text = pyRstrip(pyRstrip(line, "\n"), " \t");
  if (!text.startsWith("|") && (text.startsWith(" ") || text.startsWith("\t"))) text = text.replace(/^ +/u, "");
  if (!text.startsWith("|") || !text.endsWith("|")) return null;
  return text.slice(1, -1);
}

function parseInvalidPipeTable(state: BlockState, pos: number): number {
  while (pos < state.cursorMax) {
    const line = state.getLine(pos);
    if (stripPipeTableRow(line) === null) break;
    pos += line.length;
  }
  state.addParagraph(state.src.slice(state.cursor, pos));
  return pos;
}

function stripTableLine(line: string): string | null {
  const text = pyRstrip(pyRstrip(line, "\n"), " \t");
  if (!text || !text.includes("|")) return null;
  return text;
}

function splitTableCells(text: string): string[] {
  const cells: string[] = [];
  let start = 0;
  for (let pos = 0; pos < text.length; pos++) {
    if (text[pos] === "|" && !isEscapedPipe(text, pos)) {
      cells.push(pyStrip(text.slice(start, pos)));
      start = pos + 1;
    }
  }
  cells.push(pyStrip(text.slice(start)));
  return cells;
}

function isEscapedPipe(text: string, pos: number): boolean {
  let backslashes = 0;
  pos -= 1;
  while (pos >= 0 && text[pos] === "\\") {
    backslashes++;
    pos--;
  }
  return backslashes % 2 === 1;
}
