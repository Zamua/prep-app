// The Anki package: `fflate` for the zip container, `sql.js` for the
// collection inside it.
//
// The WASM arrives through the module-import path, which is the only one a
// cell has: compiling from bytes at runtime is refused. `sql-wasm-browser.js`
// is the glue that works here - the node glue reaches for `node:fs` because
// the runtime exposes `process.versions.node`.
//
// The compiled module is module-level state, so it is per isolate and shared
// by every cell of the worker on this node. A `Database` is never: one is
// created and closed inside a single request.
import { zipSync } from 'fflate';
import initSqlJs from 'sql.js/dist/sql-wasm-browser.js';
import sqlWasm from 'sql.js/dist/sql-wasm.wasm';
import { NotAnApkg, NotAZip, ZipEntryTooLarge, type ApkgCard, type ApkgCollection, type ApkgNote, type ApkgNoteRow, type ApkgReader, type ApkgWriter } from '../../app/ports.js';
import { FflateZip } from './zip.js';

/** Anki prefers the newer scheduler's collection, and so does this. */
const COLLECTION_NAMES = ['collection.anki21', 'collection.anki2'] as const;

/** Cribbed from anki/rslib/src/storage/sqlite.rs: minimal but complete, since
 * importers refuse a schema they do not recognise. */
const SCHEMA = `
CREATE TABLE col (
    id INTEGER PRIMARY KEY, crt INTEGER NOT NULL, mod INTEGER NOT NULL,
    scm INTEGER NOT NULL, ver INTEGER NOT NULL, dty INTEGER NOT NULL,
    usn INTEGER NOT NULL, ls INTEGER NOT NULL, conf TEXT NOT NULL,
    models TEXT NOT NULL, decks TEXT NOT NULL, dconf TEXT NOT NULL, tags TEXT NOT NULL
);
CREATE TABLE notes (
    id INTEGER PRIMARY KEY, guid TEXT NOT NULL, mid INTEGER NOT NULL,
    mod INTEGER NOT NULL, usn INTEGER NOT NULL, tags TEXT NOT NULL,
    flds TEXT NOT NULL, sfld INTEGER NOT NULL, csum INTEGER NOT NULL,
    flags INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE TABLE cards (
    id INTEGER PRIMARY KEY, nid INTEGER NOT NULL, did INTEGER NOT NULL,
    ord INTEGER NOT NULL, mod INTEGER NOT NULL, usn INTEGER NOT NULL,
    type INTEGER NOT NULL, queue INTEGER NOT NULL, due INTEGER NOT NULL,
    ivl INTEGER NOT NULL, factor INTEGER NOT NULL, reps INTEGER NOT NULL,
    lapses INTEGER NOT NULL, left INTEGER NOT NULL, odue INTEGER NOT NULL,
    odid INTEGER NOT NULL, flags INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE TABLE revlog (
    id INTEGER PRIMARY KEY, cid INTEGER NOT NULL, usn INTEGER NOT NULL,
    ease INTEGER NOT NULL, ivl INTEGER NOT NULL, lastIvl INTEGER NOT NULL,
    factor INTEGER NOT NULL, time INTEGER NOT NULL, type INTEGER NOT NULL
);
CREATE TABLE graves (usn INTEGER NOT NULL, oid INTEGER NOT NULL, type INTEGER NOT NULL);
CREATE INDEX ix_notes_csum ON notes (csum);
CREATE INDEX ix_cards_nid ON cards (nid);
CREATE INDEX ix_cards_sched ON cards (did, queue, due);
CREATE INDEX ix_revlog_cid ON revlog (cid);
`;

interface SqlDatabase {
  run(sql: string, params?: unknown[]): void;
  exec(sql: string): { columns: string[]; values: unknown[][] }[];
  prepare(sql: string): { run(params: unknown[]): void; free(): void };
  export(): Uint8Array;
  close(): void;
}

interface SqlModule {
  Database: new (bytes?: Uint8Array) => SqlDatabase;
}

let compiled: Promise<SqlModule> | null = null;

/** One compile per isolate; every cell on this node shares it. */
export function sqlEngine(): Promise<SqlModule> {
  if (!compiled) {
    compiled = initSqlJs({
      instantiateWasm(imports: WebAssembly.Imports, ready: (instance: WebAssembly.Instance) => void) {
        const instance = new WebAssembly.Instance(sqlWasm, imports);
        ready(instance);
        return instance.exports;
      },
    }) as Promise<SqlModule>;
  }
  return compiled;
}

const dec = new TextDecoder('utf-8');

function collectionOf(blob: Uint8Array, opts: { maxEntryBytes?: number; maxTotalBytes?: number }): Uint8Array {
  let entries;
  try {
    // The media stays compressed in the central directory: a real `.apkg` is
    // mostly media, and none of it is read.
    entries = new FflateZip().read(blob, { ...opts, only: COLLECTION_NAMES });
  } catch (e) {
    if (e instanceof ZipEntryTooLarge) throw e;
    if (e instanceof NotAZip) throw new NotAnApkg(`not a valid .apkg (zip parse failed): ${e.message}`);
    throw e;
  }
  const byName = new Map(entries.map((e) => [e.name, e.bytes]));
  for (const name of COLLECTION_NAMES) {
    const found = byName.get(name);
    if (found) return found;
  }
  throw new NotAnApkg('not a valid .apkg — no collection.anki2 / collection.anki21 inside');
}

export class SqlJsApkg implements ApkgReader, ApkgWriter {
  async notes(blob: Uint8Array, opts: { maxEntryBytes?: number; maxTotalBytes?: number } = {}): Promise<ApkgNote[]> {
    const collection = collectionOf(blob, opts);
    const SQL = await sqlEngine();
    let db: SqlDatabase;
    try {
      db = new SQL.Database(collection);
    } catch (e) {
      throw new NotAnApkg(`not a valid .apkg (collection is not a sqlite database): ${e instanceof Error ? e.message : String(e)}`);
    }
    try {
      const result = db.exec('SELECT id, flds FROM notes ORDER BY id ASC');
      if (!result.length) return [];
      return result[0]!.values.map((row) => ({ id: Number(row[0]), flds: cellText(row[1]) }));
    } catch (e) {
      throw new NotAnApkg(`not a valid .apkg (no readable notes table): ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      db.close();
    }
  }

  async build(col: ApkgCollection, notes: readonly ApkgNoteRow[], cards: readonly ApkgCard[]): Promise<Uint8Array> {
    const SQL = await sqlEngine();
    const db = new SQL.Database();
    let collection: Uint8Array;
    try {
      for (const statement of SCHEMA.split(';')) {
        if (statement.trim()) db.run(statement);
      }
      db.run(
        `INSERT INTO col (id, crt, mod, scm, ver, dty, usn, ls, conf, models, decks, dconf, tags)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`,
        [col.id, col.crt, col.mod, col.scm, col.ver, col.dty, col.usn, col.ls, JSON.stringify(col.conf), JSON.stringify(col.models), JSON.stringify(col.decks), JSON.stringify(col.dconf), JSON.stringify(col.tags)],
      );

      const noteStmt = db.prepare(
        `INSERT INTO notes (id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data) VALUES (?,?,?,?,?,?,?,?,?,?,?)`,
      );
      try {
        for (const n of notes) noteStmt.run([n.id, n.guid, n.mid, n.mod, n.usn, n.tags, n.flds, n.sfld, n.csum, n.flags, n.data]);
      } finally {
        noteStmt.free();
      }

      const cardStmt = db.prepare(
        `INSERT INTO cards (id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps, lapses, left, odue, odid, flags, data)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
      );
      try {
        for (const c of cards) {
          cardStmt.run([c.id, c.nid, c.did, c.ord, c.mod, c.usn, c.type, c.queue, c.due, c.ivl, c.factor, c.reps, c.lapses, c.left, c.odue, c.odid, c.flags, c.data]);
        }
      } finally {
        cardStmt.free();
      }
      collection = db.export();
    } finally {
      db.close();
    }

    // The 1980 stamp again: Python's writer takes the wall clock here, so two
    // exports of one deck differ. Anki reads the entries by name.
    const mtime = new Date(1980, 0, 1, 0, 0, 0).getTime();
    return zipSync(
      {
        'collection.anki21': [collection, { level: 6, mtime, os: 3, attrs: 0o600 << 16 }],
        media: [new TextEncoder().encode('{}'), { level: 0, mtime, os: 3, attrs: 0o600 << 16 }],
      },
      { level: 6 },
    );
  }
}

/** `flds` is declared TEXT but a collection in the wild can hold a blob. */
function cellText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  if (value instanceof Uint8Array) return dec.decode(value);
  return String(value);
}
