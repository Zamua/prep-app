// A cell's whole state in and out, for a move between runtimes. The image is
// read and applied on the same build, so the schema is already in place on
// both sides and only rows and key-value entries travel.
import { CELL_IMAGE_FORMAT, CELL_IMAGE_VERSION, type CellImage, type ImageRestore, type ImageTable, type ImageValue } from '../../domain/cellImage.js';
import type { CellStorage, SqlValue } from '../storage.js';

/** Bookkeeping a cell writes as it activates, so a blank cell holds it too. */
const ACTIVATION_TABLES = new Set(['schema_version']);

/** A runtime's own tables (`_cf_*`, celld's `_litestream_*`) start with an
 * underscore and refuse a read; the app's never do. */
function appTables(storage: CellStorage): string[] {
  return storage.sql
    .exec<{ name: string }>(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND substr(name, 1, 7) <> 'sqlite_' AND substr(name, 1, 1) <> '_' ORDER BY name",
    )
    .toArray()
    .map((r) => String(r.name));
}

function encode(v: SqlValue): ImageValue {
  if (v === null || typeof v === 'string' || typeof v === 'number') return v;
  const bytes = v instanceof ArrayBuffer ? new Uint8Array(v) : new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return { b64: btoa(bin) };
}

function decode(v: ImageValue): SqlValue {
  if (v === null || typeof v !== 'object') return v;
  return Uint8Array.from(atob(v.b64), (c) => c.charCodeAt(0));
}

function readTable(storage: CellStorage, name: string): ImageTable {
  const columns = [
    'rowid',
    ...storage.sql
      .exec<{ name: string }>('SELECT name FROM pragma_table_info(?)', name)
      .toArray()
      .map((r) => String(r.name)),
  ];
  // Aliased: SQLite names a bare rowid after the column it aliases, if any.
  const projection = ['rowid AS _image_rowid', ...columns.slice(1).map((c) => `"${c}"`)].join(', ');
  const rows = storage.sql
    .exec(`SELECT ${projection} FROM "${name}" ORDER BY rowid`)
    .toArray()
    .map((r) => [encode(r['_image_rowid'] ?? null), ...columns.slice(1).map((c) => encode(r[c] ?? null))]);
  return { columns, rows };
}

export async function imageOf(storage: CellStorage): Promise<CellImage> {
  const names = appTables(storage);
  const tables: Record<string, ImageTable> = {};
  for (const name of names) tables[name] = readTable(storage, name);
  const sequence: Record<string, number> = {};
  const hasSequence = storage.sql.exec<{ n: number }>("SELECT count(*) AS n FROM sqlite_master WHERE name = 'sqlite_sequence'").one().n > 0;
  if (hasSequence) {
    for (const r of storage.sql.exec<{ name: string; seq: number }>('SELECT name, seq FROM sqlite_sequence ORDER BY name').toArray()) {
      if (names.includes(String(r.name))) sequence[String(r.name)] = Number(r.seq);
    }
  }
  const kv: Record<string, unknown> = {};
  for (const [key, val] of await storage.list()) kv[key] = val;
  return { format: CELL_IMAGE_FORMAT, version: CELL_IMAGE_VERSION, tables, sequence, kv };
}

/**
 * Applies an image to a blank cell. A cell that holds anything beyond its
 * activation bookkeeping is refused rather than merged into, and so is one
 * whose schema differs from the image's: either answer means the move is
 * pointed at the wrong cell or the wrong build.
 */
export async function applyImage(storage: CellStorage, image: CellImage): Promise<ImageRestore> {
  const names = appTables(storage);
  const imaged = Object.keys(image.tables).sort();
  if (names.join(',') !== imaged.join(',')) return { restored: false, reason: 'schema' };
  for (const name of ACTIVATION_TABLES) {
    if (name in image.tables && JSON.stringify(readTable(storage, name)) !== JSON.stringify(image.tables[name])) {
      return { restored: false, reason: 'schema' };
    }
  }
  const occupied = names.some(
    (name) => !ACTIVATION_TABLES.has(name) && storage.sql.exec(`SELECT 1 FROM "${name}" LIMIT 1`).toArray().length > 0,
  );
  if (occupied || (await storage.list()).size > 0) return { restored: false, reason: 'not-blank' };

  let rows = 0;
  storage.transactionSync(() => {
    // Tables go in name order, which is not foreign-key order.
    storage.sql.exec('PRAGMA defer_foreign_keys = ON');
    for (const [name, t] of Object.entries(image.tables)) {
      if (ACTIVATION_TABLES.has(name)) continue;
      const insert = `INSERT INTO "${name}" (${t.columns.map((c) => `"${c}"`).join(', ')}) VALUES (${t.columns.map(() => '?').join(', ')})`;
      for (const row of t.rows) {
        storage.sql.exec(insert, ...row.map(decode));
        rows++;
      }
    }
    for (const [name, seq] of Object.entries(image.sequence)) {
      storage.sql.exec('DELETE FROM sqlite_sequence WHERE name = ?', name);
      storage.sql.exec('INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)', name, seq);
    }
  });
  for (const [key, val] of Object.entries(image.kv)) await storage.put(key, val);
  return { restored: true, rows };
}
