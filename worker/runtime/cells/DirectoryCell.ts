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
import { isoUtc, parseIso } from '../../domain/py.js';
import { compose, type Composition } from '../compose.js';
import type { Env } from '../env.js';
import type { CellStorage } from '../storage.js';

const REAP_STATE_KEY = 'reap';
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
    const state = await this.reapState();
    const now = this.c.clock.now();
    if (parseIso(state.nextAt).getTime() > now.getTime()) {
      await this.ensureAlarm();
      return;
    }
    const report = await reapIdleAnonymous(
      { cells: this.c.userCells, directory: this, clock: this.c.clock },
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

  private async ensureAlarm(): Promise<void> {
    const state = await this.reapState();
    const target = Math.max(parseIso(state.nextAt).getTime(), this.c.clock.now().getTime() + ALARM_FLOOR_MS);
    const current = await this.storage.getAlarm();
    if (current !== target) await this.storage.setAlarm(target);
  }
}
