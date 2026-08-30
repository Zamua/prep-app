// The step ledger driven end to end: a real JobCell, a real UserCell, the
// alarm the test fires. Every assertion is about rows, because rows are all a
// re-activation has.
import { beforeEach, describe, expect, it } from 'vitest';
import { llmStep, writeStep } from '../../app/jobs/registry.js';
import type { StepGraph } from '../../domain/jobs/graph.js';
import { DurabilityUnproven, MAX_DELIVERY_ATTEMPTS, MAX_REFUSALS } from '../../domain/jobs/refusal.js';
import { isoUtc } from '../../domain/time.js';
import { jobHarness, seedOwner, type JobHarness } from './harness.js';
import { MutableClock, USER } from '../repos/setup.js';

const RETRY = { attempts: 3, initialMs: 1_000, coefficient: 2, capMs: 30_000 };
const ONCE = { attempts: 1, initialMs: 2_000, coefficient: 2, capMs: 30_000 };

/** plan -> gate(1h) -> expand (batch of 2, skip) -> insert (per item). */
const GRAPH: StepGraph = {
  kind: 'Demo',
  partial: null,
  doneStatus: 'done',
  progressSeed: (input) => ({ scope: input['scope'] ?? null }),
  nodes: [
    { name: 'plan', kind: 'llm', retry: ONCE, status: 'planning' },
    {
      name: 'gate',
      kind: 'gate',
      retry: ONCE,
      status: 'awaiting_feedback',
      gate: {
        events: ['accept', 'reject', 'feedback'],
        deadlineMs: 3_600_000,
        refreshOnEvent: false,
        onEvent: {
          accept: { transient: 'accepting', go: 'proceed' },
          reject: { transient: 'rejecting', go: 'reject' },
          feedback: { transient: 'replanning', go: { rerun: 'plan' } },
        },
        onDeadline: 'reject',
        rerunError: 'replan failed: ',
      },
    },
    { name: 'expand', kind: 'llm', retry: ONCE, fanout: { mode: 'batch', size: 2, from: 'plan' }, onError: 'skip', status: 'generating', emptyError: 'every card expansion failed' },
    { name: 'insert', kind: 'write', retry: RETRY, fanout: { mode: 'per-item', from: 'expand' }, onError: 'skip', status: 'applying' },
  ],
};

/** grade -> record, no gate: the shape with no human in it. */
const LINEAR: StepGraph = {
  kind: 'Linear',
  partial: null,
  doneStatus: 'done',
  nodes: [
    { name: 'grade', kind: 'llm', retry: ONCE, status: 'grading' },
    { name: 'record', kind: 'write', retry: RETRY, keyIsJobId: true, status: 'recording' },
  ],
};

const GRAPHS = { Demo: GRAPH, Linear: LINEAR };

interface Calls {
  llm: string[];
  write: string[];
}

/** The step keys the handlers saw, which is how "exactly once" is measured. */
function register(h: JobHarness, opts: { plan?: unknown[]; expandFails?: number[]; refuseOnce?: Set<string>; failWrite?: Set<string> } = {}): Calls {
  const calls: Calls = { llm: [], write: [] };
  const refused = new Set<string>();
  h.registry.register(
    'plan',
    llmStep(async (ctx) => {
      calls.llm.push(ctx.stepKey);
      const items = opts.plan ?? ['a', 'b', 'c'];
      return { value: items, items, progress: { plan: items, round: ctx.item + 1, total: items.length } };
    }),
  );
  h.registry.register(
    'grade',
    llmStep(async (ctx) => {
      calls.llm.push(ctx.stepKey);
      if (opts.refuseOnce?.has(ctx.stepKey) && !refused.has(ctx.stepKey)) {
        refused.add(ctx.stepKey);
        throw new DurabilityUnproven('celld output gate: durability unproven');
      }
      return { value: { result: 'right' }, progress: {} };
    }),
  );
  h.registry.register(
    'expand',
    llmStep(async (ctx) => {
      calls.llm.push(ctx.stepKey);
      if (opts.expandFails?.includes(ctx.item)) throw new Error(`expand ${ctx.item} failed`);
      return { value: `card ${String(ctx.itemInput)}`, progress: { generated_count: ctx.item + 1 } };
    }),
  );
  const write = writeStep(async (ctx) => {
    calls.write.push(ctx.stepKey);
    if (opts.failWrite?.has(ctx.stepKey)) throw new Error('write refused');
    const existing = ctx.repos.idempotency.findQuestion(ctx.stepKey);
    if (existing !== null) return { value: existing };
    const deck = ctx.repos.decks.getOrCreate('demo');
    const prompt = String(ctx.itemInput ?? ctx.stepKey);
    const qid = ctx.repos.questions.add(deck, { type: 'short', prompt, answer: prompt });
    ctx.repos.idempotency.recordQuestion(ctx.stepKey, qid);
    return { value: qid, progress: { inserted: ctx.item + 1 } };
  });
  h.registry.register('insert', write);
  h.registry.register('record', write);
  return calls;
}

async function startDemo(h: JobHarness, input: Record<string, unknown> = {}): Promise<string> {
  const id = 'plan-demo-0123456789';
  await h.jobCell(id).start({
    id,
    kind: 'Demo',
    owner: USER,
    input: { deckName: 'demo', ...input },
    urlPath: `/plan/${id}`,
    workflowType: 'plan',
    deckId: 1,
    deckName: 'demo',
    at: h.clock.now().toISOString(),
  });
  return id;
}

let h: JobHarness;
beforeEach(() => {
  h = jobHarness({ graphs: GRAPHS });
  seedOwner(h);
});

describe('start', () => {
  it('writes the job, the first node and transition 1, and arms the alarm', async () => {
    register(h);
    const id = await startDemo(h);
    const l = h.ledger(id);
    expect(l.job['state']).toBe('running');
    expect(l.job['cursor']).toBe(0);
    expect(l.steps.map((s) => s['step_key'])).toEqual([`${id}-plan-0`]);
    expect(l.outbox.map((o) => [o['transition'], o['status']])).toEqual([[1, 'planning']]);
    expect(h.jobStorage(id).alarmAt).not.toBeNull();
  });

  it('is idempotent: a repeated start writes no second job and no second transition', async () => {
    register(h);
    const id = await startDemo(h);
    await startDemo(h);
    const l = h.ledger(id);
    expect(l.outbox.length).toBe(1);
    expect(l.steps.length).toBe(1);
  });

  it('does not run the step inside the RPC: the owner is mid request', async () => {
    const calls = register(h);
    await startDemo(h);
    expect(calls.llm).toEqual([]);
  });

  it('arms the wake on wall time, so a pinned clock cannot hand two steps the same instant', async () => {
    const wall = new MutableClock(new Date('2026-08-26T12:00:00Z'));
    const pinned = jobHarness({ graphs: GRAPHS, wallClock: wall });
    seedOwner(pinned);
    register(pinned);
    const id = await startDemo(pinned);
    expect(pinned.jobStorage(id).alarmAt).toBe(wall.now().getTime() + 1);
  });
  it('reuses a caller idempotency key across outer retries', async () => {
    const actual = jobHarness({ graphs: { GradeAnswer: { ...LINEAR, kind: 'GradeAnswer' } } });
    seedOwner(actual);
    register(actual);
    const runner = actual.runner();
    const input = {
      questionId: 12,
      deckName: 'capitals',
      userAnswer: 'Lima',
      idk: false,
      sessionId: '0123456789abcdef',
      card: { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima', rubric: '' },
    };

    const key = '0123456789abcdefv1';
    const first = await runner.start('GradeAnswer', input, { idempotencyKey: key });
    const second = await runner.start(
      'GradeAnswer',
      { ...input, deckName: 'renamed-capitals' },
      { idempotencyKey: key },
    );

    expect(first.workflowId).toBe('grade-session-q12-0123456789abcdefv1');
    expect(second).toEqual(first);
    expect(actual.ledger(first.workflowId).outbox).toHaveLength(1);

    await actual.settle();
    const terminal = actual.ledger(first.workflowId);
    expect(terminal.job['state']).toBe('terminal');
    actual.clock.set(new Date(actual.clock.now().getTime() + 1_000));

    const afterCompletion = await runner.start(
      'GradeAnswer',
      { ...input, deckName: 'renamed-again' },
      { idempotencyKey: key },
    );

    expect(afterCompletion).toEqual(first);
    expect(actual.ledger(first.workflowId).job['created_at']).toBe(terminal.job['created_at']);
    expect(actual.ledger(first.workflowId).outbox).toHaveLength(terminal.outbox.length);
  });
});

describe('driving to a gate', () => {
  it('runs the first step, enters the gate, and writes the deadline once', async () => {
    register(h);
    const id = await startDemo(h);
    await h.settle();
    const l = h.ledger(id);
    expect(l.job['state']).toBe('gated');
    expect(l.job['deadline_at']).toBe(isoUtc(new Date(h.clock.now().getTime() + 3_600_000)));
    expect(l.job['deadline_kind']).toBe('reject');
    expect(l.outbox.map((o) => o['status'])).toEqual(['planning', 'awaiting_feedback']);
  });

  it('parks: an alarm with nothing due is a no-op', async () => {
    register(h);
    const id = await startDemo(h);
    await h.settle();
    const before = h.ledger(id);
    await h.jobCell(id).alarm();
    expect(h.ledger(id).outbox.length).toBe(before.outbox.length);
  });

  it('carries the accept through: transient first, then the next node', async () => {
    const calls = register(h);
    const id = await startDemo(h);
    await h.settle();
    const after = await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    expect(after?.status).toBe('accepting');
    await h.settle();
    expect(h.ledger(id).outbox.map((o) => o['status'])).toEqual(['planning', 'awaiting_feedback', 'accepting', 'generating', 'applying', 'done']);
    expect(calls.llm).toEqual([`${id}-plan-0`, `${id}-expand-0`, `${id}-expand-1`, `${id}-expand-2`]);
    expect(calls.write).toEqual([`${id}-insert-0`, `${id}-insert-1`, `${id}-insert-2`]);
    expect(h.repos().questions.listInDeck(h.repos().decks.findId('demo')!).length).toBe(3);
  });

  it('rejects with the transient before the terminal', async () => {
    register(h);
    const id = await startDemo(h);
    await h.settle();
    const after = await h.jobCell(id).signal({ name: 'reject', at: h.clock.now().toISOString() });
    expect(after?.status).toBe('rejecting');
    await h.settle();
    expect(h.ledger(id).outbox.map((o) => o['status'])).toEqual(['planning', 'awaiting_feedback', 'rejecting', 'rejected']);
    expect(h.ledger(id).job['state']).toBe('terminal');
  });

  it('consumes a duplicated signal with the one it resolved, so it cannot resolve a second gate', async () => {
    register(h);
    const id = await startDemo(h);
    await h.settle();
    await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    await h.settle();
    const l = h.ledger(id);
    expect(l.events.every((e) => e['consumed_at'] !== null)).toBe(true);
    expect(l.outbox.filter((o) => o['status'] === 'accepting').length).toBe(1);
  });

  it('leaves a second, different signal for the gate it comes back to', async () => {
    const calls = register(h);
    const id = await startDemo(h);
    await h.settle();
    // Two tabs, or a double submit: the feedback resolves this round, and the
    // accept has to survive it or the user's second press does nothing.
    await h.jobCell(id).signal({ name: 'feedback', payload: 'fewer', at: h.clock.now().toISOString() });
    await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    await h.settle();
    const l = h.ledger(id);
    // Both rounds of the plan, then the accept carries the job through.
    expect(calls.llm.slice(0, 2)).toEqual([`${id}-plan-0`, `${id}-plan-1`]);
    expect(l.outbox.map((o) => o['status'])).toEqual(['planning', 'awaiting_feedback', 'replanning', 'awaiting_feedback', 'accepting', 'generating', 'applying', 'done']);
    expect(l.events.every((e) => e['consumed_at'] !== null)).toBe(true);
  });

  it('re-runs the plan on feedback with a fresh key and the same deadline', async () => {
    const calls = register(h);
    const id = await startDemo(h);
    await h.settle();
    const deadline = h.ledger(id).job['deadline_at'];
    h.clock.advance(60_000);
    await h.jobCell(id).signal({ name: 'feedback', payload: { text: 'more cards' }, at: h.clock.now().toISOString() });
    await h.settle();
    const l = h.ledger(id);
    expect(l.job['deadline_at']).toBe(deadline);
    expect(l.job['state']).toBe('gated');
    expect(calls.llm).toEqual([`${id}-plan-0`, `${id}-plan-1`]);
    expect(l.outbox.map((o) => o['status'])).toEqual(['planning', 'awaiting_feedback', 'replanning', 'awaiting_feedback']);
  });

  it('fires the deadline from a cold cell with no request', async () => {
    register(h);
    const id = await startDemo(h);
    await h.settle();
    expect(h.jobStorage(id).alarmAt).toBe(new Date(h.ledger(id).job['deadline_at'] as string).getTime());
    await h.settleThrough(3_600_001);
    const l = h.ledger(id);
    expect(l.outbox.map((o) => o['status'])).toEqual(['planning', 'awaiting_feedback', 'rejecting', 'rejected']);
    expect(l.job['terminal_status']).toBe('rejected');
  });
});

describe('failures', () => {
  it('skips a failed expansion without failing its siblings', async () => {
    const calls = register(h, { expandFails: [1] });
    const id = await startDemo(h);
    await h.settle();
    await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    await h.settleThrough(120_000);
    const l = h.ledger(id);
    expect(l.steps.filter((s) => s['name'] === 'expand').map((s) => s['status'])).toEqual(['done', 'skipped', 'done']);
    expect(calls.write.length).toBe(2);
    expect(l.job['terminal_status']).toBe('done');
  });

  it('fails the job with the graph message when every row of a node is skipped', async () => {
    register(h, { expandFails: [0, 1, 2] });
    const id = await startDemo(h);
    await h.settle();
    await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    await h.settleThrough(120_000);
    const l = h.ledger(id);
    expect(l.job['terminal_status']).toBe('failed');
    expect(l.job['error']).toBe('every card expansion failed');
    expect(h.repos().jobProgress.get(id)?.progress['error']).toBe('every card expansion failed');
  });

  it('retries a write to its policy, then skips the row', async () => {
    const calls = register(h, { plan: ['a'], failWrite: new Set([`plan-demo-0123456789-insert-0`]) });
    const id = await startDemo(h);
    await h.settle();
    await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    await h.settleThrough(120_000);
    expect(calls.write.length).toBe(3);
    const attempts = h.ledger(id).steps.filter((s) => s['name'] === 'insert');
    expect(attempts.map((s) => [s['status'], s['attempt']])).toEqual([['skipped', 3]]);
  });

  it('hands a failed re-plan back to the gate with the error, keeping the prior plan', async () => {
    const rounds: number[] = [];
    // The first plan lands; every re-plan fails, which is the case the gate
    // has to survive without losing what the user is looking at.
    h.registry.register(
      'plan',
      llmStep(async (ctx) => {
        rounds.push(ctx.item);
        if (ctx.item > 0) throw new Error('model unavailable');
        const items = ['a', 'b', 'c'];
        return { value: items, items, progress: { plan: items, round: 1, total: 3 } };
      }),
    );
    const id = await startDemo(h);
    await h.settle();
    await h.jobCell(id).signal({ name: 'feedback', at: h.clock.now().toISOString() });
    await h.settleThrough(120_000);
    const status = h.repos().jobProgress.get(id)!;
    expect(rounds).toEqual([0, 1]);
    expect(status.status).toBe('awaiting_feedback');
    expect(status.progress['error']).toBe('replan failed: model unavailable');
    expect(status.progress['plan']).toEqual(['a', 'b', 'c']);
    expect(h.ledger(id).job['state']).toBe('gated');
  });

});

describe('refusals', () => {
  it('costs a refusal, not an attempt, and lands the step exactly once', async () => {
    const id = 'grade-demo-q1-0123456789';
    const calls = register(h, { refuseOnce: new Set([`${id}-grade-0`]) });
    await h.jobCell(id).start({
      id,
      kind: 'Linear',
      owner: USER,
      input: { deckName: 'demo' },
      urlPath: `/grading/${id}`,
      workflowType: 'grading',
      deckId: null,
      deckName: 'demo',
      at: h.clock.now().toISOString(),
    });
    await h.settleThrough(60_000);
    const l = h.ledger(id);
    const grade = l.steps.find((s) => s['name'] === 'grade')!;
    expect([grade['status'], grade['attempt'], grade['refusals']]).toEqual(['done', 1, 1]);
    expect(calls.llm.filter((k) => k === `${id}-grade-0`).length).toBe(2);
    expect(calls.write).toEqual([id]);
    expect(l.job['terminal_status']).toBe('done');
  });
});

describe('the post-restart window', () => {
  /** celld's own wording for a cell whose owner has not come back yet. */
  const unreachable = () => {
    const e = new Error('The Durable Object owner is currently unreachable');
    e.name = 'DurableObjectRoutingError';
    return e;
  };

  it('defers the status write and delivers it once when the owner comes back', async () => {
    register(h);
    const id = await startDemo(h);
    let refusals = 3;
    h.interfere(({ method }) => {
      if (method === 'jobStatus' && refusals-- > 0) throw unreachable();
    });
    await h.settleThrough(60_000);
    h.interfere(null);
    await h.settleThrough(60_000);
    const first = h.ledger(id).outbox.find((o) => o['transition'] === 1)!;
    // The deferral is a row, not a timer held in an isolate: the attempt
    // count survives every restart the window would have caused.
    expect(Number(first['attempt'])).toBeGreaterThan(1);
    expect(h.statusWrites.filter((w) => w.jobId === id && w.transition === 1).length).toBeGreaterThan(1);
    expect(h.repos().jobProgress.get(id)?.status).toBe('awaiting_feedback');
    expect(h.ledger(id).outbox.every((o) => o['delivered_at'] !== null)).toBe(true);
  });

  it('counts a refused write step as a refusal, not an attempt, and lands one row', async () => {
    const calls = register(h, { plan: ['a'] });
    const id = await startDemo(h);
    await h.settle();
    await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    let refusals = 2;
    h.interfere(({ method }) => {
      if (method === 'applyJobStep' && refusals-- > 0) throw unreachable();
    });
    await h.settleThrough(60_000);
    h.interfere(null);
    await h.settleThrough(60_000);
    const insert = h.ledger(id).steps.find((s) => s['name'] === 'insert')!;
    expect([insert['status'], insert['attempt'], insert['refusals']]).toEqual(['done', 1, 2]);
    expect(calls.write.filter((k) => k === `${id}-insert-0`).length).toBe(1);
    expect(h.repos().questions.listInDeck(h.repos().decks.findId('demo')!).length).toBe(1);
    expect(h.repos().jobProgress.get(id)?.status).toBe('done');
  });

  it('abandons a status write the owner will never take, and lets the cell sleep', async () => {
    register(h, { plan: ['a'] });
    const id = await startDemo(h);
    // Permanent, not the post-restart window: this owner refuses forever.
    h.interfere(({ method }) => {
      if (method === 'jobStatus') throw new Error('no such table: job_progress');
    });
    await h.settleThrough(600_000);
    await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    await h.settleThrough(600_000);

    const outbox = h.ledger(id).outbox;
    expect(h.ledger(id).job['state']).toBe('terminal');
    expect(outbox.every((o) => o['delivered_at'] === null)).toBe(true);
    expect(outbox.map((o) => o['attempt'])).toEqual(outbox.map(() => MAX_DELIVERY_ATTEMPTS));
    // The bound is what stops the cell waking every eight seconds for the
    // life of the deployment over rows nobody will ever accept.
    expect(h.jobStorage(id).alarmAt).toBeNull();
  });

  it('gives up on a step that refuses past the twelve-refusal cap', async () => {
    register(h, { plan: ['a'] });
    const id = await startDemo(h);
    await h.settle();
    await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    h.interfere(({ method }) => {
      if (method === 'applyJobStep') throw unreachable();
    });
    await h.settleThrough(600_000);
    const insert = h.ledger(id).steps.find((s) => s['name'] === 'insert')!;
    expect(insert['refusals']).toBe(MAX_REFUSALS - 1);
    expect(insert['status']).toBe('skipped');
  });
});

describe('the seed reset', () => {
  it('empties the ledger, so a run that mints the same id starts over', async () => {
    register(h);
    const id = await startDemo(h);
    await h.settle();
    await h.jobCell(id).wipe();
    expect(await h.peek(id)).toBeNull();
    expect(h.jobStorage(id).alarmAt).toBeNull();
    await startDemo(h);
    expect(h.ledger(id).outbox.map((o) => o['status'])).toEqual(['planning']);
  });
});
