// In-memory stand-ins for the global cells and the user cells, for the
// sagas: the directory and limiter keep their rows in maps; a user cell is
// the real UserCell over fake storage, so dump, import and destroy are real.
import type { DirectoryUser, MergeAudit, MergeMarker, TombstoneReason } from '../../app/entities.js';
import type { Directory, Limiter, ReserveResult, UserCellRpc, UserCells } from '../../app/ports.js';
import { checkWindows, DEFAULT_LIMITS, RETENTION_DAYS, TERMINAL_OUTCOMES, type GenerationRow, type Limits } from '../../domain/instant/limiter.js';
import { parseIso } from '../../domain/py.js';
import { UserCell } from '../../runtime/cells/UserCell.js';
import type { Env } from '../../runtime/env.js';
import { fakeCellState, type FakeCellStorage } from './sqlStorage.js';

export class FakeDirectory implements Directory {
  users = new Map<string, DirectoryUser>();
  merges: MergeAudit[] = [];
  markers = new Map<string, MergeMarker>();
  attempts = new Map<string, number>();
  tombstones = new Map<string, { reason: TombstoneReason; at: string }>();

  async register(id: string, isAnonymous: boolean, at: string, opts: { idx?: number } = {}): Promise<{ idx: number }> {
    const existing = this.users.get(id);
    if (existing) return { idx: existing.idx };
    const idx = opts.idx ?? Math.max(0, ...[...this.users.values()].map((u) => u.idx)) + 1;
    this.users.set(id, { id, is_anonymous: isAnonymous, created_at: at, idx });
    return { idx };
  }

  async lookup(id: string): Promise<DirectoryUser | null> {
    return this.users.get(id) ?? null;
  }

  async beginMerge(anonId: string, targetId: string, at: string): Promise<{ auditId: number; marker: MergeMarker }> {
    const existing = this.markers.get(anonId);
    if (existing) return { auditId: existing.audit_id, marker: existing };
    const id = this.merges.length + 1;
    this.merges.push({ id, anon_user_id: anonId, target_user_id: targetId, started_at: at, completed_at: null, status: 'started', counts: null, error: null });
    const marker = { anon_id: anonId, target_id: targetId, audit_id: id, started_at: at };
    this.markers.set(anonId, marker);
    return { auditId: id, marker };
  }

  async completeMerge(auditId: number, counts: Record<string, number>, at: string): Promise<void> {
    const m = this.merges.find((x) => x.id === auditId);
    if (m && m.status === 'started') Object.assign(m, { status: 'completed', completed_at: at, counts });
  }

  async failMerge(auditId: number, error: string, at: string): Promise<void> {
    const m = this.merges.find((x) => x.id === auditId);
    if (m && m.status === 'started') {
      Object.assign(m, { status: 'failed', completed_at: at, error });
      this.markers.delete(m.anon_user_id);
      this.attempts.delete(m.anon_user_id);
    }
  }

  async noteMergeAttempt(anonId: string): Promise<number> {
    if (!this.markers.has(anonId)) return 0;
    const next = (this.attempts.get(anonId) ?? 0) + 1;
    this.attempts.set(anonId, next);
    return next;
  }

  async marker(anonId: string): Promise<MergeMarker | null> {
    return this.markers.get(anonId) ?? null;
  }

  async clearMarker(anonId: string): Promise<void> {
    this.markers.delete(anonId);
    this.attempts.delete(anonId);
  }

  async previousIds(targetId: string): Promise<string[]> {
    return this.merges.filter((m) => m.target_user_id === targetId && m.status === 'completed').map((m) => m.anon_user_id);
  }

  async audit(auditId: number): Promise<MergeAudit | null> {
    return this.merges.find((m) => m.id === auditId) ?? null;
  }

  async tombstone(id: string, reason: TombstoneReason, at: string): Promise<void> {
    if (!this.tombstones.has(id)) this.tombstones.set(id, { reason, at });
  }

  async tombstoneOf(id: string): Promise<{ reason: TombstoneReason; at: string } | null> {
    return this.tombstones.get(id) ?? null;
  }

  async remove(id: string): Promise<void> {
    this.users.delete(id);
  }

  async listAnonymous(after: string | null, limit: number): Promise<DirectoryUser[]> {
    return [...this.users.values()]
      .filter((u) => u.is_anonymous && u.id > (after ?? ''))
      .sort((a, b) => (a.id < b.id ? -1 : 1))
      .slice(0, limit);
  }
}

export class FakeLimiter implements Limiter {
  rows: (GenerationRow & { id: number; cards: number | null; topic_chars: number })[] = [];
  private nextId = 1;

  constructor(readonly limits: Limits = DEFAULT_LIMITS) {}

  async reserve(req: { ip: string; topicChars: number; userId: string | null; userIsAnonymous: boolean | null; at: string }): Promise<ReserveResult> {
    const at = parseIso(req.at);
    const keep = at.getTime() - RETENTION_DAYS * 86_400_000;
    this.rows = this.rows.filter((r) => r.created_at !== null && parseIso(r.created_at).getTime() >= keep);
    const refusal = checkWindows(this.rows, { ip: req.ip, userId: req.userId, userIsAnonymous: req.userIsAnonymous, at }, this.limits);
    if (refusal) return { refusal };
    const id = this.nextId++;
    this.rows.push({ id, ip: req.ip, created_at: req.at, outcome: 'pending', user_id: req.userId, cards: null, topic_chars: req.topicChars });
    return { reservation: { id } };
  }

  async resolve(id: number, outcome: 'ok' | 'failed_spent' | 'failed_free', cards: number | null, userId: string | null): Promise<void> {
    if (!(TERMINAL_OUTCOMES as readonly string[]).includes(outcome)) throw new RangeError(`unknown outcome: ${outcome}`);
    const row = this.rows.find((r) => r.id === id);
    if (row) Object.assign(row, { outcome, cards, user_id: userId ?? row.user_id });
  }

  async reassign(fromId: string, toId: string): Promise<number> {
    const moving = this.rows.filter((r) => r.user_id === fromId);
    for (const row of moving) row.user_id = toId;
    return moving.length;
  }
}

/** Real user cells over fake storage, keyed by id. */
export class FakeUserCells implements UserCells {
  readonly cells = new Map<string, { cell: UserCell; storage: FakeCellStorage }>();

  constructor(private readonly env: Env) {}

  cell(id: string): UserCellRpc {
    return this.entry(id).cell;
  }

  entry(id: string): { cell: UserCell; storage: FakeCellStorage } {
    let e = this.cells.get(id);
    if (!e) {
      const state = fakeCellState();
      e = { cell: new UserCell(state, this.env), storage: state.fake };
      this.cells.set(id, e);
    }
    return e;
  }
}
