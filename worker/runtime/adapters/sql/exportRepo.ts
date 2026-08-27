// Whole-cell reads and writes: the dump the merge exports, the idempotent
// import the target applies, the wipe, and the tombstone.
import type { ExportRepo, TombstoneRepo } from '../../../app/ports.js';
import type { CellSnapshot, Tombstone, TombstoneReason } from '../../../app/entities.js';
import { rowToProfile } from './prefsRepo.js';
import { DATA_TABLES, PROFILE_TABLE } from './schema.js';
import { Db, type CellStorage, type SqlValue } from './storage.js';

const SCRUB_CHUNK = 1 << 20;

/** Owner columns of the multi-user schema, absent from a cell. */
const USER_COLUMNS = new Set(['user_id', 'user_login']);

export class SqlExportRepo implements ExportRepo {
  private readonly db: Db;

  constructor(private readonly storage: CellStorage) {
    this.db = new Db(storage.sql);
  }

  dump(): CellSnapshot {
    const tables: Record<string, Record<string, unknown>[]> = {};
    for (const table of DATA_TABLES) tables[table] = this.db.all(`SELECT * FROM "${table}" ORDER BY rowid`);
    return { profile: this.profile(), tables };
  }

  private profile(): Record<string, unknown> | null {
    const row = this.db.first('SELECT * FROM profile LIMIT 1');
    return row ? { ...rowToProfile(row) } : null;
  }

  project(columns: Readonly<Record<string, readonly string[]>>): CellSnapshot {
    const tables: Record<string, Record<string, unknown>[]> = {};
    for (const [table, cols] of Object.entries(columns)) {
      if (!(DATA_TABLES as readonly string[]).includes(table) || cols.length === 0) continue;
      tables[table] = this.db.all(`SELECT ${cols.map((c) => `"${c}"`).join(', ')} FROM "${table}" ORDER BY rowid`);
    }
    return { profile: this.profile(), tables };
  }

  importRows(snapshot: CellSnapshot, _opts: { idempotentBy: 'id' }): Record<string, number> {
    const counts: Record<string, number> = {};
    this.storage.transactionSync(() => {
      for (const table of DATA_TABLES) {
        const rows = snapshot.tables[table];
        if (!rows || rows.length === 0) continue;
        const columns = this.db.columns(table);
        let inserted = 0;
        for (const row of rows) {
          const keys = Object.keys(row).filter((k) => columns.has(k) && !USER_COLUMNS.has(k));
          if (keys.length === 0) continue;
          const marks = keys.map(() => '?').join(', ');
          inserted += this.db.run(
            `INSERT OR IGNORE INTO "${table}" (${keys.map((k) => `"${k}"`).join(', ')}) VALUES (${marks})`,
            ...keys.map((k) => row[k] as SqlValue),
          );
        }
        if (inserted) counts[table] = inserted;
      }
    });
    return counts;
  }

  /**
   * The migrated row, columns verbatim and keyed by `id`. `id_base` is never
   * written here: a chunk does not carry one, and the importer sets it from
   * the exporter's idx, which a replay must not undo.
   */
  importProfile(row: Readonly<Record<string, unknown>>): void {
    const columns = this.db.columns(PROFILE_TABLE);
    const keys = Object.keys(row).filter((k) => columns.has(k) && k !== 'id_base');
    if (!keys.includes('id')) throw new TypeError('a profile row needs an id');
    const updates = keys.filter((k) => k !== 'id').map((k) => `"${k}" = excluded."${k}"`);
    this.db.run(
      `INSERT INTO "${PROFILE_TABLE}" (${keys.map((k) => `"${k}"`).join(', ')}) VALUES (${keys.map(() => '?').join(', ')})
       ON CONFLICT(id) DO UPDATE SET ${updates.join(', ')}`,
      ...keys.map((k) => row[k] as SqlValue),
    );
  }

  counts(): { profile: boolean; tables: Record<string, number> } {
    const tables: Record<string, number> = {};
    for (const table of DATA_TABLES) tables[table] = Number(this.db.first<{ n: number }>(`SELECT COUNT(*) AS n FROM "${table}"`)?.n ?? 0);
    return { profile: this.db.first(`SELECT id FROM "${PROFILE_TABLE}" LIMIT 1`) !== null, tables };
  }

  wipe(): void {
    this.storage.transactionSync(() => {
      for (const table of [...DATA_TABLES].reverse()) this.db.run(`DELETE FROM "${table}"`);
    });
  }
}

export class SqlTombstoneRepo implements TombstoneRepo {
  private readonly db: Db;

  constructor(storage: CellStorage) {
    this.db = new Db(storage.sql);
  }

  get(): Tombstone | null {
    const exists = this.db.first("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tombstone'");
    if (!exists) return null;
    const row = this.db.first('SELECT reason, at, scrubbed_at, former_bytes FROM tombstone LIMIT 1');
    if (!row) return null;
    return {
      reason: String(row['reason']) as TombstoneReason,
      at: String(row['at']),
      scrubbed_at: (row['scrubbed_at'] as string | null) ?? null,
      former_bytes: Number(row['former_bytes'] ?? 0),
    };
  }

  /** Creates the table itself: it is written right after `deleteAll`. */
  write(reason: TombstoneReason, at: string, formerBytes: number): void {
    this.db.script(
      'CREATE TABLE IF NOT EXISTS tombstone (reason TEXT NOT NULL, at TEXT NOT NULL, scrubbed_at TEXT, former_bytes INTEGER NOT NULL DEFAULT 0)',
    );
    if (this.get() === null) {
      this.db.run('INSERT INTO tombstone (reason, at, former_bytes) VALUES (?, ?, ?)', reason, at, formerBytes);
    }
  }

  stampScrubbed(at: string): void {
    this.db.run('UPDATE tombstone SET scrubbed_at = ? WHERE scrubbed_at IS NULL', at);
  }

  databaseSize(): number {
    return this.db.sql.databaseSize;
  }

  /** Zero-fills to `former_bytes` through a scratch table, once; a scrubbed or missing tombstone is a no-op. */
  scrub(at: string): void {
    const tomb = this.get();
    if (!tomb || tomb.scrubbed_at) return;
    this.db.script('CREATE TABLE IF NOT EXISTS scrub (b BLOB)');
    for (let written = 0; written < tomb.former_bytes; ) {
      const n = Math.min(SCRUB_CHUNK, tomb.former_bytes - written);
      this.db.run('INSERT INTO scrub (b) VALUES (zeroblob(?))', n);
      written += n;
    }
    this.db.script('DROP TABLE scrub');
    this.stampScrubbed(at);
  }
}
