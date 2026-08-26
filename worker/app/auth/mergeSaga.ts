// Merging an anonymous account into a provider account across cells. Python
// did this in one transaction; two cells cannot, so the directory's marker
// and audit row bracket a saga: dump, the domain's policy, an import
// idempotent by row id, then the anonymous cell's three-step deletion. Every
// step is retry-safe and the marker is what a later request resumes from, so
// a crash anywhere leaves the anonymous account still owning its rows or the
// target already holding them, never half of each.
//
// A resumed attempt recomputes what is left to do, so the audit counts
// describe the attempt that completed rather than the union of the attempts:
// rows a previous attempt moved are already the target's and count for
// nobody. The rows converge; the counts are an operator's record of the run.
import type { CellSnapshot, TombstoneReason } from '../entities.js';
import type { CarriedPreferences, Clock, Directory, Limiter, Random, UserCells } from '../ports.js';
import {
  CARRIED_USER_COLUMNS,
  DELETE,
  POLICY,
  mergeRows,
  precheck,
  rowKey,
  type Counts,
  type MergeResult,
  type RandomHex,
  type Row,
  type Snapshot,
} from '../../domain/merge.js';
import { isoUtc } from '../../domain/py.js';

/** The instant ledger lives in the limiter cell, not a user cell, so its
 * reassign rule travels through the `Limiter` port instead of the import. */
const LIMITER_TABLE = 'instant_generations';

const MERGED: TombstoneReason = 'merged';

/** Another merge already owns this anonymous id: its rows are going
 * somewhere else, and this cookie is not ours to resolve. */
export const MERGE_IN_PROGRESS = 'merge_in_progress';

export interface DestroyDeps {
  cells: UserCells;
  directory: Directory;
  clock: Clock;
}

export interface MergeDeps extends DestroyDeps {
  limiter: Limiter;
  randomHex: RandomHex;
}

/** `secrets.token_hex` over the `Random` port: the slug de-collision's tail. */
export function hexFrom(random: Random): RandomHex {
  return (bytes: number) =>
    Array.from(random.bytes(bytes), (b) => b.toString(16).padStart(2, '0')).join('');
}

const refusal = (reason: string, resolved: boolean): MergeResult => ({ resolved, merged: false, counts: {}, reason });

/**
 * Moves everything the anonymous account owns onto the target and destroys
 * it. Never throws for a refusal; a thrown step leaves the marker and the
 * `started` audit row behind, and the next request carrying the same cookie
 * resumes from there.
 */
export async function mergeAnonymous(anonId: string, targetId: string, deps: MergeDeps): Promise<MergeResult> {
  // Not an attempt at anything: recording it would only pollute the trail.
  if (anonId === targetId) return refusal('same_user', true);

  const marker = await deps.directory.marker(anonId);
  if (marker) {
    if (marker.target_id !== targetId) return refusal(MERGE_IN_PROGRESS, false);
    return await finish(marker.audit_id, anonId, targetId, deps);
  }

  const state = await deps.cells.cell(anonId).precheck();
  const target = await deps.directory.lookup(targetId);
  // A tombstoned cell reports itself gone, which is `anon_missing`: the rows
  // are already somewhere else and the cookie is safe to drop.
  const refused = precheck(state.exists ? { is_anonymous: state.isAnonymous } : null, target ? { id: target.id } : null, false);
  if (refused) return refused;

  const { auditId } = await deps.directory.beginMerge(anonId, targetId, isoUtc(deps.clock.now()));
  return await finish(auditId, anonId, targetId, deps);
}

/** Steps three to five, from wherever the last attempt stopped. */
async function finish(auditId: number, anonId: string, targetId: string, deps: MergeDeps): Promise<MergeResult> {
  const audit = await deps.directory.audit(auditId);
  let counts = audit?.status === 'completed' ? (audit.counts ?? {}) : null;
  if (counts === null) {
    counts = await moveRows(anonId, targetId, deps);
    await deps.directory.completeMerge(auditId, counts, isoUtc(deps.clock.now()));
  }
  await destroyAccount(anonId, MERGED, deps);
  // Last: the marker is the flag that says the saga is unfinished.
  await deps.directory.clearMarker(anonId);
  return { resolved: true, merged: true, counts, reason: null };
}

/** Step three. Idempotent: the import ignores rows the target already holds
 * by primary key, and the ledger reassign is a no-op the second time. */
async function moveRows(anonId: string, targetId: string, deps: MergeDeps): Promise<Counts> {
  const anon = deps.cells.cell(anonId);
  const target = deps.cells.cell(targetId);
  const state = await anon.precheck();
  if (!state.exists) return {};

  const anonSnapshot = await anon.dump();
  const targetSnapshot = await target.dump();
  const before = snapshotOf(anonSnapshot, targetSnapshot, anonId, targetId);
  const { after, counts } = mergeRows(before, anonId, targetId, deps.randomHex);

  await target.importRows(payloadOf(anonSnapshot, after, before, anonId, targetId));
  const moved = await deps.limiter.reassign(anonId, targetId);
  if (moved) counts[LIMITER_TABLE] = moved;

  // The target's own cell is the authority on the columns it had not set.
  for (const key of Object.keys(counts)) if (key.startsWith('users.')) delete counts[key];
  Object.assign(counts, await target.carryPreferences(carriedOf(anonSnapshot)));
  return counts;
}

/** The two accounts as the domain's policy reads them: one owner column per
 * rule, synthesised, since a cell holds one user and stores neither. */
function snapshotOf(anon: CellSnapshot, target: CellSnapshot, anonId: string, targetId: string): Snapshot {
  const tables: Snapshot['tables'] = {};
  for (const rule of POLICY) {
    if (rule.table === LIMITER_TABLE) continue;
    const anonRows = (anon.tables[rule.table] ?? []).map((r) => ({ ...r, [rule.column]: anonId }));
    const moved = new Set(anonRows.map((r) => rowKey(rule.table, r)).filter((k): k is string => k !== null));
    const targetRows = (target.tables[rule.table] ?? [])
      .filter((r) => !moved.has(rowKey(rule.table, r) ?? ''))
      .map((r) => ({ ...r, [rule.column]: targetId }));
    tables[rule.table] = { [rule.column]: { [anonId]: anonRows, [targetId]: targetRows } };
  }
  return { users: { [anonId]: anon.profile ?? null, [targetId]: target.profile ?? null }, tables };
}

/**
 * What the target has to insert: every table the anonymous cell holds, less
 * the rows a rule deleted or dropped. Derived tables (cards, reviews, the
 * session answers, the trivia queue) carry no owner column and so no rule:
 * in one database ownership followed their parent's foreign key, and across
 * cells the parent's move is exactly what has to bring them along.
 */
function payloadOf(anon: CellSnapshot, after: Snapshot, before: Snapshot, anonId: string, targetId: string): CellSnapshot {
  const ruleFor = new Map(POLICY.map((r) => [r.table, r]));
  const tables: Record<string, Row[]> = {};
  for (const [table, rows] of Object.entries(anon.tables)) {
    const rule = ruleFor.get(table);
    if (!rule) {
      tables[table] = [...rows];
      continue;
    }
    if (rule.rule === DELETE) continue;
    const kept = new Set((before.tables[table]?.[rule.column]?.[targetId] ?? []).map(canonical));
    tables[table] = (after.tables[table]?.[rule.column]?.[targetId] ?? []).filter((r) => !kept.has(canonical(r)));
  }
  return { profile: null, tables };
}

const canonical = (row: Row): string => JSON.stringify(row, Object.keys(row).sort());

function carriedOf(anon: CellSnapshot): CarriedPreferences {
  const profile = (anon.profile ?? {}) as Row;
  const [retention, mode] = CARRIED_USER_COLUMNS;
  return {
    desired_retention: (profile[retention] as number | null) ?? null,
    editor_input_mode: (profile[mode] as string | null) ?? null,
  };
}

/**
 * The three-step deletion, shared by the merge, the reaper and account
 * deletion. `deleteAll` leaves the freed pages readable in the next
 * snapshot, so a zero-fill scrub follows in its own RPC: combining it with
 * the wipe fails the output gate with `DurabilityUnproven` and rolls the
 * whole RPC back. Every step is idempotent, and the retry that a transient
 * RPC failure needs is the cells adapter's, applied at the composition root.
 */
export async function destroyAccount(id: string, reason: TombstoneReason, deps: DestroyDeps): Promise<void> {
  const at = isoUtc(deps.clock.now());
  const cell = deps.cells.cell(id);
  await cell.destroy(reason, at);
  await cell.scrub(at);
  // The directory's tombstone outlives the cell's: it answers for an id whose
  // cell has since been reclaimed.
  await deps.directory.tombstone(id, reason, at);
  await deps.directory.remove(id);
}
