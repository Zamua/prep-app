// What the resolver does with an anonymous id that is no longer live. The
// cell answers for itself: it checks its own tombstone before serving, so a
// cookie naming a merged, reaped or deleted id cannot resurrect an empty
// account and no directory read sits on the request path. The marker covers
// the one window the tombstone does not, between a merge starting and the
// anonymous cell being destroyed.
import type { MergeMarker } from '../entities.js';
import type { Precheck } from '../ports.js';
import type { MergeResult } from '../../domain/merge.js';

/** The rows are moving to another account right now. */
export const GONE_MERGING = 'merging';
/** No cell and no tombstone: reaped long enough ago to have been forgotten. */
export const GONE_MISSING = 'missing';

export type AnonAccess = { kind: 'serve' } | { kind: 'gone'; reason: string };

/** A tombstoned, missing or merging id is never served: a write against it is
 * lost, and serving the empty cell would look like data loss to the person. */
export function anonAccess(state: Precheck, marker: MergeMarker | null = null): AnonAccess {
  if (state.tombstoned) return { kind: 'gone', reason: state.tombstoned };
  if (marker) return { kind: 'gone', reason: GONE_MERGING };
  if (!state.exists) return { kind: 'gone', reason: GONE_MISSING };
  return { kind: 'serve' };
}

export type CookieVerdict = 'keep' | 'clear';

/** Cleared exactly when the merge resolved the cookie. Collapsing `resolved`
 * into `merged` is how a cookie gets dropped on a failure path. */
export function cookieVerdict(result: MergeResult): CookieVerdict {
  return result.resolved ? 'clear' : 'keep';
}
