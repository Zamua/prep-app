// What the ledger says to do next, and when the cell must wake to do it.
import { describe, expect, it } from 'vitest';
import { JOB_GRAPHS } from '../../app/jobs/graph.js';
import type { StepGraph } from '../../domain/jobs/graph.js';
import type { EventRow, JobRow, OutboxRow, StepRow } from '../../domain/jobs/ledger.js';
import { backoffMs, deriveAlarm, nextAction, runnableRows, stepKey, type LedgerState } from '../../domain/jobs/schedule.js';
import { DurabilityUnproven, isRefusal, MAX_REFUSALS, refusalBackoffMs } from '../../domain/jobs/refusal.js';

const NOW = new Date('2026-03-14T15:00:00Z');
const iso = (ms: number): string => new Date(NOW.getTime() + ms).toISOString().replace('Z', '+00:00').replace('.000', '');

function job(over: Partial<JobRow> = {}): JobRow {
  return {
    id: 'plan-capitals-abcdef0123',
    kind: 'PlanGenerate',
    owner: 'u@example.test',
    input: { deckId: 1, deckName: 'capitals', prompt: 'p' },
    state: 'running',
    cursor: 0,
    created_at: iso(-1000),
    deadline_at: null,
    deadline_kind: null,
    terminal_at: null,
    terminal_status: null,
    error: null,
    transition: 1,
    ...over,
  };
}

function step(over: Partial<StepRow> & { step_key: string; name: string; idx: number }): StepRow {
  return {
    item: 0,
    status: 'pending',
    attempt: 0,
    refusals: 0,
    next_attempt_at: null,
    output: null,
    error: null,
    started_at: null,
    finished_at: null,
    ...over,
  };
}

const state = (over: Partial<LedgerState> & { steps: StepRow[] }): LedgerState => ({
  graph: JOB_GRAPHS.PlanGenerate,
  job: job(),
  events: [],
  outbox: [],
  ...over,
});

describe('nextAction', () => {
  it('runs the pending row at the cursor', () => {
    const s = state({ steps: [step({ step_key: 'k-plan-0', name: 'plan', idx: 0 })] });
    expect(nextAction(s, NOW)).toEqual({ kind: 'run', stepKey: 'k-plan-0', event: null, byDeadline: false });
  });

  it('waits until a backed-off row is due, then runs it', () => {
    const s = state({ steps: [step({ step_key: 'k-plan-0', name: 'plan', idx: 0, next_attempt_at: iso(2000) })] });
    expect(nextAction(s, NOW)).toEqual({ kind: 'wait', untilIso: iso(2000) });
    expect(nextAction(s, new Date(NOW.getTime() + 2000)).kind).toBe('run');
  });

  it('advances once every row of the cursor node has landed', () => {
    const s = state({ steps: [step({ step_key: 'k-plan-0', name: 'plan', idx: 0, status: 'done' })] });
    expect(nextAction(s, NOW)).toEqual({ kind: 'advance', to: 1 });
  });

  it('parks on a gate until an event or the deadline', () => {
    const gated = state({
      job: job({ cursor: 1, state: 'gated', deadline_at: iso(86_400_000) }),
      steps: [step({ step_key: 'k-plan-0', name: 'plan', idx: 0, status: 'done' }), step({ step_key: 'k-gate-0', name: 'gate', idx: 1 })],
    });
    expect(nextAction(gated, NOW)).toEqual({ kind: 'gate', untilIso: iso(86_400_000) });

    const event: EventRow = { seq: 1, name: 'accept', payload: null, at: iso(10), consumed_at: null };
    expect(nextAction({ ...gated, events: [event] }, NOW)).toEqual({ kind: 'run', stepKey: 'k-gate-0', event: 'accept', byDeadline: false });

    expect(nextAction(gated, new Date(NOW.getTime() + 86_400_001))).toEqual({ kind: 'run', stepKey: 'k-gate-0', event: 'reject', byDeadline: true });
  });

  it('ignores an event the gate does not wait for, and a consumed one', () => {
    const gated = state({
      job: job({ cursor: 1, state: 'gated', deadline_at: iso(86_400_000) }),
      steps: [step({ step_key: 'k-plan-0', name: 'plan', idx: 0, status: 'done' }), step({ step_key: 'k-gate-0', name: 'gate', idx: 1 })],
      events: [
        { seq: 1, name: 'apply', payload: null, at: iso(1), consumed_at: null },
        { seq: 2, name: 'accept', payload: null, at: iso(2), consumed_at: iso(3) },
      ],
    });
    expect(nextAction(gated, NOW).kind).toBe('gate');
  });

  it('finishes a terminal job and a cursor past the last node', () => {
    expect(nextAction(state({ job: job({ state: 'terminal' }), steps: [] }), NOW)).toEqual({ kind: 'finish' });
    expect(nextAction(state({ job: job({ cursor: 4 }), steps: [] }), NOW)).toEqual({ kind: 'finish' });
  });

  it('skips a node the input excludes: card scope has no transform gate', () => {
    const card = {
      graph: JOB_GRAPHS.Transform,
      job: job({ kind: 'Transform', input: { scope: 'card', targetId: 7, prompt: 'p' }, cursor: 1 }),
      steps: [step({ step_key: 'k-compute-0', name: 'compute', idx: 0, status: 'done' }), step({ step_key: 'k-apply-0', name: 'apply', idx: 1 })],
      events: [],
      outbox: [],
    };
    expect(nextAction(card, NOW)).toEqual({ kind: 'run', stepKey: 'k-apply-0', event: null, byDeadline: false });
  });
});

describe('the batch barrier', () => {
  const fanout = { mode: 'batch', size: 4, from: 'plan' } as const;
  const rows = (statuses: string[]): StepRow[] =>
    statuses.map((s, i) => step({ step_key: `k-expand-${i}`, name: 'expand', idx: 2, item: i, status: s as StepRow['status'] }));

  it('opens only the lowest unfinished group', () => {
    const runnable = runnableRows(rows(['pending', 'pending', 'pending', 'pending', 'pending', 'pending']), fanout);
    expect(runnable.map((r) => r.item)).toEqual([0, 1, 2, 3]);
  });

  it('holds group two until every member of group one has landed', () => {
    const half = rows(['done', 'skipped', 'done', 'pending', 'pending']);
    expect(runnableRows(half, fanout).map((r) => r.item)).toEqual([3]);
    const landed = rows(['done', 'skipped', 'done', 'done', 'pending']);
    expect(runnableRows(landed, fanout).map((r) => r.item)).toEqual([4]);
  });

  it('opens every row of a per-item node at once', () => {
    expect(runnableRows(rows(['pending', 'pending', 'pending', 'pending', 'pending']), { mode: 'per-item', from: 'expand' }).length).toBe(5);
  });
});

describe('deriveAlarm', () => {
  it('takes the earliest of a retry, the gate deadline and an undelivered write', () => {
    const outbox: OutboxRow = { transition: 1, status: 'planning', payload: {}, at: iso(0), delivered_at: null, attempt: 0, next_attempt_at: iso(500) };
    const s = state({
      job: job({ cursor: 2 }),
      steps: [step({ step_key: 'k-expand-0', name: 'expand', idx: 2, next_attempt_at: iso(9_000) })],
      outbox: [outbox],
    });
    expect(deriveAlarm(s)).toBe(iso(500));
    expect(deriveAlarm({ ...s, outbox: [{ ...outbox, delivered_at: iso(1) }] })).toBe(iso(9_000));
  });

  it('wakes a gated job on its deadline, not on its open gate row', () => {
    const s = state({
      job: job({ cursor: 1, state: 'gated', deadline_at: iso(86_400_000) }),
      steps: [step({ step_key: 'k-plan-0', name: 'plan', idx: 0, status: 'done' }), step({ step_key: 'k-gate-0', name: 'gate', idx: 1 })],
    });
    // A gate's row stays pending until someone clicks; counting it as a retry
    // would spin the cell against a deadline hours away.
    expect(deriveAlarm(s)).toBe(iso(86_400_000));
  });

  it('treats a pending row with no scheduled time as due now', () => {
    const s = state({ steps: [step({ step_key: 'k-plan-0', name: 'plan', idx: 0 })] });
    expect(deriveAlarm(s)).toBe(s.job.created_at);
  });

  it('is null for a terminal job whose writes all landed: nothing left to wake for', () => {
    const s = state({
      job: job({ state: 'terminal', cursor: 4 }),
      steps: [step({ step_key: 'k-plan-0', name: 'plan', idx: 0, status: 'done' })],
      outbox: [{ transition: 1, status: 'done', payload: {}, at: iso(0), delivered_at: iso(1), attempt: 0, next_attempt_at: null }],
    });
    expect(deriveAlarm(s)).toBeNull();
  });

  it('still wakes a terminal job that owes a status write', () => {
    const s = state({
      job: job({ state: 'terminal', cursor: 4 }),
      steps: [],
      outbox: [{ transition: 9, status: 'done', payload: {}, at: iso(0), delivered_at: null, attempt: 1, next_attempt_at: iso(250) }],
    });
    expect(deriveAlarm(s)).toBe(iso(250));
  });
});

describe('backoff', () => {
  it('is the Go policy: initial times coefficient per attempt, capped', () => {
    const llm = { initialMs: 2000, coefficient: 2, capMs: 30_000 };
    expect([1, 2, 3, 4, 5, 6].map((a) => backoffMs(llm, a))).toEqual([2000, 4000, 8000, 16_000, 30_000, 30_000]);
    const trivia = { initialMs: 500, coefficient: 2, capMs: 30_000 };
    expect([1, 2, 3].map((a) => backoffMs(trivia, a))).toEqual([500, 1000, 2000]);
  });

  it('covers the 6-8s restart window with twelve refusals, 250ms doubling to 8s', () => {
    const delays = Array.from({ length: MAX_REFUSALS }, (_, i) => refusalBackoffMs(i));
    expect(delays.slice(0, 6)).toEqual([250, 500, 1000, 2000, 4000, 8000]);
    expect(Math.max(...delays)).toBe(8000);
    expect(delays.reduce((a, b) => a + b, 0)).toBeGreaterThan(8000);
  });
});

describe('step keys', () => {
  it('keeps the Go worker spelling where one exists', () => {
    const plan = JOB_GRAPHS.PlanGenerate;
    const expand = plan.nodes.find((n) => n.name === 'expand')!;
    const insert = plan.nodes.find((n) => n.name === 'insert')!;
    expect(stepKey('plan-x-1', expand, 3)).toBe('plan-x-1-expand-3');
    expect(stepKey('plan-x-1', insert, 0)).toBe('plan-x-1-insert-0');
  });

  it('keys the grading record on the job id itself', () => {
    const record = JOB_GRAPHS.GradeAnswer.nodes.find((n) => n.name === 'record')!;
    expect(stepKey('grade-capitals-q9-abc', record, 0)).toBe('grade-capitals-q9-abc');
  });
});

describe('every declared graph', () => {
  it('names a status literal for every node', () => {
    for (const graph of Object.values(JOB_GRAPHS) as StepGraph[]) {
      for (const node of graph.nodes) expect(node.status, `${graph.kind}.${node.name}`).toBeTruthy();
    }
  });
});

describe('what counts as a refusal', () => {
  const named = (name: string, message = 'x'): Error => Object.assign(new Error(message), { name });

  it('is what celld says when it declines the work, not when the work fails', () => {
    expect(isRefusal(new DurabilityUnproven('celld output gate: durability unproven: too large'))).toBe(true);
    expect(isRefusal(named('DurableObjectRoutingError', 'The Durable Object owner is currently unreachable'))).toBe(true);
    expect(isRefusal(new Error('remote RPC owner was stale'))).toBe(true);
    expect(isRefusal(new Error('node is shedding load'))).toBe(true);
    expect(isRefusal(new Error('the AI returned 0 cards'))).toBe(false);
    expect(isRefusal(new Error('agent http: deadline exceeded'))).toBe(false);
    expect(isRefusal('not an error')).toBe(false);
  });
});
