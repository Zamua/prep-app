// The DirectoryCell's tables: enumeration data written at create, merge and
// delete, the merge audit (the source of `previous_ids`), markers and
// tombstones.
import type { DirectoryUser, MergeAudit, MergeMarker, TombstoneReason } from '../../../app/entities.js';
import { ChunkRejected } from '../../../domain/migrate.js';
import { Db, type CellStorage, type Row } from './storage.js';

const toUser = (r: Row): DirectoryUser => ({
  id: String(r['id']),
  is_anonymous: Boolean(r['is_anonymous']),
  created_at: String(r['created_at']),
  idx: Number(r['idx']),
});

const toMarker = (r: Row): MergeMarker => ({
  anon_id: String(r['anon_id']),
  target_id: String(r['target_id']),
  audit_id: Number(r['audit_id']),
  started_at: String(r['started_at']),
});

/** The `Directory` port, synchronous; the cell wraps it in RPC. */
export class SqlDirectoryRepo {
  private readonly db: Db;

  constructor(private readonly storage: CellStorage) {
    this.db = new Db(storage.sql);
  }

  register(id: string, isAnonymous: boolean, at: string, opts: { idx?: number } = {}): { idx: number } {
    return this.storage.transactionSync(() => {
      const existing = this.db.first<{ idx: number }>('SELECT idx FROM users WHERE id = ?', id);
      if (existing) return { idx: Number(existing.idx) };
      let idx = opts.idx;
      if (idx === undefined) {
        const row = this.db.first<{ m: number | null }>('SELECT MAX(idx) AS m FROM users');
        idx = Number(row?.m ?? 0) + 1;
      } else {
        // `idx` is UNIQUE, so a caller naming one another account holds would
        // raise a bare SqliteError: a 500 with an HTML body, and one the RPC
        // layer spends its whole backoff on. Named instead, because which two
        // accounts collided is the entire diagnosis.
        const taken = this.db.first<{ id: string }>('SELECT id FROM users WHERE idx = ?', idx);
        if (taken) throw new ChunkRejected(`idx ${idx} already belongs to ${String(taken.id)}, not ${id}`);
      }
      this.db.run('INSERT INTO users (id, is_anonymous, created_at, idx) VALUES (?, ?, ?, ?)', id, isAnonymous ? 1 : 0, at, idx);
      return { idx };
    });
  }

  lookup(id: string): DirectoryUser | null {
    const row = this.db.first('SELECT id, is_anonymous, created_at, idx FROM users WHERE id = ?', id);
    return row ? toUser(row) : null;
  }

  beginMerge(anonId: string, targetId: string, at: string): { auditId: number; marker: MergeMarker } {
    return this.storage.transactionSync(() => {
      const existing = this.db.first('SELECT anon_id, target_id, audit_id, started_at FROM merge_markers WHERE anon_id = ?', anonId);
      if (existing) {
        const marker = toMarker(existing);
        return { auditId: marker.audit_id, marker };
      }
      const auditId = this.db.insert(
        `INSERT INTO account_merges (anon_user_id, target_user_id, started_at, status) VALUES (?, ?, ?, 'started')`,
        anonId,
        targetId,
        at,
      );
      this.db.run('INSERT INTO merge_markers (anon_id, target_id, audit_id, started_at) VALUES (?, ?, ?, ?)', anonId, targetId, auditId, at);
      return { auditId, marker: { anon_id: anonId, target_id: targetId, audit_id: auditId, started_at: at } };
    });
  }

  completeMerge(auditId: number, counts: Record<string, number>, at: string): void {
    this.db.run(
      `UPDATE account_merges SET status = 'completed', completed_at = ?, counts = ? WHERE id = ? AND status = 'started'`,
      at,
      JSON.stringify(counts),
      auditId,
    );
  }

  failMerge(auditId: number, error: string, at: string): void {
    this.storage.transactionSync(() => {
      this.db.run(`UPDATE account_merges SET status = 'failed', completed_at = ?, error = ? WHERE id = ? AND status = 'started'`, at, error, auditId);
      this.db.run('DELETE FROM merge_markers WHERE audit_id = ?', auditId);
    });
  }

  /** One attempt counted against the marker, and the count now standing. A
   * marker that has since been cleared reports zero: the saga is over. */
  noteMergeAttempt(anonId: string): number {
    return this.storage.transactionSync(() => {
      this.db.run('UPDATE merge_markers SET attempts = attempts + 1 WHERE anon_id = ?', anonId);
      const row = this.db.first<{ attempts: number }>('SELECT attempts FROM merge_markers WHERE anon_id = ?', anonId);
      return Number(row?.attempts ?? 0);
    });
  }

  marker(anonId: string): MergeMarker | null {
    const row = this.db.first('SELECT anon_id, target_id, audit_id, started_at FROM merge_markers WHERE anon_id = ?', anonId);
    return row ? toMarker(row) : null;
  }

  clearMarker(anonId: string): void {
    this.db.run('DELETE FROM merge_markers WHERE anon_id = ?', anonId);
  }

  /** Ids the target used to have, oldest merge first. */
  previousIds(targetId: string): string[] {
    return this.db
      .all<{ anon_user_id: string }>(`SELECT anon_user_id FROM account_merges WHERE target_user_id = ? AND status = 'completed' ORDER BY id`, targetId)
      .map((r) => r.anon_user_id);
  }

  audit(auditId: number): MergeAudit | null {
    const r = this.db.first('SELECT * FROM account_merges WHERE id = ?', auditId);
    if (!r) return null;
    return {
      id: Number(r['id']),
      anon_user_id: String(r['anon_user_id']),
      target_user_id: String(r['target_user_id']),
      started_at: String(r['started_at']),
      completed_at: (r['completed_at'] as string | null) ?? null,
      status: String(r['status']),
      counts: r['counts'] ? (JSON.parse(String(r['counts'])) as Record<string, number>) : null,
      error: (r['error'] as string | null) ?? null,
    };
  }

  tombstone(id: string, reason: TombstoneReason, at: string): void {
    this.db.run('INSERT OR IGNORE INTO tombstones (id, reason, at) VALUES (?, ?, ?)', id, reason, at);
  }

  tombstoneOf(id: string): { reason: TombstoneReason; at: string } | null {
    const row = this.db.first('SELECT reason, at FROM tombstones WHERE id = ?', id);
    return row ? { reason: String(row['reason']) as TombstoneReason, at: String(row['at']) } : null;
  }

  remove(id: string): void {
    this.db.run('DELETE FROM users WHERE id = ?', id);
  }

  /** Anonymous ids after `after` (exclusive), ascending, for the reaper's walk. */
  listAnonymous(after: string | null, limit: number): DirectoryUser[] {
    return this.db
      .all('SELECT id, is_anonymous, created_at, idx FROM users WHERE is_anonymous = 1 AND id > ? ORDER BY id LIMIT ?', after ?? '', limit)
      .map(toUser);
  }
}
