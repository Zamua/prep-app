// When an anonymous account stops coming back. Nobody asked for the account,
// so nothing deletes it on the person's behalf; this window is that delete.
// It exceeds the cookie's own lifetime on purpose, so a cookie that still
// verifies names a live account and "valid cookie, missing user" stays a rare
// path rather than a routine one.
import { isoUtc, parseIso } from './py.js';

export const IDLE_DAYS = 365;

/** Accounts per walk. The walk carries a cursor, so the batch bounds one
 * tick's work rather than the sweep. */
export const BATCH_LIMIT = 50;

const DAY_MS = 86_400_000;

export function cutoffFor(now: Date): string {
  return isoUtc(new Date(now.getTime() - IDLE_DAYS * DAY_MS));
}

/** Strict: an account last seen at the cutoff instant survives. */
export function isIdle(lastSeenAt: string, cutoff: string): boolean {
  return parseIso(lastSeenAt).getTime() < parseIso(cutoff).getTime();
}

/**
 * How long an open migration run holds the sweep off.
 *
 * A migration registers an account before its cell holds anything, and a
 * migrated `created_at` is Python's, years old, so a sweep landing in that
 * gap destroys a live account permanently: the cell tombstones, every later
 * chunk for it is refused, and nothing un-tombstones a cell. The hold has to
 * cover the whole cutover, which is why it is days rather than minutes; the
 * seal lifts it, and this is only the backstop for a run rolled back and
 * never sealed.
 */
export const MIGRATION_HOLD_MS = 30 * DAY_MS;

/** How long a held sweep waits before looking again. */
export const MIGRATION_RECHECK_MS = 3_600_000;

/** True while a run opened at `openedAt` still holds the sweep off. */
export function migrationHolds(openedAt: string | null, now: Date): boolean {
  if (openedAt === null) return false;
  return now.getTime() - parseIso(openedAt).getTime() < MIGRATION_HOLD_MS;
}
