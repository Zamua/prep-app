// The instant-generation ledger and its windows. RPC only.
import { DurableObject } from 'cloudflare:workers';
import type { LedgerReset, Limiter, ReserveResult, Sync } from '../../app/ports.js';
import { compose, type Composition } from '../compose.js';
import type { Env } from '../env.js';
import { pageByRowid, type CellStorage, type DumpPage } from '../storage.js';

export class InstantLimiterCell extends DurableObject<Env> implements Limiter {
  private readonly c: Composition;
  private readonly repo: Sync<Limiter> & LedgerReset;
  private readonly storage: CellStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const storage = ctx.storage as unknown as CellStorage;
    const c = compose(env);
    void ctx.blockConcurrencyWhile(async () => {
      c.migrateLimiter(storage);
    });
    this.c = c;
    this.storage = storage;
    this.repo = c.limiterRepo(storage);
  }

  /** The verifier's read of the ledger, which the migration carries as a
   * trailing 48 h window rather than resetting. */
  async dumpPage(table: string, after: number | null, limit: number, columns: readonly string[] | null): Promise<DumpPage> {
    return pageByRowid(this.storage.sql, table, { after, limit, columns: columns ?? undefined });
  }

  /** The migration's copy of the ledger, keyed by the id it preserves, so a
   * replay inserts nothing. */
  async importMigrationRows(table: string, rows: readonly Record<string, unknown>[]): Promise<number> {
    return this.storage.transactionSync(() => this.c.importGlobalRows(this.storage, table, rows));
  }

  async migrationCounts(tables: readonly string[]): Promise<Record<string, number>> {
    const out: Record<string, number> = {};
    for (const table of tables) out[table] = this.c.countRows(this.storage, table);
    return out;
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

  /** Test only, from `POST /_test/seed`: the ledger is global, so a
   * run's spend has to leave with the data it was recorded against. */
  async wipe(): Promise<void> {
    this.repo.wipe();
  }

  async fetch(_request: Request): Promise<Response> {
    return new Response('rpc only', { status: 501 });
  }
}
