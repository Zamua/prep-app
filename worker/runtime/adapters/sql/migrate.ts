// Versioned, idempotent migrations for each cell class. Every step is
// re-runnable (IF NOT EXISTS, column checks) and the version is written
// last, so a step that fails mid-way replays on the next activation.
import { AUTOINCREMENT_TABLES, DIRECTORY_SCHEMA, JOB_SCHEMA, LIMITER_SCHEMA, USER_SCHEMA } from './schema.js';
import { Db, type Sql, type SqlValue } from './storage.js';

export interface Migration {
  version: number;
  apply(db: Db): void;
}

export const USER_MIGRATIONS: readonly Migration[] = [
  { version: 1, apply: (db) => db.script(USER_SCHEMA) },
  { version: 2, apply: addJobProgress },
  { version: 3, apply: addStepResults },
];

/** The read model `WorkflowRunner.status` answers from, on cells created
 * before the runner existed. */
function addJobProgress(db: Db): void {
  db.script(
    `CREATE TABLE IF NOT EXISTS job_progress (
  workflow_id TEXT PRIMARY KEY,
  payload     TEXT NOT NULL,
  transition  INTEGER NOT NULL,
  updated_at  TEXT NOT NULL
)`,
  );
}

/** What a redelivered write step answers from, for the steps whose ops are
 * not each individually keyed. */
function addStepResults(db: Db): void {
  db.script(
    `CREATE TABLE IF NOT EXISTS steps_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  result          TEXT NOT NULL,
  created_at      TEXT NOT NULL
)`,
  );
}

export const JOB_MIGRATIONS: readonly Migration[] = [{ version: 1, apply: (db) => db.script(JOB_SCHEMA) }];
export const DIRECTORY_MIGRATIONS: readonly Migration[] = [
  { version: 1, apply: (db) => db.script(DIRECTORY_SCHEMA) },
  { version: 2, apply: addMergeAttempts },
];

/** The merge's give-up counter, on markers written before it existed. */
function addMergeAttempts(db: Db): void {
  const has = db.first("SELECT name FROM pragma_table_info('merge_markers') WHERE name = 'attempts'");
  if (!has) db.script('ALTER TABLE merge_markers ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0');
}
export const LIMITER_MIGRATIONS: readonly Migration[] = [{ version: 1, apply: (db) => db.script(LIMITER_SCHEMA) }];

/** 0 when the version table does not exist yet (a fresh or wiped cell). */
export function currentVersion(db: Db): number {
  const table = db.first("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'");
  if (!table) return 0;
  const row = db.first<{ version: number }>('SELECT version FROM schema_version LIMIT 1');
  return row ? Number(row.version) : 0;
}

/** Applies every migration above the stored version; returns the version now stored. */
export function migrate(sql: Sql, migrations: readonly Migration[]): number {
  const db = new Db(sql);
  try {
    db.script('PRAGMA foreign_keys = ON');
  } catch {
    // A runtime that owns the pragma refuses it; foreign keys are then its default.
  }
  let version = currentVersion(db);
  for (const m of migrations) {
    if (m.version <= version) continue;
    m.apply(db);
    if (db.run('UPDATE schema_version SET version = ?', m.version) === 0) {
      db.run('INSERT INTO schema_version (version) VALUES (?)', m.version);
    }
    version = m.version;
  }
  return version;
}

export const ID_BLOCK = 2 ** 32;

/**
 * Seeds every autoincrement counter to the start of the cell's id block so
 * ids are unique across cells. Block 0 (idx 0) is the parity seed's and the
 * migration importer's; a counter already past its block start is left.
 */
export function seedSequences(sql: Sql, idx: number): void {
  const db = new Db(sql);
  const base = idx * ID_BLOCK;
  for (const table of AUTOINCREMENT_TABLES) {
    const row = db.first<{ seq: number }>('SELECT seq FROM sqlite_sequence WHERE name = ?', table);
    if (row === null) {
      db.run('INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)', table, base);
    } else if (Number(row.seq) < base) {
      db.run('UPDATE sqlite_sequence SET seq = ? WHERE name = ?', base, table);
    }
  }
}

/**
 * Rows into a global cell's table, keyed by the primary key they carry, so a
 * replay inserts nothing. Column names are checked against the catalogue
 * first: an identifier cannot be bound.
 */
export function insertOrIgnore(sql: Sql, table: string, rows: readonly Record<string, unknown>[]): number {
  const db = new Db(sql);
  const columns = db.columns(table);
  if (columns.size === 0) throw new RangeError(`no such table: ${table}`);
  let inserted = 0;
  for (const row of rows) {
    const keys = Object.keys(row).filter((k) => columns.has(k));
    if (keys.length === 0) continue;
    inserted += db.run(
      `INSERT OR IGNORE INTO "${table}" (${keys.map((k) => `"${k}"`).join(', ')}) VALUES (${keys.map(() => '?').join(', ')})`,
      ...keys.map((k) => row[k] as SqlValue),
    );
  }
  return inserted;
}

export function countRows(sql: Sql, table: string): number {
  const db = new Db(sql);
  if (db.columns(table).size === 0) throw new RangeError(`no such table: ${table}`);
  return Number(db.first<{ n: number }>(`SELECT COUNT(*) AS n FROM "${table}"`)?.n ?? 0);
}

/** Drops every counter so ids restart at 1: the parity seed pins block 0. */
export function resetSequences(sql: Sql): void {
  new Db(sql).run('DELETE FROM sqlite_sequence');
}
