// The instant-generation ledger and its windows. RPC only.
import { DurableObject } from 'cloudflare:workers';
import type { Limiter, ReserveResult, Sync } from '../../app/ports.js';
import { compose } from '../compose.js';
import type { Env } from '../env.js';
import type { CellStorage } from '../storage.js';

export class InstantLimiterCell extends DurableObject<Env> implements Limiter {
  private readonly repo: Sync<Limiter>;

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

  async fetch(_request: Request): Promise<Response> {
    return new Response('rpc only', { status: 501 });
  }
}
