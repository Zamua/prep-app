// The statements a cell runs, filed under the action that ran them, plus the
// cell as it stood before the first one: everything a replay needs to count
// what each action read.
import type { FakeCellStorage } from '../fakes/sqlStorage.js';

export interface Statement {
  label: string;
  sql: string;
  bindings: unknown[];
}

export interface Snapshot {
  tables: { name: string; sql: string; columns: string[]; rows: unknown[][] }[];
  indexes: string[];
  sequences: { name: string; seq: number }[];
}

/** Blobs travel as base64 so a recording is plain JSON. */
export const encode = (v: unknown): unknown =>
  v instanceof Uint8Array ? { b64: Buffer.from(v).toString('base64') } : v instanceof ArrayBuffer ? { b64: Buffer.from(new Uint8Array(v)).toString('base64') } : v;

export function snapshot(storage: FakeCellStorage): Snapshot {
  const db = storage.db;
  const tables = (db.prepare("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid").all() as { name: string; sql: string }[]).map((t) => {
    const stmt = db.prepare(`SELECT * FROM "${t.name}" ORDER BY rowid`);
    return { ...t, columns: stmt.columns().map((c) => c.name), rows: (stmt.raw().all() as unknown[][]).map((r) => r.map(encode)) };
  });
  const indexes = (db.prepare("SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY rowid").all() as { sql: string }[]).map((i) => i.sql);
  const sequences = db.prepare('SELECT name, seq FROM sqlite_sequence').all() as { name: string; seq: number }[];
  return { tables, indexes, sequences };
}

export class Recorder {
  readonly statements: Statement[] = [];
  /** Requests per label, so totals divide into a per-request figure. */
  readonly counts = new Map<string, number>();
  private label: string | null = null;

  constructor(storage: FakeCellStorage) {
    const sql = storage.sql as unknown as { exec: (query: string, ...bindings: unknown[]) => unknown };
    const exec = sql.exec.bind(sql);
    sql.exec = (query: string, ...bindings: unknown[]) => {
      this.statements.push({ label: this.label ?? '', sql: query, bindings: bindings.map(encode) });
      return exec(query, ...bindings);
    };
  }

  /** Files every statement `fn` runs under `label`. Statements outside any
   * `during` are kept under '' so a replay still reaches the same state. */
  async during<T>(label: string, fn: () => Promise<T>): Promise<T> {
    this.label = label;
    this.counts.set(label, (this.counts.get(label) ?? 0) + 1);
    try {
      return await fn();
    } finally {
      this.label = null;
    }
  }
}
