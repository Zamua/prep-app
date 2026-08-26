// A cell's storage under node: SqlStorage over better-sqlite3 plus the KV
// and transaction surface the cells use. One instance per fake cell.
import Database from 'better-sqlite3';
import type { CellStorage, Row, Sql, SqlCursor, SqlValue } from '../../runtime/adapters/sql/storage.js';

class Cursor<T extends Row> implements SqlCursor<T> {
  constructor(
    private readonly rows: T[],
    readonly rowsWritten: number,
  ) {}
  toArray(): T[] {
    return this.rows;
  }
  one(): T {
    if (this.rows.length !== 1) throw new Error(`expected exactly one row, got ${this.rows.length}`);
    return this.rows[0]!;
  }
  [Symbol.iterator](): IterableIterator<T> {
    return this.rows[Symbol.iterator]();
  }
}

const toBinding = (v: unknown): unknown => (v instanceof Uint8Array && !Buffer.isBuffer(v) ? Buffer.from(v) : v);

/** More than one statement: a script, run without bindings as SqlStorage does. */
function isScript(query: string): boolean {
  const body = query.replace(/--[^\n]*/g, '').trim().replace(/;\s*$/, '');
  return body.includes(';');
}

export class SqlStorageFake implements Sql {
  constructor(readonly db: Database.Database) {}

  exec<T extends Row = Row>(query: string, ...bindings: unknown[]): SqlCursor<T> {
    if (bindings.length === 0 && isScript(query)) {
      this.db.exec(query);
      return new Cursor<T>([], 0);
    }
    const stmt = this.db.prepare(query);
    const args = bindings.map(toBinding);
    if (stmt.reader) return new Cursor<T>(stmt.all(...args) as T[], 0);
    const info = stmt.run(...args);
    return new Cursor<T>([], Number(info.changes));
  }

  get databaseSize(): number {
    const pages = this.db.pragma('page_count', { simple: true }) as number;
    const size = this.db.pragma('page_size', { simple: true }) as number;
    return pages * size;
  }
}

export class FakeCellStorage implements CellStorage {
  readonly sql: SqlStorageFake;
  private readonly kv = new Map<string, unknown>();
  /** The scheduled wake, in epoch ms; a test drives it rather than waiting. */
  alarmAt: number | null = null;

  constructor(readonly db: Database.Database = new Database(':memory:')) {
    db.pragma('foreign_keys = ON');
    this.sql = new SqlStorageFake(db);
  }

  transactionSync<T>(fn: () => T): T {
    return this.db.transaction(fn)();
  }

  async deleteAll(): Promise<void> {
    const tables = this.db
      .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
      .all() as { name: string }[];
    this.db.pragma('foreign_keys = OFF');
    for (const { name } of tables) this.db.exec(`DROP TABLE IF EXISTS "${name}"`);
    this.db.pragma('foreign_keys = ON');
    this.kv.clear();
    this.alarmAt = null;
  }

  async get<T = unknown>(key: string): Promise<T | undefined> {
    return this.kv.get(key) as T | undefined;
  }

  async put<T = unknown>(key: string, value: T): Promise<void> {
    this.kv.set(key, structuredClone(value));
  }

  async delete(key: string): Promise<boolean> {
    return this.kv.delete(key);
  }

  async getAlarm(): Promise<number | null> {
    return this.alarmAt;
  }

  async setAlarm(scheduledTime: number | Date): Promise<void> {
    this.alarmAt = typeof scheduledTime === 'number' ? scheduledTime : scheduledTime.getTime();
  }

  async deleteAlarm(): Promise<void> {
    this.alarmAt = null;
  }

  /** Every row of a table, for assertions. */
  rows(table: string): Record<string, SqlValue>[] {
    return this.db.prepare(`SELECT * FROM "${table}" ORDER BY rowid`).all() as Record<string, SqlValue>[];
  }
}

/** A DurableObjectState over the fake storage. */
export function fakeCellState(storage: FakeCellStorage = new FakeCellStorage()): DurableObjectState & { fake: FakeCellStorage } {
  return {
    fake: storage,
    storage,
    blockConcurrencyWhile: <T>(fn: () => Promise<T>) => fn(),
  } as unknown as DurableObjectState & { fake: FakeCellStorage };
}
