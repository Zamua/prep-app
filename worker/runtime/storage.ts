// The slice of a cell's storage the runtime relies on: the SqlStorage
// cursor API, the KV surface and the synchronous transaction.

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
}
