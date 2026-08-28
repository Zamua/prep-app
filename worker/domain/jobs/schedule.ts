// What the ledger says to do next, decided from the rows alone. Pure, so a
// cold cell, an evicted cell and a duplicate alarm all reach the same answer.
import { isoUtc, parseIso } from '../time.js';
import { activeNodes, nodeAt, type Fanout, type StepGraph, type StepNode } from './graph.js';
import { isFinished, type EventRow, type JobRow, type OutboxRow, type StepRow } from './ledger.js';
import { isAbandoned } from './refusal.js';

export interface LedgerState {
  graph: StepGraph;
  job: JobRow;
  steps: readonly StepRow[];
  events: readonly EventRow[];
  outbox: readonly OutboxRow[];
}

/** The signal a gate resolves on. `seq` is null for the deadline, which has no
 * event row of its own; `payload` is the body a re-run is shown. */
export interface GateSignal {
  name: string;
  payload: unknown;
  seq: number | null;
}

export type Action =
  /** Run this step row. `event` is set when the row is a gate being resolved. */
  | { kind: 'run'; stepKey: string; event: GateSignal | null; byDeadline: boolean }
  /** Nothing is due; the alarm is already derived for `untilIso`. */
  | { kind: 'wait'; untilIso: string }
  /** Parked on a human gate until an event or the deadline. */
  | { kind: 'gate'; untilIso: string | null }
  /** The cursor's node is finished; the cursor has not moved yet. */
  | { kind: 'advance'; to: number }
  | { kind: 'finish' };

const ms = (iso: string): number => parseIso(iso).getTime();

const earliest = (isos: readonly string[]): string | null =>
  isos.length === 0 ? null : isos.reduce((a, b) => (ms(b) < ms(a) ? b : a));

/** Delay before attempt number `attempt + 1` of a step. */
export function backoffMs(policy: { initialMs: number; coefficient: number; capMs: number }, attempt: number): number {
  return Math.min(policy.capMs, Math.round(policy.initialMs * policy.coefficient ** Math.max(0, attempt - 1)));
}

/** The idempotency key of one step row. Stable across a retry, which is what
 * makes a write step safe to run again. */
export function stepKey(jobId: string, node: StepNode, item: number): string {
  return node.keyIsJobId ? jobId : `${jobId}-${node.name}-${item}`;
}

/** The rows of a node whose barrier lets them run: everything for `per-item`,
 * the lowest unfinished group for `batch`. */
export function runnableRows(rows: readonly StepRow[], fanout: Fanout | undefined): StepRow[] {
  const pending = rows.filter((r) => r.status === 'pending');
  if (fanout?.mode !== 'batch') return pending;
  const size = fanout.size ?? 1;
  const group = (r: StepRow): number => Math.floor(r.item / size);
  const open = Math.min(...pending.map(group));
  // A later group waits on every member of every earlier one, the Go barrier.
  if (rows.some((r) => !isFinished(r) && group(r) < open)) return [];
  return pending.filter((r) => group(r) === open);
}

function firstUnconsumed(events: readonly EventRow[], names: readonly string[]): EventRow | null {
  const matches = events.filter((e) => e.consumed_at === null && names.includes(e.name)).sort((a, b) => a.seq - b.seq);
  return matches[0] ?? null;
}

export function nextAction(state: LedgerState, now: Date): Action {
  const { graph, job, steps, events } = state;
  if (job.state === 'terminal') return { kind: 'finish' };
  const nodes = activeNodes(graph, job.input);
  if (job.cursor >= nodes.length) return { kind: 'finish' };
  const node = nodeAt(graph, job.input, job.cursor)!;
  const rows = steps.filter((r) => r.idx === job.cursor);

  if (node.kind === 'gate') {
    // The latest round: a re-run leaves the previous gate row behind as history.
    const row = rows.reduce<StepRow | undefined>((a, b) => (a === undefined || b.item > a.item ? b : a), undefined);
    if (row === undefined) return { kind: 'wait', untilIso: isoUtc(now) };
    if (isFinished(row)) return { kind: 'advance', to: job.cursor + 1 };
    const event = firstUnconsumed(events, node.gate!.events);
    if (event) return { kind: 'run', stepKey: row.step_key, event: { name: event.name, payload: event.payload, seq: event.seq }, byDeadline: false };
    if (job.deadline_at !== null && ms(job.deadline_at) <= now.getTime()) {
      return { kind: 'run', stepKey: row.step_key, event: { name: node.gate!.onDeadline, payload: null, seq: null }, byDeadline: true };
    }
    return { kind: 'gate', untilIso: job.deadline_at };
  }

  if (rows.length === 0) return { kind: 'wait', untilIso: isoUtc(now) };
  if (rows.every(isFinished)) return { kind: 'advance', to: job.cursor + 1 };

  const runnable = runnableRows(rows, node.fanout);
  const due = runnable.filter((r) => r.next_attempt_at === null || ms(r.next_attempt_at) <= now.getTime()).sort((a, b) => a.item - b.item);
  if (due.length) return { kind: 'run', stepKey: due[0]!.step_key, event: null, byDeadline: false };

  const waiting = runnable.map((r) => r.next_attempt_at).filter((v): v is string => v !== null);
  // Nothing runnable and nothing scheduled: an earlier barrier group still
  // holds a pending row that a later activation will pick up.
  if (waiting.length === 0) return { kind: 'wait', untilIso: isoUtc(now) };
  return { kind: 'wait', untilIso: earliest(waiting)! };
}

/**
 * When the cell must next wake, from the rows alone: the earliest pending
 * retry, the gate deadline, and the earliest undelivered status write that is
 * still worth offering. Null when nothing is outstanding, which is a job that
 * has finished and flushed, or one whose owner refused every delivery.
 */
export function deriveAlarm(state: LedgerState): string | null {
  const { graph, job, steps, outbox } = state;
  const candidates: string[] = [];
  if (job.state !== 'terminal') {
    for (const row of steps) {
      if (row.status !== 'pending') continue;
      // A gate's row is pending for as long as the gate is open; its wake is
      // the deadline, not a retry, or the cell would spin until someone clicks.
      if (nodeAt(graph, job.input, row.idx)?.kind === 'gate') continue;
      // A pending row with no scheduled time is due now.
      candidates.push(row.next_attempt_at ?? job.created_at);
    }
    if (job.state === 'gated' && job.deadline_at !== null) candidates.push(job.deadline_at);
    // A signal that landed while the cursor was elsewhere is acted on at the
    // gate it belongs to, not where it arrived. Without a wake for it the cell
    // would sleep to the deadline with the user's click unanswered.
    const gate = job.state === 'gated' ? nodeAt(graph, job.input, job.cursor)?.gate : undefined;
    if (gate && firstUnconsumed(state.events, gate.events)) candidates.push(job.created_at);
  }
  for (const row of outbox) {
    if (row.delivered_at !== null || isAbandoned(row)) continue;
    candidates.push(row.next_attempt_at ?? row.at);
  }
  return earliest(candidates);
}
