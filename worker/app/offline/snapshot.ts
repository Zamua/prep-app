// The read-only client snapshot, transcribed from prep/offline/routes.py.
// The identity in the payload is display-only on the client; the sync
// endpoint never trusts a client-side ownership claim.
import { isoUtc } from '../../domain/time.js';
import { json, type ApiResult } from '../http.js';
import type { Clock, UserRepos } from '../ports.js';

export interface SnapshotIdentity {
  id: string;
  displayName: string | null;
  /** The accounts merged into this one, so a device stamped with a
   * merged-away id recognises itself instead of raising the mismatch. */
  previousIds: string[];
}

export function snapshot(deps: { repos: UserRepos; clock: Clock }, who: SnapshotIdentity): ApiResult {
  return json({
    user: { id: who.id, display_name: who.displayName || who.id, previous_ids: who.previousIds },
    generated_at: isoUtc(deps.clock.now()),
    decks: deps.repos.offline.snapshotDecks(),
    cards: deps.repos.offline.snapshotCards(),
  });
}
