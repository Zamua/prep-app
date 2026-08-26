// The ledger's rows as the domain reads them. The adapter parses SQL rows
// into these; every scheduling decision is taken over them and nothing else,
// which is what makes a re-activation reach the same decision as the
// activation that died.

export type JobState = 'running' | 'gated' | 'terminal';
export type StepStatus = 'pending' | 'done' | 'skipped' | 'failed';

export interface JobRow {
  id: string;
  kind: string;
  owner: string;
  input: Readonly<Record<string, unknown>>;
  state: JobState;
  /** The index into the graph's active nodes the next activation resumes at. */
  cursor: number;
  created_at: string;
  deadline_at: string | null;
  deadline_kind: string | null;
  terminal_at: string | null;
  /** Which terminal the job is heading for, written before the transition
   * that announces it: terminal is a written state, never an inference. */
  terminal_status: string | null;
  error: string | null;
  /** The number of the last transition written to the outbox. */
  transition: number;
}

export interface StepRow {
  step_key: string;
  name: string;
  /** The node's position among the graph's active nodes. */
  idx: number;
  /** The fanout ordinal within the node; 0 for a node that does not fan out. */
  item: number;
  status: StepStatus;
  attempt: number;
  refusals: number;
  next_attempt_at: string | null;
  output: unknown;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface EventRow {
  seq: number;
  name: string;
  payload: unknown;
  at: string;
  consumed_at: string | null;
}

export interface OutboxRow {
  transition: number;
  status: string;
  payload: Readonly<Record<string, unknown>>;
  at: string;
  delivered_at: string | null;
  attempt: number;
  next_attempt_at: string | null;
}

/** Everything one activation reads before it decides anything. */
export interface LedgerRows {
  job: JobRow;
  steps: StepRow[];
  events: EventRow[];
  outbox: OutboxRow[];
}

/** A step row about to exist: the unit of work, keyed by its idempotency key. */
export interface NewStep {
  step_key: string;
  name: string;
  idx: number;
  item: number;
}

export type StepWrite = Pick<StepRow, 'step_key' | 'status' | 'attempt' | 'refusals' | 'next_attempt_at' | 'output' | 'error' | 'started_at' | 'finished_at'>;

export type JobWrite = Partial<Pick<JobRow, 'state' | 'cursor' | 'deadline_at' | 'deadline_kind' | 'terminal_at' | 'terminal_status' | 'error' | 'transition'>>;

/**
 * One activation's whole result, applied in a single transaction. Splitting
 * it would let a crash land between the step row and the cursor, which is the
 * one state the ledger has no way to read back.
 */
export interface LedgerCommit {
  step?: StepWrite;
  /** Rows for the node the cursor moves to; written with the move. */
  materialize?: readonly NewStep[];
  job?: JobWrite;
  consumeEvents?: ConsumeEvents;
  outbox?: Omit<OutboxRow, 'delivered_at' | 'attempt' | 'next_attempt_at'>;
}

/**
 * Which unconsumed events a commit stamps. A resolved gate stamps everything
 * through the event it acted on plus every later repeat of that name, so a
 * double-clicked signal cannot resolve a second gate while a genuinely
 * different later signal still can. `throughSeq: null` stamps them all, which
 * is what a terminal job does: nothing can resolve a gate any more.
 */
export interface ConsumeEvents {
  at: string;
  throughSeq: number | null;
  /** The resolved event's name; later repeats of it are stamped too. */
  name?: string;
}

/** What a step handler returns. */
export interface StepOutput {
  /** What this step produced. Stored in the step row and offered to later steps. */
  value?: unknown;
  /** The items a later node's `fanout.from` this node expands over. */
  items?: readonly unknown[];
  /** Progress keys merged into the job's payload, in ledger order. */
  progress?: Record<string, unknown>;
}

export const isFinished = (row: StepRow): boolean => row.status !== 'pending';

/** Every step's progress contribution, applied in ledger order over the base.
 * A handler expresses a progress key by returning it; the runner never knows
 * what the keys mean. */
export function mergeProgress(base: Readonly<Record<string, unknown>>, steps: readonly StepRow[]): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base };
  const ordered = [...steps].sort((a, b) => a.idx - b.idx || a.item - b.item);
  for (const step of ordered) {
    const output = step.output;
    if (output === null || typeof output !== 'object') continue;
    const progress = (output as { progress?: unknown }).progress;
    if (progress === null || typeof progress !== 'object' || Array.isArray(progress)) continue;
    Object.assign(out, progress);
  }
  return out;
}
