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
