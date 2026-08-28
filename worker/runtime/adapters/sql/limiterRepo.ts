// The InstantLimiterCell's ledger: `instant_generations`.
// The windows are the domain's; the cell reads the last day and reserves.
import type { ReserveResult } from '../../../app/ports.js';
import {
  checkWindows,
  DAY_WINDOW_S,
  RETENTION_DAYS,
  TERMINAL_OUTCOMES,
  type GenerationRow,
  type Limits,
} from '../../../domain/instant/limiter.js';
import { parseIso } from '../../../domain/time.js';
import { Db, type CellStorage } from './storage.js';
import { DAY_MS, isoUtc, shifted } from './time.js';

export interface ReserveRequest {
  ip: string;
  topicChars: number;
  userId: string | null;
  userIsAnonymous: boolean | null;
  at: string;
}

export class SqlLimiterRepo {
  private readonly db: Db;

  constructor(
    private readonly storage: CellStorage,
    private readonly limits: Limits,
  ) {
    this.db = new Db(storage.sql);
  }

  /** The whole ledger, for the parity seed: a durable limiter cell would
   * otherwise carry one run's spend into the next against a pinned clock. */
  wipe(): void {
    this.db.run('DELETE FROM instant_generations');
  }

  reserve(req: ReserveRequest): ReserveResult {
    const at = parseIso(req.at);
    return this.storage.transactionSync(() => {
      this.db.run('DELETE FROM instant_generations WHERE created_at < ?', isoUtc(shifted(at, -RETENTION_DAYS * DAY_MS)));
      const rows = this.db.all<GenerationRow & Record<string, string | null>>(
        'SELECT ip, created_at, outcome, user_id FROM instant_generations WHERE created_at >= ?',
        isoUtc(shifted(at, -DAY_WINDOW_S * 1000)),
      );
      const refusal = checkWindows(rows, { ip: req.ip, userId: req.userId, userIsAnonymous: req.userIsAnonymous, at }, this.limits);
      if (refusal) return { refusal };
      const id = this.db.insert(
        `INSERT INTO instant_generations (ip, created_at, outcome, topic_chars, user_id) VALUES (?, ?, 'pending', ?, ?)`,
        req.ip,
        isoUtc(at),
        req.topicChars,
        req.userId,
      );
      return { reservation: { id } };
    });
  }

  /** Never clears a user id the reservation already carries. */
  resolve(id: number, outcome: string, cards: number | null, userId: string | null): void {
    if (!(TERMINAL_OUTCOMES as readonly string[]).includes(outcome)) throw new RangeError(`unknown outcome: ${JSON.stringify(outcome)}`);
    this.db.run('UPDATE instant_generations SET outcome = ?, cards = ?, user_id = COALESCE(?, user_id) WHERE id = ?', outcome, cards, userId, id);
  }

  /** The merge's reassign rule over the ledger: the anonymous account's spend
   * follows it onto the target, so signing in never hands out a fresh quota. */
  reassign(fromId: string, toId: string): number {
    return this.db.run('UPDATE instant_generations SET user_id = ? WHERE user_id = ?', toId, fromId);
  }

  rows(): GenerationRow[] {
    return this.db.all<GenerationRow & Record<string, string | null>>('SELECT ip, created_at, outcome, user_id FROM instant_generations ORDER BY id');
  }
}
