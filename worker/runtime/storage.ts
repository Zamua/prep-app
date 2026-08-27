// The slice of a cell's storage the runtime relies on: the SqlStorage
// cursor API, the KV surface, the synchronous transaction and the alarm.

export type SqlValue = string | number | null | ArrayBuffer | Uint8Array;
export type Row = Record<string, SqlValue>;

export interface SqlCursor<T extends Row = Row> {
  toArray(): T[];
  one(): T;
  readonly rowsWritten: number;
  [Symbol.iterator](): IterableIterator<T>;
}

export interface Sql {
  exec<T extends Row = Row>(query: string, ...bindings: unknown[]): SqlCursor<T>;
  readonly databaseSize: number;
}

export interface CellStorage {
  readonly sql: Sql;
  transactionSync<T>(fn: () => T): T;
  deleteAll(): Promise<void>;
  get<T = unknown>(key: string): Promise<T | undefined>;
  put<T = unknown>(key: string, value: T): Promise<void>;
  delete(key: string): Promise<boolean>;
  /** The one durable timer: it fires on an evicted cell and survives a node
   * restart. Every schedule in the app is derived from rows and re-armed
   * through these, never held in an isolate. */
  getAlarm(): Promise<number | null>;
  setAlarm(scheduledTime: number | Date): Promise<void>;
  deleteAlarm(): Promise<void>;
}

/** One bounded page of a table, and where the next one resumes. */
export interface DumpPage {
  rows: Row[];
  /** The last row's rowid, or null when this page was the last. */
  next: number | null;
}

/** A table or column the cell does not have. Identifiers cannot be bound,
 * so every name is checked against the cell's own catalogue first. */
export class UnknownTable extends Error {}

export function cellTables(sql: Sql): Set<string> {
  const rows = sql
    .exec<{ name: string }>("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    .toArray();
  return new Set(rows.map((r) => String(r.name)));
}

/**
 * The migration verifier's read: rows in rowid order after a cursor, capped.
 * Paging by rowid rather than by each table's own key is what lets one
 * argument bound both the import and the dump, whatever a table is keyed by.
 */
export function pageByRowid(
  sql: Sql,
  table: string,
  opts: { after?: number | null; limit: number; columns?: readonly string[] },
): DumpPage {
  if (!cellTables(sql).has(table)) throw new UnknownTable(`no such table: ${table}`);
  const known = new Set(
    sql
      .exec<{ name: string }>('SELECT name FROM pragma_table_info(?)', table)
      .toArray()
      .map((r) => String(r.name)),
  );
  const wanted = opts.columns?.length ? opts.columns : [...known];
  for (const column of wanted) if (!known.has(column)) throw new UnknownTable(`${table} has no column ${column}`);
  const projection = wanted.map((c) => `"${c}"`).join(', ');
  const rows = sql
    .exec<Row & { _rowid: number }>(
      `SELECT rowid AS _rowid, ${projection} FROM "${table}" WHERE rowid > ? ORDER BY rowid LIMIT ?`,
      opts.after ?? 0,
      opts.limit,
    )
    .toArray();
  // A full page carries a cursor; an empty one never does, whatever the limit
  // was, or a `limit=0` read indexes off the end of the array.
  const last = rows.length > 0 && rows.length === opts.limit ? Number(rows[rows.length - 1]!._rowid) : null;
  for (const row of rows) delete (row as Record<string, unknown>)['_rowid'];
  return { rows, next: last };
}

/**
 * The same storage with one transaction depth across everything built on it.
 * celld refuses a second `BEGIN`, and most repository methods already open
 * their own transaction, so a caller that needs several of them to land as
 * one fact has no way to say so. Here an inner call joins the outer: the
 * outer commit is what makes the work atomic, and an inner throw still
 * unwinds through it and rolls the whole thing back.
 */
export function joinedTransactions(storage: CellStorage): CellStorage {
  let depth = 0;
  return {
    get sql() {
      return storage.sql;
    },
    transactionSync<T>(fn: () => T): T {
      if (depth > 0) return fn();
      depth += 1;
      try {
        return storage.transactionSync(fn);
      } finally {
        depth -= 1;
      }
    },
    deleteAll: () => storage.deleteAll(),
    get: <T>(key: string) => storage.get<T>(key),
    put: <T>(key: string, value: T) => storage.put<T>(key, value),
    delete: (key: string) => storage.delete(key),
    getAlarm: () => storage.getAlarm(),
    setAlarm: (at: number | Date) => storage.setAlarm(at),
    deleteAlarm: () => storage.deleteAlarm(),
  };
}
