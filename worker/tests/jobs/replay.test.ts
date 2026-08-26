// The kill matrix over the four real graphs. At every step boundary the cell
// is dropped and rebuilt over the same storage, which is the only crash an
// in-process test can stage, and the run must land in the same place: the
// same rows in the owner, each step run once, and a transition sequence that
// only ever goes up.
//
// The step bodies are the lane's own, not the four workflows': what is under
// test is the ledger, and a handler that counted its own calls is exactly
// what the assertion needs.
import { describe, expect, it } from 'vitest';
import { JOB_GRAPHS, WORKFLOW_TYPE, type JobKind } from '../../app/jobs/graph.js';
import { llmStep, writeStep } from '../../app/jobs/registry.js';
import { jobHarness, seedOwner, type JobHarness } from './harness.js';
import { USER } from '../repos/setup.js';

interface Run {
  /** Every LLM step key the handlers saw, in order. */
  llm: string[];
  /** Every write step key, including the ones a re-run answered from the ledger. */
  write: string[];
  /** Rows the owner ended up with. */
  questions: number;
  status: string;
  transitions: number[];
  activations: number;
}

const PLAN_ITEMS = ['alpha', 'bravo', 'charlie'];

function register(h: JobHarness, calls: { llm: string[]; write: string[] }, killAt: { key: string } | null): void {
  const died = new Set<string>();
  const llm = (items?: readonly unknown[]) =>
    llmStep(async (ctx) => {
      calls.llm.push(ctx.stepKey);
      const value = items ?? `${ctx.name}:${String(ctx.itemInput ?? ctx.item)}`;
      return { value, ...(items ? { items } : {}), progress: { [`${ctx.name}_count`]: ctx.item + 1 } };
    });
  const write = writeStep(async (ctx) => {
    calls.write.push(ctx.stepKey);
    const existing = ctx.repos.idempotency.findQuestion(ctx.stepKey);
    if (existing !== null) return { value: existing };
    const deck = ctx.repos.decks.getOrCreate('capitals');
    // `add` transacts on its own; a cell's storage refuses a second BEGIN.
    const qid = ctx.repos.questions.add(deck, { type: 'short', prompt: ctx.stepKey, answer: 'a' });
    ctx.repos.idempotency.recordQuestion(ctx.stepKey, qid);
    // The dangerous kill: the data row landed, the step row has not. A
    // re-run must find the key, not write a second row.
    if (killAt && killAt.key === ctx.stepKey && !died.has(ctx.stepKey)) {
      died.add(ctx.stepKey);
      throw new Error('the node went away mid-step');
    }
    return { value: qid };
  });
  h.registry.register('plan', llm(PLAN_ITEMS));
  h.registry.register('expand', llm());
  h.registry.register('insert', write);
  h.registry.register('compute', llm());
  h.registry.register('apply', write);
  h.registry.register('generate', llm(['q1', 'q2']));
  h.registry.register('grade', llm());
  h.registry.register('record', write);
}

const START: Record<JobKind, { id: string; input: Record<string, unknown>; accept?: string }> = {
  PlanGenerate: { id: 'plan-capitals-0000000001', input: { deckId: 1, deckName: 'capitals', prompt: 'p' }, accept: 'accept' },
  Transform: { id: 'transform-deck-1-0000000002', input: { scope: 'deck', targetId: 1, prompt: 'p', deckName: 'capitals' }, accept: 'apply' },
  TriviaGenerate: { id: 'trivia-capitals-0000000003', input: { deckId: 1, deckName: 'capitals', topic: 't' } },
  GradeAnswer: { id: 'grade-capitals-q1-0000000004', input: { questionId: 1, deckName: 'capitals', userAnswer: 'a', idk: false } },
};

/**
 * Drives one job to terminal, restarting the cell after the numbered
 * activation. Activation 0 is the start RPC itself.
 */
async function run(kind: JobKind, opts: { killAfter?: number; killInStep?: string } = {}): Promise<Run> {
  const h = jobHarness({ graphs: JOB_GRAPHS });
  seedOwner(h, USER, { push: false });
  const calls = { llm: [] as string[], write: [] as string[] };
  register(h, calls, opts.killInStep ? { key: opts.killInStep } : null);
  const spec = START[kind];

  await h.jobCell(spec.id).start({
    id: spec.id,
    kind,
    owner: USER,
    input: spec.input,
    urlPath: `/x/${spec.id}`,
    workflowType: WORKFLOW_TYPE[kind],
    deckId: 1,
    deckName: 'capitals',
    at: h.clock.now().toISOString(),
  });
  let activations = 0;
  if (opts.killAfter === 0) await h.restart(spec.id);

  for (let guard = 0; guard < 60; guard++) {
    const ledger = h.ledger(spec.id);
    if (ledger.job['state'] === 'terminal' && h.jobStorage(spec.id).alarmAt === null) break;
    if (ledger.job['state'] === 'gated' && spec.accept) {
      await h.jobCell(spec.id).signal({ name: spec.accept, at: h.clock.now().toISOString() });
      activations++;
      if (activations === opts.killAfter) await h.restart(spec.id);
      continue;
    }
    if (!(await h.tick(spec.id))) break;
    activations++;
    if (activations === opts.killAfter) await h.restart(spec.id);
  }

  const repos = h.repos();
  const deck = repos.decks.findId('capitals');
  return {
    llm: calls.llm,
    write: calls.write,
    questions: deck === null ? 0 : repos.questions.listInDeck(deck).length,
    status: repos.jobProgress.get(spec.id)?.status ?? 'gone',
    transitions: h.statusWrites.filter((w) => w.jobId === spec.id).map((w) => w.transition),
    activations,
  };
}

const KINDS: JobKind[] = ['PlanGenerate', 'Transform', 'TriviaGenerate', 'GradeAnswer'];

/** The kill points each kind must offer: the start RPC plus one per
 * activation. Named so a graph that quietly loses a step fails here rather
 * than passing a smaller matrix. */
const KILL_POINTS: Record<JobKind, number> = { PlanGenerate: 5, Transform: 4, TriviaGenerate: 3, GradeAnswer: 3 };

/** No key twice, and each step's key exactly once. */
function expectExactlyOnce(run: Run, reference: Run): void {
  expect(new Set(run.llm)).toEqual(new Set(reference.llm));
  expect(run.llm.length).toBe(reference.llm.length);
  expect(new Set(run.write)).toEqual(new Set(reference.write));
  expect(run.questions).toBe(reference.questions);
  expect(run.status).toBe('done');
}

/**
 * A restart can re-offer an undelivered transition, which the
 * `(jobId, transition)` guard drops; what must never happen is a number
 * going backwards or one being skipped.
 */
function expectMonotonic(run: Run): void {
  let high = 0;
  for (const t of run.transitions) {
    expect(t, 'a transition was delivered out of order').toBeGreaterThanOrEqual(high);
    high = t;
  }
  expect([...new Set(run.transitions)].sort((a, b) => a - b)).toEqual(Array.from({ length: high }, (_, i) => i + 1));
}

describe('the reference run', () => {
  const runs = new Map<JobKind, Run>();

  it('takes each kind to done, each step key once', async () => {
    for (const kind of KINDS) {
      const r = await run(kind);
      runs.set(kind, r);
      expect(r.status, kind).toBe('done');
      expectMonotonic(r);
      expect(new Set(r.llm).size, kind).toBe(r.llm.length);
    }
    expect(runs.get('PlanGenerate')!.llm).toEqual([
      'plan-capitals-0000000001-plan-0',
      'plan-capitals-0000000001-expand-0',
      'plan-capitals-0000000001-expand-1',
      'plan-capitals-0000000001-expand-2',
    ]);
    expect(runs.get('PlanGenerate')!.write).toEqual([
      'plan-capitals-0000000001-insert-0',
      'plan-capitals-0000000001-insert-1',
      'plan-capitals-0000000001-insert-2',
    ]);
    // The grading record's key is the job id itself.
    expect(runs.get('GradeAnswer')!.write).toEqual(['grade-capitals-q1-0000000004']);
  });
});

describe('a kill at every step boundary', () => {
  for (const kind of KINDS) {
    it(`replays ${kind} from the rows alone`, async () => {
      const reference = await run(kind);
      expect(reference.activations + 1, `${kind} offers fewer kill points than the matrix names`).toBeGreaterThanOrEqual(KILL_POINTS[kind]);
      for (let killAfter = 0; killAfter <= reference.activations; killAfter++) {
        const killed = await run(kind, { killAfter });
        expectExactlyOnce(killed, reference);
        expectMonotonic(killed);
      }
    });
  }
});

describe('the named kill points', () => {
  it('gate entry: the deadline survives a restart and the gate still waits', async () => {
    const h = jobHarness({ graphs: JOB_GRAPHS });
    seedOwner(h, USER, { push: false });
    register(h, { llm: [], write: [] }, null);
    const id = 'plan-capitals-0000000009';
    await h.jobCell(id).start({
      id,
      kind: 'PlanGenerate',
      owner: USER,
      input: { deckId: 1, deckName: 'capitals', prompt: 'p' },
      urlPath: `/plan/${id}`,
      workflowType: 'plan',
      deckId: 1,
      deckName: 'capitals',
      at: h.clock.now().toISOString(),
    });
    await h.tick(id);
    const deadline = h.ledger(id).job['deadline_at'];
    expect(h.ledger(id).job['state']).toBe('gated');
    await h.restart(id);
    expect(h.ledger(id).job['deadline_at']).toBe(deadline);
    expect(h.jobStorage(id).alarmAt).toBe(new Date(deadline as string).getTime());
    expect(h.repos().jobProgress.get(id)?.status).toBe('awaiting_feedback');
  });

  it('signal persisted, not run: the restart picks the event up from the rows', async () => {
    const h = jobHarness({ graphs: JOB_GRAPHS });
    seedOwner(h, USER, { push: false });
    const calls = { llm: [] as string[], write: [] as string[] };
    register(h, calls, null);
    const id = 'plan-capitals-0000000010';
    await h.jobCell(id).start({
      id,
      kind: 'PlanGenerate',
      owner: USER,
      input: { deckId: 1, deckName: 'capitals', prompt: 'p' },
      urlPath: `/plan/${id}`,
      workflowType: 'plan',
      deckId: 1,
      deckName: 'capitals',
      at: h.clock.now().toISOString(),
    });
    await h.tick(id);
    const after = await h.jobCell(id).signal({ name: 'accept', at: h.clock.now().toISOString() });
    expect(after?.status).toBe('accepting');
    await h.restart(id);
    for (let i = 0; i < 20 && (await h.tick(id)); i++);
    expect(h.repos().jobProgress.get(id)?.status).toBe('done');
    expect(new Set(calls.llm).size).toBe(calls.llm.length);
  });

  it('deadline fire: a cold cell rejects with no request at all', async () => {
    const h = jobHarness({ graphs: JOB_GRAPHS });
    seedOwner(h, USER, { push: false });
    register(h, { llm: [], write: [] }, null);
    const id = 'transform-deck-1-0000000011';
    await h.jobCell(id).start({
      id,
      kind: 'Transform',
      owner: USER,
      input: { scope: 'deck', targetId: 1, prompt: 'p', deckName: 'capitals' },
      urlPath: `/transform/${id}`,
      workflowType: 'transform',
      deckId: 1,
      deckName: 'capitals',
      at: h.clock.now().toISOString(),
    });
    await h.tick(id);
    expect(h.ledger(id).job['state']).toBe('gated');
    await h.restart(id);
    for (let i = 0; i < 20 && (await h.tick(id)); i++);
    expect(h.repos().jobProgress.get(id)?.status).toBe('rejected');
    expect(h.repos().questions.listInDeck(h.repos().decks.findId('capitals') ?? 0).length).toBe(0);
  });

  it('a write that landed before the node died is not written twice', async () => {
    const key = 'trivia-capitals-0000000003-insert-0';
    const reference = await run('TriviaGenerate');
    const killed = await run('TriviaGenerate', { killInStep: key });
    expect(killed.questions).toBe(reference.questions);
    // The step ran twice; the ledger answered the second one.
    expect(killed.write.filter((k) => k === key).length).toBe(2);
    expect(killed.status).toBe('done');
    expectMonotonic(killed);
  });
});
