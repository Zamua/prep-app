// Python's `csv` dialect: comma delimiter, `"` quoting doubled on
// escape, CRLF terminator, minimal quoting. The wire format is shared
// with the CSV exporter, so the dialect is part of the contract.

const QUOTE = '"';
const DELIMITER = ',';
export const LINE_TERMINATOR = '\r\n';

/** QUOTE_MINIMAL: quote only when the value carries a structural char. */
function quoteField(value: string): string {
  const needs = value.includes(DELIMITER) || value.includes(QUOTE) || value.includes('\r') || value.includes('\n');
  return needs ? QUOTE + value.split(QUOTE).join(QUOTE + QUOTE) + QUOTE : value;
}

export function writeRow(fields: readonly string[]): string {
  return fields.map(quoteField).join(DELIMITER) + LINE_TERMINATOR;
}

/**
 * Rows of a CSV body. A quoted field keeps embedded newlines and a
 * doubled quote inside one is a single quote. A blank line is an empty
 * row, as `csv.reader` reads it.
 */
export function parseRows(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let quoted = false;
  // Whether the current field opened with a quote, and whether the
  // current line carried anything at all (a blank line is `[]`).
  let fieldQuoted = false;
  let lineSeen = false;
  const endField = () => {
    row.push(field);
    field = '';
    fieldQuoted = false;
  };
  /** A newline always ends a row, even an empty one; end of input only
   * ends a row that had something on it. */
  const endRow = (atEof: boolean) => {
    if (!lineSeen && atEof) return;
    endField();
    if (lineSeen || !atEof) rows.push(lineSeen ? row : []);
    row = [];
    lineSeen = false;
  };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (quoted) {
      if (ch !== QUOTE) {
        field += ch;
      } else if (text[i + 1] === QUOTE) {
        field += QUOTE;
        i++;
      } else {
        quoted = false;
      }
      continue;
    }
    if (ch === QUOTE && field === '' && !fieldQuoted) {
      quoted = true;
      fieldQuoted = true;
      lineSeen = true;
    } else if (ch === DELIMITER) {
      endField();
      lineSeen = true;
    } else if (ch === '\r' || ch === '\n') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      endRow(false);
    } else {
      field += ch;
      lineSeen = true;
    }
  }
  endRow(true);
  return rows;
}

/** `csv.DictReader`: header row, missing columns null, blank rows skipped. */
export function parseDict(text: string): { fieldnames: string[] | null; rows: Record<string, string | null>[] } {
  const raw = parseRows(text);
  if (!raw.length) return { fieldnames: null, rows: [] };
  const fieldnames = raw[0]!;
  const rows: Record<string, string | null>[] = [];
  for (const values of raw.slice(1)) {
    if (values.length === 0) continue;
    const out: Record<string, string | null> = {};
    fieldnames.forEach((name, i) => {
      out[name] = i < values.length ? values[i]! : null;
    });
    rows.push(out);
  }
  return { fieldnames, rows };
}
