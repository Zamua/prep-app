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
