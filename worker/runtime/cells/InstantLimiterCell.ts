// The instant-generation ledger and its windows. RPC only.
import { DurableObject } from 'cloudflare:workers';
import type { LedgerReset, Limiter, ReserveResult, Sync } from '../../app/ports.js';
import { compose } from '../compose.js';
import type { Env } from '../env.js';
import type { CellStorage } from '../storage.js';

export class InstantLimiterCell extends DurableObject<Env> implements Limiter {
  private readonly repo: Sync<Limiter> & LedgerReset;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const storage = ctx.storage as unknown as CellStorage;
    const c = compose(env);
    void ctx.blockConcurrencyWhile(async () => {
      c.migrateLimiter(storage);
    });
    this.repo = c.limiterRepo(storage);
  }

  async reserve(req: { ip: string; topicChars: number; userId: string | null; userIsAnonymous: boolean | null; at: string }): Promise<ReserveResult> {
    return this.repo.reserve(req);
  }

  async resolve(id: number, outcome: 'ok' | 'failed_spent' | 'failed_free', cards: number | null, userId: string | null): Promise<void> {
    this.repo.resolve(id, outcome, cards, userId);
  }

  async reassign(fromId: string, toId: string): Promise<number> {
    return this.repo.reassign(fromId, toId);
  }

  /** Parity only, from `POST /_parity/seed`: the ledger is global, so a
   * run's spend has to leave with the data it was recorded against. */
  async wipe(): Promise<void> {
    this.repo.wipe();
  }

  async fetch(_request: Request): Promise<Response> {
    return new Response('rpc only', { status: 501 });
  }
}
