// The directory: enumeration data written at create, merge and delete, the
// merge audit and markers, tombstones. RPC only; its fetch answers nothing.
import { DurableObject } from 'cloudflare:workers';
import type { Directory, Sync } from '../../app/ports.js';
import type { DirectoryUser, MergeAudit, MergeMarker, TombstoneReason } from '../../app/entities.js';
import { compose } from '../compose.js';
import type { Env } from '../env.js';
import type { CellStorage } from '../storage.js';

export class DirectoryCell extends DurableObject<Env> implements Directory {
  private readonly repo: Sync<Directory>;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const storage = ctx.storage as unknown as CellStorage;
    const c = compose(env);
    void ctx.blockConcurrencyWhile(async () => {
      c.migrateDirectory(storage);
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
}
