// A user cell's reads, kept between its own statements so a read repeated
// with nothing written in between costs no rows. A result is keyed by its
// statement and bindings and stands until a write lands on a table the
// statement names.
//
// Every write the cell makes passes through here, so the cache needs no help
// from the code that writes. What it cannot attribute to a table drops every
// result: a statement it does not parse, a transaction that rolled back,
// `deleteAll`, and a `total_changes()` that moved without a write through
// here, which is how a row changed behind its back shows up.
import type { CellStorage, Row, Sql, SqlCursor } from './storage.js';

/** Parent tables whose ON DELETE actions write their children, which a
 * statement that deletes rows (a DELETE, or a REPLACE resolving a conflict)
 * writes too. No foreign key acts on update. A test holds this to the schema. */
export const CASCADES: Readonly<Record<string, readonly string[]>> = {
  decks: ['questions', 'study_sessions', 'trivia_sessions'],
  questions: ['cards', 'reviews', 'study_session_answers', 'trivia_queue'],
  study_sessions: ['study_session_answers'],
};

/** Functions whose answer can change with no write: the clock, the random
 * source, the connection's own counters. A test holds this to SQLite's own
 * list of nondeterministic builtins. */
export const VOLATILE_FUNCTIONS: readonly string[] = [
  'random', 'randomblob', 'changes', 'total_changes', 'last_insert_rowid', 'load_extension',
  'date', 'time', 'datetime', 'julianday', 'strftime', 'unixepoch', 'timediff',
  'current_date', 'current_time', 'current_timestamp',
];

// The catalogue, the sequence table and the pragmas change without a write
// to any table a statement names.
const VOLATILE = new RegExp(String.raw`\b(?:${VOLATILE_FUNCTIONS.join('|')})\b|\bsqlite_|\bpragma`, 'i');

/** Enough for the rows a request repeats; a long list is read once anyway.
 * The byte budget is per cell, and a node holds many cells. */
const MAX_ROWS = 32;
const MAX_BYTES = 256 * 1024;
/** The writes a holder of a mark can still be shown, a few requests' worth. */
const MAX_LOGGED = 64;

export type Statement =
  | { kind: 'read'; tables: readonly string[] | null }
  | { kind: 'write'; tables: readonly string[] }
  | { kind: 'opaque' };

const LEADING = /^(?:\s+|--[^\n]*(?:\n|$)|\/\*[\s\S]*?\*\/)*/;
const WRITE =
  /^(?:insert(?:\s+or\s+\w+)?\s+into|replace\s+into|update(?:\s+or\s+\w+)?|delete\s+from)\s+(?:main\s*\.\s*)?(?:"(\w+)"|`(\w+)`|\[(\w+)\]|(\w+))(?=[\s(]|$)/i;
const DELETES = /^(?:delete|replace|(?:insert|update)\s+or\s+replace)\b/i;

function closure(table: string): string[] {
  const out = new Set([table]);
  const queue = [table];
  while (queue.length) for (const child of CASCADES[queue.shift()!] ?? []) if (!out.has(child)) out.add(child), queue.push(child);
  return [...out];
}

/** Every word in the statement: a superset of the tables it reads, since a
 * table can only be read by naming it. */
export function wordsOf(sql: string): string[] {
  return [...new Set(sql.toLowerCase().match(/[a-z_][a-z0-9_]*/g) ?? [])];
}

function isScript(sql: string): boolean {
  return sql.replace(/--[^\n]*/g, '').trim().replace(/;\s*$/, '').includes(';');
}

export function classify(sql: string): Statement {
  if (isScript(sql)) return { kind: 'opaque' };
  const head = sql.replace(LEADING, '');
  if (/^select\b/i.test(head)) return { kind: 'read', tables: VOLATILE.test(sql) ? null : wordsOf(sql) };
  const write = WRITE.exec(head);
  if (write) {
    const table = (write[1] ?? write[2] ?? write[3] ?? write[4])!.toLowerCase();
    return { kind: 'write', tables: DELETES.test(head) ? closure(table) : [table] };
  }
  return { kind: 'opaque' };
}

// Statement texts are a closed set, give or take the ones built per call.
const classified = new Map<string, Statement>();
function statementOf(sql: string): Statement {
  let s = classified.get(sql);
  if (s === undefined) {
    if (classified.size >= 2048) classified.clear();
    s = classify(sql);
    classified.set(sql, s);
  }
  return s;
}

/** Null when a binding is not a plain value, which makes the read uncached. */
function keyOf(sql: string, bindings: readonly unknown[]): string | null {
  const parts: unknown[] = [];
  for (const v of bindings) {
    if (v === null || typeof v === 'string') parts.push(v);
    else if (typeof v === 'number') parts.push(Number.isFinite(v) && !Object.is(v, -0) ? v : [String(Object.is(v, -0) ? '-0' : v)]);
    else return null;
  }
  return JSON.stringify([sql, parts]);
}

/** Roughly what a result holds in memory; null when a value is not plain. */
function sizeOf(key: string, rows: readonly Row[]): number | null {
  let bytes = 2 * key.length;
  for (const r of rows) {
    for (const [k, v] of Object.entries(r)) {
      if (typeof v === 'string') bytes += 2 * (k.length + v.length);
      else if (v === null || typeof v === 'number') bytes += 2 * k.length + 8;
      else return null;
    }
  }
  return bytes;
}

const copy = <T extends Row>(rows: readonly Row[]): T[] => rows.map((r) => ({ ...r }) as T);

export interface Mark {
  epoch: number;
  tick: number;
}

interface Held {
  rows: readonly Row[];
  tables: readonly string[];
  mark: Mark;
  bytes: number;
}

/** A write as it was sent, for a holder that can tell which writes leave its
 * answer standing. `bindings` is null when one is not a short plain value. */
export interface LoggedWrite {
  tick: number;
  sql: string;
  bindings: readonly (string | number | null)[] | null;
  tables: readonly string[];
}

function loggable(bindings: readonly unknown[]): readonly (string | number | null)[] | null {
  for (const v of bindings) if (!(v === null || typeof v === 'number' || (typeof v === 'string' && v.length <= 64))) return null;
  return [...(bindings as (string | number | null)[])];
}

export class ReadCache {
  private epoch = 0;
  private tick = 0;
  private readonly written = new Map<string, number>();
  private readonly results = new Map<string, Held>();
  private bytes = 0;
  private readonly log: LoggedWrite[] = [];
  /** `total_changes()` after the last write through here; null when unknown. */
  private changes: number | null = null;

  constructor(private readonly raw: CellStorage) {}

  mark(): Mark {
    return { epoch: this.epoch, tick: this.tick };
  }

  private unchanged(mark: Mark, tables: readonly string[]): boolean {
    return mark.epoch === this.epoch && tables.every((t) => (this.written.get(t) ?? 0) <= mark.tick);
  }

  /** Whether nothing has written `tables` since `mark`, asked of the database
   * as well as of this cache before a held answer is served. */
  holds(mark: Mark, tables: readonly string[]): boolean {
    if (!this.unchanged(mark, tables)) return false;
    this.verify();
    return this.unchanged(mark, tables);
  }

  /** The writes since `mark` that touched `tables`, or null when they cannot
   * all be named: something was dropped, or the log no longer reaches back. */
  writesSince(mark: Mark, tables: readonly string[]): LoggedWrite[] | null {
    if (mark.epoch !== this.epoch) return null;
    this.verify();
    if (mark.epoch !== this.epoch) return null;
    if (this.tick > mark.tick && (this.log[0]?.tick ?? Infinity) > mark.tick + 1) return null;
    return this.log.filter((w) => w.tick > mark.tick && w.tables.some((t) => tables.includes(t)));
  }

  drop(): void {
    this.epoch += 1;
    this.written.clear();
    this.results.clear();
    this.bytes = 0;
    this.log.length = 0;
  }

  /** Drops everything, and trusts no change count until the next one is read. */
  forgetChanges(): void {
    this.drop();
    this.changes = null;
  }

  private total(): number {
    return Number(this.raw.sql.exec<{ n: number }>('SELECT total_changes() AS n').one().n);
  }

  private verify(): void {
    const n = this.total();
    if (n !== this.changes) this.drop();
    this.changes = n;
  }

  /** A write of this cell's own: anything changed before it is caught, and its
   * own count is absorbed after it. */
  write<T>(sent: { sql: string; bindings: readonly unknown[]; tables: readonly string[] } | null, fn: () => T): T {
    this.verify();
    if (sent === null) this.drop();
    else {
      this.tick += 1;
      for (const t of sent.tables) this.written.set(t, this.tick);
      this.log.push({ tick: this.tick, sql: sent.sql, bindings: loggable(sent.bindings), tables: sent.tables });
      if (this.log.length > MAX_LOGGED) this.log.shift();
    }
    try {
      return fn();
    } finally {
      this.changes = this.total();
    }
  }

  async writeAsync<T>(fn: () => Promise<T>): Promise<T> {
    this.verify();
    try {
      return await fn();
    } finally {
      this.changes = this.total();
    }
  }

  lookup(key: string): readonly Row[] | null {
    const held = this.results.get(key);
    if (!held || !this.holds(held.mark, held.tables)) return null;
    this.results.delete(key);
    this.results.set(key, held);
    return held.rows;
  }

  keep(key: string, tables: readonly string[], mark: Mark, rows: readonly Row[]): void {
    if (rows.length > MAX_ROWS || !this.unchanged(mark, tables)) return;
    const bytes = sizeOf(key, rows);
    if (bytes === null || bytes > MAX_BYTES / 8) return;
    this.forget(key);
    while (this.bytes + bytes > MAX_BYTES) this.forget(this.results.keys().next().value!);
    this.results.set(key, { rows: copy(rows), tables, mark, bytes });
    this.bytes += bytes;
  }

  private forget(key: string): void {
    const held = this.results.get(key);
    if (!held) return;
    this.results.delete(key);
    this.bytes -= held.bytes;
  }
}

/** A held result: copies out, so a caller that mutates a row mutates its own. */
class HeldCursor<T extends Row> implements SqlCursor<T> {
  readonly rowsWritten = 0;
  constructor(
    private readonly rows: readonly Row[],
    private readonly live: () => SqlCursor<T>,
  ) {}
  toArray(): T[] {
    return copy<T>(this.rows);
  }
  /** Anything but exactly one row is the live cursor's error to raise. */
  one(): T {
    return this.rows.length === 1 ? copy<T>(this.rows)[0]! : this.live().one();
  }
  [Symbol.iterator](): IterableIterator<T> {
    return this.toArray()[Symbol.iterator]();
  }
}

/** A live read, kept once it has been read whole. */
class KeepingCursor<T extends Row> implements SqlCursor<T> {
  private kept = false;
  constructor(
    private readonly inner: SqlCursor<T>,
    private readonly keep: (rows: readonly Row[]) => void,
  ) {}
  get rowsWritten(): number {
    return this.inner.rowsWritten;
  }
  toArray(): T[] {
    const rows = this.inner.toArray();
    if (!this.kept) {
      this.kept = true;
      this.keep(rows);
    }
    return rows;
  }
  one(): T {
    return this.inner.one();
  }
  [Symbol.iterator](): IterableIterator<T> {
    return this.inner[Symbol.iterator]();
  }
}

class CachedSql implements Sql {
  constructor(
    private readonly raw: CellStorage,
    private readonly cache: ReadCache,
  ) {}

  get databaseSize(): number {
    return this.raw.sql.databaseSize;
  }

  exec<T extends Row = Row>(query: string, ...bindings: unknown[]): SqlCursor<T> {
    const statement = statementOf(query);
    if (statement.kind === 'write') return this.cache.write({ sql: query, bindings, tables: statement.tables }, () => this.raw.sql.exec<T>(query, ...bindings));
    if (statement.kind === 'opaque') return this.cache.write(null, () => this.raw.sql.exec<T>(query, ...bindings));
    const tables = statement.tables;
    const key = tables === null ? null : keyOf(query, bindings);
    if (tables === null || key === null) return this.raw.sql.exec<T>(query, ...bindings);
    const live = () => this.raw.sql.exec<T>(query, ...bindings);
    const held = this.cache.lookup(key);
    if (held) return new HeldCursor<T>(held, live);
    const mark = this.cache.mark();
    return new KeepingCursor<T>(live(), (rows) => this.cache.keep(key, tables, mark, rows));
  }
}

/** A user cell's storage with its reads kept. */
export class ReadCachingStorage implements CellStorage {
  readonly cache: ReadCache;
  readonly sql: Sql;

  constructor(private readonly raw: CellStorage) {
    this.cache = new ReadCache(raw);
    this.sql = new CachedSql(raw, this.cache);
  }

  /** Drops every held result, so the next reads go to the rows. */
  forgetReads(): void {
    this.cache.drop();
  }

  transactionSync<T>(fn: () => T): T {
    try {
      return this.raw.transactionSync(fn);
    } catch (e) {
      // Rolled back: a result read inside it may be of rows that never landed.
      this.cache.drop();
      throw e;
    }
  }

  async deleteAll(): Promise<void> {
    this.cache.drop();
    try {
      await this.raw.deleteAll();
    } finally {
      this.cache.forgetChanges();
    }
  }

  get<T = unknown>(key: string): Promise<T | undefined> {
    return this.raw.get<T>(key);
  }

  list<T = unknown>(): Promise<Map<string, T>> {
    return this.raw.list<T>();
  }

  getAlarm(): Promise<number | null> {
    return this.raw.getAlarm();
  }

  // The key-value surface and the alarm write the same database on the
  // runtime, so they move `total_changes()` too.
  put<T = unknown>(key: string, value: T): Promise<void> {
    return this.cache.writeAsync(() => this.raw.put<T>(key, value));
  }

  delete(key: string): Promise<boolean> {
    return this.cache.writeAsync(() => this.raw.delete(key));
  }

  setAlarm(scheduledTime: number | Date): Promise<void> {
    return this.cache.writeAsync(() => this.raw.setAlarm(scheduledTime));
  }

  deleteAlarm(): Promise<void> {
    return this.cache.writeAsync(() => this.raw.deleteAlarm());
  }
}

/** The cache behind a storage, when it has one. */
export function readCacheOf(storage: CellStorage): ReadCache | null {
  return storage instanceof ReadCachingStorage ? storage.cache : null;
}
