// The directory: enumeration data written at create, merge and delete, the
// merge audit and markers, tombstones. RPC only; its fetch answers nothing.
//
// It also owns the retention sweep. There is one directory, so the walk over
// anonymous accounts belongs here rather than in any user's cell, and the
// alarm that drives it is re-derived from the sweep's own row on every
// activation: an eviction or a node restart resumes where it stopped.
import { DurableObject } from 'cloudflare:workers';
import type { Directory, Sync } from '../../app/ports.js';
import type { DirectoryUser, MergeAudit, MergeMarker, TombstoneReason } from '../../app/entities.js';
import { reapIdleAnonymous } from '../../app/auth/reaper.js';
import { isoUtc, parseIso } from '../../domain/time.js';
import { MIGRATION_RECHECK_MS, migrationHolds } from '../../domain/reaper.js';
import { compose, type Composition } from '../compose.js';
import type { Env } from '../env.js';
import { pageByRowid, type CellStorage, type DumpPage } from '../storage.js';

/** What a parity dump of the directory carries. */
const DUMP_TABLES = ['users', 'account_merges', 'merge_markers', 'tombstones'] as const;

const REAP_STATE_KEY = 'reap';
/** The migration's one-way flag. Never cleared: once the cutover verifies,
 * a stale runbook step or a second run of the migrator must not be able to
 * write into a fleet that is already serving. */
const MIGRATION_SEAL_KEY = 'migration_sealed';
/** Which snapshot this fleet is being built from, and when that run opened.
 * The verifier reads it back so it cannot compare a fleet against a snapshot
 * the fleet was not built from, and the sweep reads it so it cannot destroy
 * an account the run has registered but not yet written. */
const MIGRATION_RUN_KEY = 'migration_run';
const DAY_MS = 86_400_000;
/** A wake is never asked for the past. */
const ALARM_FLOOR_MS = 1;
/** Between pages of one sweep: a walk drains over several activations rather
 * than holding the cell for the whole directory. */
const PAGE_GAP_MS = 1_000;

/** The sweep's own row. `nextAt` is stored rather than derived from
 * `lastReapAt`, so a cold activation re-arms the instant the last one chose
 * instead of pushing it out by however long the cell was asleep. */
interface ReapState {
  nextAt: string;
  /** Where the current walk resumes; null between sweeps. */
  cursor: string | null;
  lastReapAt: string | null;
}

/** The migration run this fleet is under, as the verifier and the sweep
 * both read it. */
export interface MigrationRun {
  /** sha256 of the snapshot the run is replaying. */
  snapshot: string;
  openedAt: string;
}

export class DirectoryCell extends DurableObject<Env> implements Directory {
  private readonly c: Composition;
  private readonly storage: CellStorage;
  private readonly repo: Sync<Directory>;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const storage = ctx.storage as unknown as CellStorage;
    const c = compose(env);
    this.c = c;
    this.storage = storage;
    void ctx.blockConcurrencyWhile(async () => {
      c.migrateDirectory(storage);
      await this.ensureAlarm();
    });
    this.repo = c.directoryRepo(storage);
  }

  /** The directory's own rows, in rowid order. Parity only: a test that
   * used to read the merge audit out of the shared database has nowhere
   * else to look for it. */
  async dumpTables(): Promise<Record<string, Record<string, unknown>[]>> {
    const tables: Record<string, Record<string, unknown>[]> = {};
    for (const table of DUMP_TABLES) {
      tables[table] = this.storage.sql.exec(`SELECT * FROM "${table}" ORDER BY rowid`).toArray();
    }
    return tables;
  }

  /** The verifier's read of the directory: `users` against the exporter's
   * own ranking, `account_merges` field by field. */
  async dumpPage(table: string, after: number | null, limit: number, columns: readonly string[] | null): Promise<DumpPage> {
    return pageByRowid(this.storage.sql, table, { after, limit, columns: columns ?? undefined });
  }

  /** The migration seal, which the entry worker reads before every
   * `/_migrate/*` call. It lives here because there is one directory and the
   * flag is fleet-wide. */
  async sealMigration(): Promise<void> {
    await this.storage.put<boolean>(MIGRATION_SEAL_KEY, true);
    // The cutover is over, so the retention sweep goes back on.
    await this.storage.delete(MIGRATION_RUN_KEY);
  }

  async migrationSealed(): Promise<boolean> {
    return (await this.storage.get<boolean>(MIGRATION_SEAL_KEY)) === true;
  }

  /**
   * Opens a run against one snapshot: the digest the verifier reads back,
   * and the instant that holds the retention sweep off until the seal. Sent
   * once per run, before any user, so a run killed halfway still says which
   * snapshot the rows on this fleet came from.
   */
  async beginMigrationRun(snapshot: string): Promise<MigrationRun> {
    const run: MigrationRun = { snapshot, openedAt: isoUtc(this.c.clock.now()) };
    await this.storage.put<MigrationRun>(MIGRATION_RUN_KEY, run);
    return run;
  }

  async migrationRun(): Promise<MigrationRun | null> {
    return (await this.storage.get<MigrationRun>(MIGRATION_RUN_KEY)) ?? null;
  }

  /** The migration's copy of a global table this cell owns: `account_merges`,
   * ids preserved because they are the source of `previous_ids`. Keyed by
   * that id, so a replay inserts nothing. */
  async importMigrationRows(table: string, rows: readonly Record<string, unknown>[]): Promise<number> {
    return this.storage.transactionSync(() => this.c.importGlobalRows(this.storage, table, rows));
  }

  async migrationCounts(tables: readonly string[]): Promise<Record<string, number>> {
    const out: Record<string, number> = {};
    for (const table of tables) out[table] = this.c.countRows(this.storage, table);
    return out;
  }

  async register(id: string, isAnonymous: boolean, at: string, opts?: { idx?: number }): Promise<{ idx: number }> {
    return this.repo.register(id, isAnonymous, at, opts);
  }

  async lookup(id: string): Promise<DirectoryUser | null> {
    return this.repo.lookup(id);
  }

  async beginMerge(anonId: string, targetId: string, at: string): Promise<{ auditId: number; marker: MergeMarker }> {
    return this.repo.beginMerge(anonId, targetId, at);
  }

  async completeMerge(auditId: number, counts: Record<string, number>, at: string): Promise<void> {
    this.repo.completeMerge(auditId, counts, at);
  }

  async failMerge(auditId: number, error: string, at: string): Promise<void> {
    this.repo.failMerge(auditId, error, at);
  }

  async noteMergeAttempt(anonId: string): Promise<number> {
    return this.repo.noteMergeAttempt(anonId);
  }

  async marker(anonId: string): Promise<MergeMarker | null> {
    return this.repo.marker(anonId);
  }

  async clearMarker(anonId: string): Promise<void> {
    this.repo.clearMarker(anonId);
  }

  async previousIds(targetId: string): Promise<string[]> {
    return this.repo.previousIds(targetId);
  }

  async audit(auditId: number): Promise<MergeAudit | null> {
    return this.repo.audit(auditId);
  }

  async tombstone(id: string, reason: TombstoneReason, at: string): Promise<void> {
    this.repo.tombstone(id, reason, at);
  }

  async tombstoneOf(id: string): Promise<{ reason: TombstoneReason; at: string } | null> {
    return this.repo.tombstoneOf(id);
  }

  async remove(id: string): Promise<void> {
    this.repo.remove(id);
  }

  async listAnonymous(after: string | null, limit: number): Promise<DirectoryUser[]> {
    return this.repo.listAnonymous(after, limit);
  }

  async fetch(_request: Request): Promise<Response> {
    return new Response('rpc only', { status: 501 });
  }

  // ---- the daily sweep ----------------------------------------------------------

  /**
   * One page of the retention walk. A page that comes back full leaves the
   * cursor set and wakes again shortly; a short one closes the sweep and the
   * next is a day out. One account's failure costs one account, and the walk
   * that reaches it again re-selects it.
   */
  async alarm(): Promise<void> {
    if (!this.c.periodicWork) return;
    const state = await this.reapState();
    const now = this.c.clock.now();
    if (parseIso(state.nextAt).getTime() > now.getTime()) {
      await this.ensureAlarm();
      return;
    }
    // A run in flight registers accounts whose cells are not written yet,
    // against Python `created_at` values years old. Sweeping now would read
    // them as idle and destroy them, and a reaped cell refuses every later
    // chunk forever, so the walk waits for the seal.
    if (migrationHolds((await this.migrationRun())?.openedAt ?? null, now)) {
      await this.storage.put<ReapState>(REAP_STATE_KEY, { ...state, nextAt: isoUtc(new Date(now.getTime() + MIGRATION_RECHECK_MS)) });
      await this.ensureAlarm();
      return;
    }
    const report = await reapIdleAnonymous(
      { cells: this.c.userCells, jobs: this.c.jobCells, directory: this, clock: this.c.clock },
      { after: state.cursor },
    );
    const done = report.cursor === null;
    await this.storage.put<ReapState>(REAP_STATE_KEY, {
      nextAt: isoUtc(new Date(this.c.clock.now().getTime() + (done ? DAY_MS : PAGE_GAP_MS))),
      cursor: report.cursor,
      lastReapAt: done ? isoUtc(now) : state.lastReapAt,
    });
    await this.ensureAlarm();
  }

  /** The row, or the one a directory that has never swept starts from: due
   * now, so the first activation of a fresh deployment sweeps once. */
  private async reapState(): Promise<ReapState> {
    const stored = await this.storage.get<ReapState>(REAP_STATE_KEY);
    return stored ?? { nextAt: isoUtc(this.c.clock.now()), cursor: null, lastReapAt: null };
  }

  /** Same kill switch the user cells honour: a parity target pins the clock,
   * so a never-swept directory would read as due at once and destroy accounts
   * under the corpus. */
  private async ensureAlarm(): Promise<void> {
    const current = await this.storage.getAlarm();
    if (!this.c.periodicWork) {
      if (current !== null) await this.storage.deleteAlarm();
      return;
    }
    const state = await this.reapState();
    const target = Math.max(parseIso(state.nextAt).getTime(), this.c.clock.now().getTime() + ALARM_FLOOR_MS);
    if (current !== target) await this.storage.setAlarm(target);
  }
}
