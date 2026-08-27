// Statement helpers over a cell's SqlStorage. Bindings are normalized here
// because SqlStorage accepts only null, number, string and bytes.

import type { Row, Sql, SqlValue } from '../../storage.js';

export type { CellStorage, Row, Sql, SqlCursor, SqlValue } from '../../storage.js';

/** Booleans and undefined are not SqlStorage values. */
export function bind(value: unknown): SqlValue {
  if (value === undefined || value === null) return null;
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'number' || typeof value === 'string') return value;
  if (value instanceof ArrayBuffer || value instanceof Uint8Array) return value;
  throw new TypeError(`cannot bind ${typeof value}`);
}

/** Runs one statement; returns the rows for a read and the change count for a write. */
export class Db {
  constructor(readonly sql: Sql) {}

  all<T extends Row = Row>(query: string, ...bindings: unknown[]): T[] {
    return this.sql.exec<T>(query, ...bindings.map(bind)).toArray();
  }

  first<T extends Row = Row>(query: string, ...bindings: unknown[]): T | null {
    const rows = this.all<T>(query, ...bindings);
    return rows.length ? rows[0]! : null;
  }

  /** Rows changed by the last INSERT, UPDATE or DELETE. */
  run(query: string, ...bindings: unknown[]): number {
    this.sql.exec(query, ...bindings.map(bind));
    return Number(this.sql.exec<{ n: number }>('SELECT changes() AS n').one().n);
  }

  /** Rowid of the last INSERT on this connection. */
  insert(query: string, ...bindings: unknown[]): number {
    this.sql.exec(query, ...bindings.map(bind));
    return Number(this.sql.exec<{ id: number }>('SELECT last_insert_rowid() AS id').one().id);
  }

  /** Runs a multi-statement script; bindings are not allowed. */
  script(sqlText: string): void {
    this.sql.exec(sqlText);
  }

  columns(table: string): Set<string> {
    return new Set(this.all<{ name: string }>(`SELECT name FROM pragma_table_info(?)`, table).map((r) => r.name));
  }

  /** The primary key in its declared order; empty for a rowid-only table. */
  primaryKey(table: string): string[] {
    return this.all<{ name: string; pk: number }>(`SELECT name, pk FROM pragma_table_info(?)`, table)
      .filter((r) => Number(r.pk) > 0)
      .sort((a, b) => Number(a.pk) - Number(b.pk))
      .map((r) => String(r.name));
  }

  tables(): string[] {
    return this.all<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").map((r) => r.name);
  }
}

/**
 * What a row whose primary key is already there means.
 *
 * `ignore` keeps the row the cell holds. That is the merge's rule: the two
 * cells mint from disjoint id blocks, so a collision is a bug and the
 * target's row is the one to keep.
 *
 * `update` overwrites it when it differs. That is the migration's, and the
 * delta pass is why: every table but `cards` is also inserted into, but
 * `cards` is only ever rewritten - each review moves `stability`,
 * `difficulty`, `next_due`, `step` and `fsrs_state` - so an import that can
 * only insert carries a user's pre-window schedule forward, and no re-run of
 * the same import can repair it.
 */
export type ConflictMode = 'ignore' | 'update';

/**
 * The write one row costs, keyed on the table's own primary key.
 *
 * `DO UPDATE` is guarded on the row actually differing (`IS NOT` is
 * null-safe), so `changes()` counts rows inserted or changed and a replay of
 * an unchanged export still reports zero: the delta pass's count is the
 * window's writes and nothing else.
 */
export function writeStatement(table: string, keys: readonly string[], primaryKey: readonly string[], conflict: ConflictMode): string {
  const quoted = keys.map((k) => `"${k}"`).join(', ');
  const marks = keys.map(() => '?').join(', ');
  const updatable = conflict === 'update' && primaryKey.length > 0 ? keys.filter((k) => !primaryKey.includes(k)) : [];
  if (updatable.length === 0) return `INSERT OR IGNORE INTO "${table}" (${quoted}) VALUES (${marks})`;
  const target = primaryKey.map((k) => `"${k}"`).join(', ');
  const set = updatable.map((k) => `"${k}" = excluded."${k}"`).join(', ');
  const changed = updatable.map((k) => `"${table}"."${k}" IS NOT excluded."${k}"`).join(' OR ');
  return `INSERT INTO "${table}" (${quoted}) VALUES (${marks}) ON CONFLICT(${target}) DO UPDATE SET ${set} WHERE ${changed}`;
}
