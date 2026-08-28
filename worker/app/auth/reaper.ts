// The sweep that deletes anonymous accounts nobody came back to. It walks the
// directory's anonymous ids in id order behind a cursor, so a tick's work is
// bounded and the next tick continues where this one stopped rather than
// re-reading the same head of the list.
import type { Directory, UserCells } from '../ports.js';
import { BATCH_LIMIT, cutoffFor, isIdle } from '../../domain/reaper.js';
import { destroyAccount, type DestroyDeps } from './mergeSaga.js';

export interface ReapDeps extends DestroyDeps {
  cells: UserCells;
  directory: Directory;
}

export interface ReapOptions {
  /** The id the last walk stopped at; null starts the sweep over. */
  after?: string | null;
  limit?: number;
}

export interface ReapReport {
  scanned: number;
  reaped: number;
  /** Already tombstoned: an earlier delete that never reached the directory. */
  cleaned: number;
  /** Registered with nothing in its cell yet, so there is no date to judge
   * it by. Counted rather than folded into `scanned`: a number that stays up
   * across sweeps is registrations whose first write never landed. */
  skipped: number;
  failed: number;
  /** Where the next walk resumes; null when the list is exhausted. */
  cursor: string | null;
}

/**
 * One walk. Each account is destroyed on its own, so one failure costs one
 * account rather than the batch: the next walk re-reads whatever is left, and
 * a half-destroyed account converges because every step of the deletion is
 * idempotent.
 */
export async function reapIdleAnonymous(deps: ReapDeps, opts: ReapOptions = {}): Promise<ReapReport> {
  const limit = opts.limit ?? BATCH_LIMIT;
  const cutoff = cutoffFor(deps.clock.now());
  const page = await deps.directory.listAnonymous(opts.after ?? null, limit);
  let reaped = 0;
  let cleaned = 0;
  let skipped = 0;
  let failed = 0;
  for (const user of page) {
    try {
      if (await deps.directory.tombstoneOf(user.id)) {
        await deps.directory.remove(user.id);
        cleaned++;
        continue;
      }
      // A merge already owns this account's rows. Destroying it now would
      // leave the saga reading an empty cell and recording a merge that
      // moved nothing, with the cookie that could have retried deleted.
      if (await deps.directory.marker(user.id)) continue;
      // The cell's own date, or nothing. The directory row's `created_at` is
      // not a stand-in for it: a migrated account's is years old while its
      // cell is still being written, and reaping is one-way.
      const lastSeen = await deps.cells.cell(user.id).lastSeenAt();
      if (lastSeen === null) {
        // No profile is two different accounts. A tombstoned cell is a
        // delete that stopped before the directory and has to finish; an
        // untombstoned one is a register whose first write has not landed,
        // and there is no date to judge it by.
        if (!(await deps.cells.cell(user.id).precheck()).tombstoned) {
          skipped++;
          continue;
        }
        await destroyAccount(user.id, 'reaped', deps);
        reaped++;
        continue;
      }
      if (!isIdle(lastSeen, cutoff)) continue;
      await destroyAccount(user.id, 'reaped', deps);
      reaped++;
    } catch {
      failed++;
    }
  }
  return { scanned: page.length, reaped, cleaned, skipped, failed, cursor: page.length < limit ? null : (page[page.length - 1]?.id ?? null) };
}
