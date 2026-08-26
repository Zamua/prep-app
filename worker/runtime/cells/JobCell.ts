// One job's cell: the step ledger, and the loop that drives it. Every
// decision is taken from the rows, so an eviction, a node restart and a
// duplicate alarm all reach the same one.
//
// Two rules the shape rests on. A caller-originated RPC (`start`, `signal`,
// `terminate`) never calls back into the owner's cell: the owner is mid
// request when it calls here, and a cell serves one request at a time.
// Everything that touches the owner therefore happens on the alarm. And the
// alarm is derived from the rows at the end of every RPC and in the
// constructor, never held, so a rolled-back RPC still converges.
import { DurableObject } from 'cloudflare:workers';
import type { LlmStepContext, StepInfo } from '../../app/jobs/registry.js';
import type { JobCellRpc, JobLedger, JobStatusWrite, JobTransition } from '../../app/ports.js';
import { activeNodes, nodeAt, nodeOnError, type StepGraph, type StepNode } from '../../domain/jobs/graph.js';
import {
  mergeProgress,
  type JobWrite,
  type LedgerCommit,
  type LedgerRows,
  type NewStep,
  type OutboxRow,
  type StepOutput,
  type StepRow,
  type StepWrite,
} from '../../domain/jobs/ledger.js';
import { isAbandoned, isRefusal, MAX_DELIVERY_ATTEMPTS, MAX_REFUSALS, refusalBackoffMs } from '../../domain/jobs/refusal.js';
import { backoffMs, deriveAlarm, nextAction, stepKey, type Action, type GateSignal, type LedgerState } from '../../domain/jobs/schedule.js';
import { isoUtc } from '../../domain/py.js';
import { compose, type Composition } from '../compose.js';
import type { Env } from '../env.js';
import type { CellStorage } from '../storage.js';

/** A wake is never asked for the past: a due-now alarm lands just after now. */
const ALARM_FLOOR_MS = 1;
/** Enough iterations to run one step and settle the bookkeeping around it. */
const MAX_DRIVE_STEPS = 16;

export class UnknownJobKind extends Error {}

export class JobCell extends DurableObject<Env> implements JobCellRpc {
  private readonly c: Composition;
  private readonly storage: CellStorage;
  private readonly ledger: JobLedger;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.c = compose(env);
    this.storage = ctx.storage as unknown as CellStorage;
    this.ledger = this.c.jobLedger(this.storage);
    void ctx.blockConcurrencyWhile(async () => {
      this.c.migrateJobCell(this.storage);
      await this.ensureAlarm();
    });
  }

  // ---- RPC ------------------------------------------------------------------

  /** Writes the job and its first transition, arms the alarm, and returns.
   * The work is the alarm's, so a start costs the route one small write. */
  async start(job: {
    id: string;
    kind: string;
    owner: string;
    input: Record<string, unknown>;
    urlPath: string;
    workflowType: string;
    deckId: number | null;
    deckName: string | null;
    at: string;
  }): Promise<JobTransition> {
    const fresh = this.ledger.create({
      id: job.id,
      kind: job.kind,
      owner: job.owner,
      input: job.input,
      createdAt: job.at,
      urlPath: job.urlPath,
      workflowType: job.workflowType,
      deckId: job.deckId,
      deckName: job.deckName,
    });
    if (fresh) this.ledger.commit(this.enterCommit(this.stateOf(this.ledger.read()!), 0, this.now()));
    await this.ensureAlarm();
    return (await this.peek()) ?? EMPTY;
  }

  /**
   * Records the event and, when the cursor is on a gate waiting for it,
   * resolves it here: the transient status has to be written before this RPC
   * returns, or the route re-renders the buttons the user just pressed.
   */
  async signal(event: { name: string; payload?: unknown; at: string }): Promise<JobTransition | null> {
    if (this.ledger.read() === null) return null;
    this.ledger.appendEvent({ name: event.name, payload: event.payload ?? null, at: event.at });
    const state = this.stateOf(this.ledger.read()!);
    const action = nextAction(state, this.now());
    if (action.kind === 'run' && action.event?.name === event.name) this.ledger.commit(this.resolveGate(state, action.event, false, this.now()));
    await this.ensureAlarm();
    return this.peek();
  }

  async terminate(reason: string, at: string): Promise<void> {
    const rows = this.ledger.read();
    if (!rows || rows.job.state === 'terminal') return;
    const state = this.stateOf(rows);
    const now = new Date(at);
    const t = this.transition(state, 'failed', now, state.steps, { error: reason, finished_at: isoUtc(now) });
    this.ledger.commit({
      job: {
        ...t.job,
        state: 'terminal',
        terminal_at: isoUtc(now),
        terminal_status: 'failed',
        error: reason,
        cursor: activeNodes(state.graph, state.job.input).length,
      },
      outbox: t.outbox,
    });
    await this.ensureAlarm();
  }

  async peek(): Promise<JobTransition | null> {
    const rows = this.ledger.read();
    if (!rows) return null;
    const last = rows.outbox.at(-1);
    return last ? { status: last.status, progress: { ...last.payload }, transition: last.transition } : EMPTY;
  }

  /** The internal drive kick. The public router never reaches a JobCell. */
  override async fetch(_request: Request): Promise<Response> {
    return Response.json(await this.drive());
  }

  async alarm(): Promise<void> {
    await this.drive();
  }

  // ---- the loop ---------------------------------------------------------------

  private async drive(): Promise<JobTransition | null> {
    let ran = 0;
    for (let i = 0; i < MAX_DRIVE_STEPS; i++) {
      const rows = this.ledger.read();
      if (!rows) return null;
      await this.flushOutbox(rows);
      const state = this.stateOf(rows);
      const action = nextAction(state, this.now());
      if (action.kind === 'run') {
        if (ran >= 1) break;
        ran++;
        await this.runStep(state, action);
        continue;
      }
      if (action.kind === 'advance') {
        this.ledger.commit(this.enterCommit(state, action.to, this.now()));
        continue;
      }
      if (action.kind === 'finish') {
        const commit = this.finishCommit(state);
        if (!commit) break;
        this.ledger.commit(commit);
        continue;
      }
      break;
    }
    const after = this.ledger.read();
    if (after) await this.flushOutbox(after);
    await this.ensureAlarm();
    return this.peek();
  }

  /** Undelivered transitions, oldest first. A refused delivery is deferred on
   * the refusal backoff and the alarm brings it back, up to
   * `MAX_DELIVERY_ATTEMPTS`; past that the row is abandoned so an owner that
   * refuses permanently cannot keep this cell awake for the deploy's life. */
  private async flushOutbox(rows: LedgerRows): Promise<void> {
    const route = this.ledger.route();
    for (const row of rows.outbox) {
      if (row.delivered_at !== null || isAbandoned(row)) continue;
      const now = this.now();
      if (row.next_attempt_at !== null && new Date(row.next_attempt_at).getTime() > now.getTime()) continue;
      const write: JobStatusWrite = {
        jobId: rows.job.id,
        transition: row.transition,
        status: row.status,
        progress: { ...row.payload },
        urlPath: route.urlPath,
        kind: route.workflowType,
        deckId: route.deckId,
        deckName: route.deckName,
      };
      try {
        await this.c.userCellsDirect.cell(rows.job.owner).jobStatus(write);
        this.ledger.markDelivered(row.transition, isoUtc(this.now()));
      } catch (e) {
        const attempt = row.attempt + 1;
        this.ledger.deferDelivery(row.transition, attempt, isoUtc(new Date(this.now().getTime() + refusalBackoffMs(attempt - 1))));
        if (attempt >= MAX_DELIVERY_ATTEMPTS) {
          console.error(`job ${rows.job.id}: giving up on status write ${row.transition} to ${rows.job.owner} after ${attempt} attempts: ${message(e)}`);
        } else if (!isRefusal(e)) {
          console.warn(`job ${rows.job.id}: status write ${row.transition} failed: ${message(e)}`);
        }
        return;
      }
    }
  }

  // ---- running one step -------------------------------------------------------

  private async runStep(state: LedgerState, action: Extract<Action, { kind: 'run' }>): Promise<void> {
    const row = state.steps.find((r) => r.step_key === action.stepKey)!;
    const node = nodeAt(state.graph, state.job.input, row.idx)!;
    if (node.kind === 'gate') {
      this.ledger.commit(this.resolveGate(state, action.event!, action.byDeadline, this.now()));
      return;
    }
    const started = isoUtc(this.now());
    let output: StepOutput;
    try {
      output = await this.execute(state, node, row);
    } catch (e) {
      this.ledger.commit(this.failureCommit(state, node, row, started, e));
      return;
    }
    const done: StepWrite = {
      step_key: row.step_key,
      status: 'done',
      attempt: row.attempt + 1,
      refusals: row.refusals,
      next_attempt_at: null,
      output,
      error: null,
      started_at: row.started_at ?? started,
      finished_at: isoUtc(this.now()),
    };
    this.ledger.commit(this.afterStep(state, row, done));
  }

  private async execute(state: LedgerState, node: StepNode, row: StepRow): Promise<StepOutput> {
    const info: StepInfo = {
      jobId: state.job.id,
      kind: state.job.kind,
      owner: state.job.owner,
      stepKey: row.step_key,
      name: node.name,
      idx: row.idx,
      item: row.item,
      input: state.job.input,
      outputs: this.outputsOf(state),
      itemInput: node.fanout ? (this.itemsFor(state, node)[row.item] ?? null) : null,
      clock: this.c.clock,
    };
    if (node.kind === 'write') {
      return this.c.userCellsDirect.cell(state.job.owner).applyJobStep({
        jobId: info.jobId,
        jobKind: info.kind,
        name: info.name,
        stepKey: info.stepKey,
        idx: info.idx,
        item: info.item,
        input: { ...info.input },
        outputs: { ...info.outputs },
        itemInput: info.itemInput,
        at: isoUtc(this.now()),
      });
    }
    // The owner's credential is read here, once for this step: a key revoked
    // between two steps stops the second one.
    const agent = this.c.agentFor(() => this.c.userCellsDirect.cell(state.job.owner).agentConfig(), { timeoutMs: this.c.jobLlmTimeoutMs });
    const ctx: LlmStepContext = { ...info, site: 'job', agent, signal: AbortSignal.timeout(this.c.jobLlmTimeoutMs) };
    return this.c.stepRegistry.get(node.name)(ctx);
  }

  /**
   * A refusal costs a refusal, not an attempt: nothing was written, so the
   * step re-reads and re-decides. A real failure costs an attempt, and once
   * they are spent the node's `onError` decides between skipping the row and
   * failing the job.
   */
  private failureCommit(state: LedgerState, node: StepNode, row: StepRow, started: string, e: unknown): LedgerCommit {
    const now = this.now();
    const base: StepWrite = {
      step_key: row.step_key,
      status: 'pending',
      attempt: row.attempt,
      refusals: row.refusals,
      next_attempt_at: null,
      output: row.output,
      error: message(e),
      started_at: row.started_at ?? started,
      finished_at: null,
    };
    if (isRefusal(e) && row.refusals + 1 < MAX_REFUSALS) {
      return { step: { ...base, refusals: row.refusals + 1, next_attempt_at: isoUtc(new Date(now.getTime() + refusalBackoffMs(row.refusals))) } };
    }
    const attempt = row.attempt + 1;
    if (attempt < node.retry.attempts) {
      return { step: { ...base, attempt, next_attempt_at: isoUtc(new Date(now.getTime() + backoffMs(node.retry, attempt))) } };
    }
    const spent: StepWrite = { ...base, attempt, status: nodeOnError(node) === 'skip' ? 'skipped' : 'failed', finished_at: isoUtc(now) };
    if (nodeOnError(node) === 'skip') return this.afterStep(state, row, spent);
    const gate = this.rerunGate(state, row);
    if (gate) return this.reGate(state, gate, spent, message(e), now);
    return { step: spent, job: this.failJob(state, message(e)) };
  }

  /** The commit that follows a written step row: the node's other rows may
   * still be pending, in which case only the row moves. */
  private afterStep(state: LedgerState, row: StepRow, write: StepWrite): LedgerCommit {
    const steps = state.steps.map((r) => (r.step_key === row.step_key ? { ...r, ...write } : r));
    const siblings = steps.filter((r) => r.idx === row.idx);
    if (siblings.some((r) => r.status === 'pending')) return { step: write };
    const node = nodeAt(state.graph, state.job.input, row.idx)!;
    if (node.emptyError && siblings.every((r) => r.status !== 'done')) return { step: write, job: this.failJob(state, node.emptyError) };
    // A gate's transient must be visible on its own, so a gate never advances
    // in the same commit; every other node does, which halves the alarm hops.
    return { step: write, ...this.enterCommit({ ...state, steps }, row.idx + 1, this.now(), steps) };
  }

  // ---- moving the cursor ---------------------------------------------------------

  /**
   * Enters the node at `cursor`: its rows, the status transition that names
   * it, and, for a gate, the deadline. The deadline is written once, so a
   * re-run returns to the gate it already had.
   */
  private enterCommit(state: LedgerState, cursor: number, now: Date, steps: readonly StepRow[] = state.steps): LedgerCommit {
    const nodes = activeNodes(state.graph, state.job.input);
    for (let at = cursor; ; at++) {
      if (at >= nodes.length) return { job: { cursor: at, terminal_status: state.job.terminal_status ?? state.graph.doneStatus } };
      const node = nodes[at]!;
      const items = node.fanout ? this.itemsFor({ ...state, steps }, node) : null;
      if (items !== null && items.length === 0) {
        if (node.emptyError) return { job: this.failJob(state, node.emptyError) };
        continue;
      }
      const count = items?.length ?? 1;
      const base = nextItem(steps, node.name);
      const materialize: NewStep[] = [];
      for (let i = 0; i < count; i++) materialize.push({ step_key: stepKey(state.job.id, node, base + i), name: node.name, idx: at, item: base + i });
      const job: JobWrite = { cursor: at, state: node.kind === 'gate' ? 'gated' : 'running' };
      if (node.kind === 'gate' && state.job.deadline_at === null) {
        job.deadline_at = isoUtc(new Date(now.getTime() + node.gate!.deadlineMs));
        job.deadline_kind = node.gate!.onDeadline;
      }
      const t = this.transition(state, node.status, now, steps);
      return { materialize, job: { ...job, ...t.job }, outbox: t.outbox };
    }
  }

  /** The last transition: the terminal the ledger already decided on. */
  private finishCommit(state: LedgerState): LedgerCommit | null {
    if (state.job.state === 'terminal') return null;
    const now = this.now();
    const status = state.job.terminal_status ?? state.graph.doneStatus;
    const extra: Record<string, unknown> = { finished_at: isoUtc(now) };
    if (state.job.error) extra['error'] = state.job.error;
    const t = this.transition(state, status, now, state.steps, extra);
    // Nothing can resolve a gate any more, so a signal that arrived late is
    // stamped here rather than left looking pending forever.
    return { job: { ...t.job, state: 'terminal', terminal_at: isoUtc(now), terminal_status: status }, outbox: t.outbox, consumeEvents: { at: isoUtc(now), throughSeq: null } };
  }

  // ---- gates -------------------------------------------------------------------

  private resolveGate(state: LedgerState, signal: GateSignal, byDeadline: boolean, now: Date): LedgerCommit {
    const nodes = activeNodes(state.graph, state.job.input);
    const node = nodes[state.job.cursor]!;
    const event = signal.name;
    const outcome = node.gate!.onEvent[event]!;
    const row = state.steps.find((r) => r.idx === state.job.cursor && r.status === 'pending')!;
    const write: StepWrite = {
      step_key: row.step_key,
      status: 'done',
      attempt: row.attempt + 1,
      refusals: row.refusals,
      next_attempt_at: null,
      // The payload travels with the outcome: a re-run reads its feedback text
      // out of the step row, which is the only place a cold cell can find it.
      output: { value: { event, byDeadline, payload: signal.payload } },
      error: null,
      started_at: row.started_at,
      finished_at: isoUtc(now),
    };
    const steps = state.steps.map((r) => (r.step_key === row.step_key ? { ...r, ...write } : r));
    const t = this.transition(state, outcome.transient, now, steps);
    const commit: LedgerCommit = {
      step: write,
      consumeEvents: { at: isoUtc(now), throughSeq: signal.seq, name: event },
      job: { ...t.job, state: 'running' },
      outbox: t.outbox,
    };
    if (outcome.go === 'reject') {
      commit.job = { ...commit.job, cursor: nodes.length, terminal_status: 'rejected' };
      return commit;
    }
    if (typeof outcome.go === 'object') {
      // A re-run goes back to an earlier node with a fresh row. The deadline
      // is untouched: one timer per gate, whatever the round.
      const target = nodes.findIndex((n) => n.name === (outcome.go as { rerun: string }).rerun);
      const back = nodes[target]!;
      const item = nextItem(steps, back.name);
      commit.materialize = [{ step_key: stepKey(state.job.id, back, item), name: back.name, idx: target, item }];
      commit.job = { ...commit.job, cursor: target };
    }
    return commit;
  }

  /** The gate a failed re-run answers to: a non-fanout node re-entered at a
   * later round, with a gate after it. */
  private rerunGate(state: LedgerState, row: StepRow): { node: StepNode; idx: number } | null {
    if (row.item === 0) return null;
    const nodes = activeNodes(state.graph, state.job.input);
    if (nodes[row.idx]?.fanout) return null;
    const idx = nodes.findIndex((n, i) => i > row.idx && n.kind === 'gate');
    return idx === -1 ? null : { node: nodes[idx]!, idx };
  }

  /** A failed re-plan keeps the prior plan and hands the gate back with the
   * error under it, rather than failing the job. */
  private reGate(state: LedgerState, gate: { node: StepNode; idx: number }, write: StepWrite, error: string, now: Date): LedgerCommit {
    const steps = state.steps.map((r) => (r.step_key === write.step_key ? { ...r, ...write } : r));
    const item = nextItem(steps, gate.node.name);
    const t = this.transition(state, gate.node.status, now, steps, { error: `${gate.node.gate!.rerunError ?? ''}${error}` });
    return {
      step: write,
      materialize: [{ step_key: stepKey(state.job.id, gate.node, item), name: gate.node.name, idx: gate.idx, item }],
      job: { ...t.job, cursor: gate.idx, state: 'gated' },
      outbox: t.outbox,
    };
  }

  // ---- pieces ---------------------------------------------------------------------

  private failJob(state: LedgerState, error: string): JobWrite {
    return { cursor: activeNodes(state.graph, state.job.input).length, terminal_status: 'failed', error };
  }

  private transition(
    state: LedgerState,
    status: string,
    now: Date,
    steps: readonly StepRow[],
    extra: Record<string, unknown> = {},
  ): { job: JobWrite; outbox: Omit<OutboxRow, 'delivered_at' | 'attempt' | 'next_attempt_at'> } {
    const base: Record<string, unknown> = { started_at: state.job.created_at, ...(state.graph.progressSeed?.(state.job.input) ?? {}) };
    const payload = mergeProgress(base, steps);
    Object.assign(payload, extra);
    payload['status'] = status;
    const transition = state.job.transition + 1;
    return { job: { transition }, outbox: { transition, status, payload, at: isoUtc(now) } };
  }

  /** Each node's finished value by name; a fanout node's is an array in item
   * order, which is the order a later node's rows are keyed by. */
  private outputsOf(state: LedgerState): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const node of activeNodes(state.graph, state.job.input)) {
      const rows = doneRows(state.steps, node.name);
      out[node.name] = node.fanout ? rows.map(valueOf) : (rows.at(-1) ? valueOf(rows.at(-1)!) : null);
    }
    return out;
  }

  /** What a fanout node expands over: the source's declared items, or, when
   * the source itself fanned out, its successful values. */
  private itemsFor(state: LedgerState, node: StepNode): unknown[] {
    const source = activeNodes(state.graph, state.job.input).find((n) => n.name === node.fanout!.from);
    if (!source) return [];
    const rows = doneRows(state.steps, source.name);
    if (source.fanout) return rows.map(valueOf);
    const items = rows.at(-1) ? (rows.at(-1)!.output as StepOutput | null)?.items : undefined;
    return items ? [...items] : [];
  }

  private stateOf(rows: LedgerRows): LedgerState {
    return { graph: this.graphOf(rows.job.kind), job: rows.job, steps: rows.steps, events: rows.events, outbox: rows.outbox };
  }

  private graphOf(kind: string): StepGraph {
    const graph = this.c.jobGraphs[kind];
    if (!graph) throw new UnknownJobKind(`no graph for job kind ${JSON.stringify(kind)}`);
    return graph;
  }

  private now(): Date {
    return this.c.clock.now();
  }

  /**
   * Derived from the rows and nothing else, at the end of every RPC and in
   * the constructor. A fired alarm with nothing due is a no-op.
   *
   * A wake is a wall-clock delay, so the derived instant is armed as the
   * distance it sits ahead of the job's own clock. The runtime does not
   * deliver one instant twice, and a job's derived wake is the same instant
   * at every step of it, so arming that instant directly would leave the
   * second step of any job waiting for a restart whenever the two clocks do
   * not move together.
   */
  private async ensureAlarm(): Promise<void> {
    const rows = this.ledger.read();
    const at = rows ? deriveAlarm(this.stateOf(rows)) : null;
    if (at === null) {
      if ((await this.storage.getAlarm()) !== null) await this.storage.deleteAlarm();
      return;
    }
    const delay = Math.max(new Date(at).getTime() - this.now().getTime(), ALARM_FLOOR_MS);
    await this.storage.setAlarm(this.c.wallClock.now().getTime() + delay);
  }
}

const EMPTY: JobTransition = { status: '', progress: {}, transition: 0 };

const doneRows = (steps: readonly StepRow[], name: string): StepRow[] => steps.filter((r) => r.name === name && r.status === 'done').sort((a, b) => a.item - b.item);

const nextItem = (steps: readonly StepRow[], name: string): number => Math.max(-1, ...steps.filter((r) => r.name === name).map((r) => r.item)) + 1;

const valueOf = (row: StepRow): unknown => (row.output as StepOutput | null)?.value ?? null;

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));
