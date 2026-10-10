// A whole cell as data, for moving it to another runtime: every table's rows
// with their rowids, the AUTOINCREMENT counters, and the key-value entries.
// Both sides run the same build, so an image carries rows and never DDL.

export const CELL_IMAGE_FORMAT = 'prep.cell-image';
export const CELL_IMAGE_VERSION = 1;

/** A SQLite value as JSON. A blob travels as base64. */
export type ImageValue = null | number | string | { b64: string };

export interface ImageTable {
  /** `rowid` first, then the table's own columns. */
  columns: string[];
  rows: ImageValue[][];
}

export interface CellImage {
  format: typeof CELL_IMAGE_FORMAT;
  version: typeof CELL_IMAGE_VERSION;
  tables: Record<string, ImageTable>;
  sequence: Record<string, number>;
  kv: Record<string, unknown>;
}

export type ImageRestore = { restored: true; rows: number } | { restored: false; reason: 'not-blank' | 'schema' };

export class InvalidCellImage extends Error {}

/** Names are interpolated into SQL, which cannot bind an identifier. */
const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

function value(v: unknown, where: string): ImageValue {
  if (v === null || typeof v === 'string') return v;
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (isRecord(v) && Object.keys(v).length === 1 && typeof v['b64'] === 'string') return { b64: v['b64'] };
  throw new InvalidCellImage(`${where}: not a SQLite value`);
}

function table(name: string, t: unknown): ImageTable {
  if (!IDENT.test(name)) throw new InvalidCellImage(`table name ${JSON.stringify(name)}`);
  if (!isRecord(t) || !Array.isArray(t['columns']) || !Array.isArray(t['rows'])) throw new InvalidCellImage(`${name}: columns and rows are required`);
  const columns = t['columns'].map((c) => {
    if (typeof c !== 'string' || !IDENT.test(c)) throw new InvalidCellImage(`${name}: column name ${JSON.stringify(c)}`);
    return c;
  });
  if (columns[0] !== 'rowid') throw new InvalidCellImage(`${name}: rowid must be the first column`);
  if (new Set(columns).size !== columns.length) throw new InvalidCellImage(`${name}: duplicate column`);
  const rows = t['rows'].map((r, i) => {
    if (!Array.isArray(r) || r.length !== columns.length) throw new InvalidCellImage(`${name}[${i}]: ${columns.length} values expected`);
    const row = r.map((v, j) => value(v, `${name}[${i}].${columns[j]}`));
    if (typeof row[0] !== 'number' || !Number.isInteger(row[0])) throw new InvalidCellImage(`${name}[${i}]: rowid must be an integer`);
    return row;
  });
  return { columns, rows };
}

/** The image as a cell may apply it, or InvalidCellImage. */
export function parseCellImage(input: unknown): CellImage {
  if (!isRecord(input)) throw new InvalidCellImage('not an object');
  if (input['format'] !== CELL_IMAGE_FORMAT) throw new InvalidCellImage(`format must be ${CELL_IMAGE_FORMAT}`);
  if (input['version'] !== CELL_IMAGE_VERSION) throw new InvalidCellImage(`version must be ${CELL_IMAGE_VERSION}`);
  const { tables, sequence, kv } = input;
  if (!isRecord(tables) || !isRecord(sequence) || !isRecord(kv)) throw new InvalidCellImage('tables, sequence and kv are required');
  const parsed: Record<string, ImageTable> = {};
  for (const [name, t] of Object.entries(tables)) parsed[name] = table(name, t);
  const seq: Record<string, number> = {};
  for (const [name, n] of Object.entries(sequence)) {
    if (!(name in parsed)) throw new InvalidCellImage(`sequence for unknown table ${name}`);
    if (typeof n !== 'number' || !Number.isInteger(n) || n < 0) throw new InvalidCellImage(`sequence ${name}: not a counter`);
    seq[name] = n;
  }
  return { format: CELL_IMAGE_FORMAT, version: CELL_IMAGE_VERSION, tables: parsed, sequence: seq, kv: { ...kv } };
}
