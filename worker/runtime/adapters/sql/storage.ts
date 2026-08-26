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

  tables(): string[] {
    return this.all<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").map((r) => r.name);
  }
}
