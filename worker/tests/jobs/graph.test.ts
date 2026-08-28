// The graphs as data: well formed, matching the Go worker's retry policies,
// and naming only status literals the partials actually render.
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { JOB_GRAPHS, jobRoute, WORKFLOW_TYPE, type JobKind } from '../../app/jobs/graph.js';
import { DuplicateStep, llmStep, StepRegistry, unregisteredSteps, UnknownStep, writeStep, type StepContext } from '../../app/jobs/registry.js';
import { statusLiterals, validateGraph, type StepGraph } from '../../domain/jobs/graph.js';
import { gradeId, planId, transformId, triviaId } from '../../domain/jobs/ids.js';

const ROOT = new URL('../..', import.meta.url).pathname;
const graphs = Object.entries(JOB_GRAPHS) as [JobKind, StepGraph][];

describe('every declared graph', () => {
  it('holds the invariants the runner assumes', () => {
    for (const [kind, graph] of graphs) expect(() => validateGraph(graph), kind).not.toThrow();
  });

  it('names one workflow_type per kind', () => {
    expect(WORKFLOW_TYPE).toEqual({ PlanGenerate: 'plan', Transform: 'transform', TriviaGenerate: 'trivia_gen', GradeAnswer: 'grading' });
  });

  it('renders every status literal it can write in its own partial', () => {
    for (const [kind, graph] of graphs) {
      if (graph.partial === null) continue;
      const html = readFileSync(join(ROOT, 'templates', graph.partial), 'utf8');
      for (const status of statusLiterals(graph)) {
        expect(html.includes(`'${status}'`), `${kind}: ${graph.partial} never mentions ${status}`).toBe(true);
      }
    }
  });

  it('keys the grading record on the job id and nothing else', () => {
    const keyed = graphs.flatMap(([kind, g]) => g.nodes.filter((n) => n.keyIsJobId).map((n) => `${kind}.${n.name}`));
    expect(keyed).toEqual(['GradeAnswer.record']);
  });
});

describe('the retry policies', () => {
  const policyOf = (kind: JobKind, name: string) => JOB_GRAPHS[kind].nodes.find((n) => n.name === name)!.retry;

  it('are the Go worker values, transcribed', () => {
    // No retry on an LLM call, whatever the workflow.
    for (const [kind, name] of [
      ['PlanGenerate', 'plan'],
      ['PlanGenerate', 'expand'],
      ['Transform', 'compute'],
      ['TriviaGenerate', 'generate'],
      ['GradeAnswer', 'grade'],
    ] as [JobKind, string][]) {
      expect(policyOf(kind, name), `${kind}.${name}`).toEqual({ attempts: 1, initialMs: 2_000, coefficient: 2, capMs: 30_000 });
    }
    expect(policyOf('PlanGenerate', 'insert')).toEqual({ attempts: 3, initialMs: 1_000, coefficient: 2, capMs: 30_000 });
    expect(policyOf('Transform', 'apply')).toEqual({ attempts: 3, initialMs: 1_000, coefficient: 2, capMs: 30_000 });
    expect(policyOf('TriviaGenerate', 'insert')).toEqual({ attempts: 3, initialMs: 500, coefficient: 2, capMs: 30_000 });
    expect(policyOf('GradeAnswer', 'record')).toEqual({ attempts: 5, initialMs: 1_000, coefficient: 2, capMs: 30_000 });
  });

  it('gives the two human gates their Go budgets and never refreshes them', () => {
    const plan = JOB_GRAPHS.PlanGenerate.nodes.find((n) => n.kind === 'gate')!.gate!;
    const transform = JOB_GRAPHS.Transform.nodes.find((n) => n.kind === 'gate')!.gate!;
    expect(plan.deadlineMs).toBe(24 * 3_600_000);
    expect(transform.deadlineMs).toBe(3_600_000);
    expect([plan.refreshOnEvent, transform.refreshOnEvent]).toEqual([false, false]);
    expect([plan.onDeadline, transform.onDeadline]).toEqual(['reject', 'reject']);
  });

  it('skips the transform gate for card scope only', () => {
    const gate = JOB_GRAPHS.Transform.nodes.find((n) => n.kind === 'gate')!;
    expect(gate.onlyWhen).toEqual({ input: 'scope', in: ['deck', 'reorganize'] });
  });

  it('expands in batches of four and inserts per item, as the Go workflow does', () => {
    expect(JOB_GRAPHS.PlanGenerate.nodes.find((n) => n.name === 'expand')!.fanout).toEqual({ mode: 'batch', size: 4, from: 'plan' });
    expect(JOB_GRAPHS.PlanGenerate.nodes.find((n) => n.name === 'insert')!.fanout).toEqual({ mode: 'per-item', from: 'expand' });
    expect(JOB_GRAPHS.TriviaGenerate.nodes.find((n) => n.name === 'insert')!.fanout).toEqual({ mode: 'per-item', from: 'generate' });
  });

  it("keeps the Go don't-fail-siblings rule where the Go workflow has it", () => {
    const skipping = graphs.flatMap(([kind, g]) => g.nodes.filter((n) => n.onError === 'skip').map((n) => `${kind}.${n.name}`));
    expect(skipping.sort()).toEqual(['PlanGenerate.expand', 'PlanGenerate.insert', 'TriviaGenerate.insert']);
  });
});

describe('validateGraph', () => {
  const base: StepGraph = JOB_GRAPHS.TriviaGenerate;
  const broken = (nodes: StepGraph['nodes']): StepGraph => ({ ...base, nodes });

  it('refuses a gate with an outcome missing, a deadline outside its events, or a re-run forward', () => {
    const gate = JOB_GRAPHS.PlanGenerate.nodes[1]!;
    const plan = JOB_GRAPHS.PlanGenerate.nodes[0]!;
    expect(() => validateGraph(broken([plan, { ...gate, gate: { ...gate.gate!, onEvent: {} } }]))).toThrow(/no outcome/);
    expect(() => validateGraph(broken([plan, { ...gate, gate: { ...gate.gate!, onDeadline: 'nope' } }]))).toThrow(/deadlines to/);
    expect(() =>
      validateGraph(broken([plan, { ...gate, gate: { ...gate.gate!, onEvent: { ...gate.gate!.onEvent, feedback: { transient: 'x', go: { rerun: 'later' } } } } }])),
    ).toThrow(/not an earlier node/);
  });

  it('refuses a fanout whose source is not an earlier node, and a graph that opens on a gate', () => {
    const insert = base.nodes[1]!;
    expect(() => validateGraph(broken([insert]))).toThrow(/not an earlier node/);
    expect(() => validateGraph(broken([JOB_GRAPHS.PlanGenerate.nodes[1]!]))).toThrow(/opens on a gate/);
  });

  it('refuses a duplicate node name and a retry that cannot back off', () => {
    expect(() => validateGraph(broken([base.nodes[0]!, base.nodes[0]!]))).toThrow(/twice/);
    expect(() => validateGraph(broken([{ ...base.nodes[0]!, retry: { attempts: 0, initialMs: 1, coefficient: 2, capMs: 2 } }]))).toThrow(/allows 0 attempts/);
  });
});

describe('the registry', () => {
  const noop = async () => ({});

  it('refuses a second handler for a name, because the loser would be unreachable', () => {
    const registry = new StepRegistry().register('plan', llmStep(noop));
    expect(() => registry.register('plan', llmStep(noop))).toThrow(DuplicateStep);
  });

  it('names the missing handler rather than running nothing', () => {
    expect(() => new StepRegistry().get('plan')).toThrow(UnknownStep);
  });

  it('lists every node with no handler, gates excluded', () => {
    const empty = new StepRegistry();
    const expected = graphs.flatMap(([, g]) => g.nodes.filter((n) => n.kind !== 'gate').map((n) => n.name));
    expect(unregisteredSteps(JOB_GRAPHS, empty)).toEqual([...new Set(expected)].sort());
    empty.register('plan', llmStep(noop));
    expect(unregisteredSteps(JOB_GRAPHS, empty)).not.toContain('plan');
  });

  it('refuses to run a write handler in the JobCell, or an LLM handler in the owner cell', async () => {
    const write = writeStep(noop);
    const llm = llmStep(noop);
    const jobSite = { site: 'job', name: 'insert' } as unknown as StepContext;
    const ownerSite = { site: 'owner', name: 'plan' } as unknown as StepContext;
    await expect(write(jobSite)).rejects.toThrow(/ran at job, not owner/);
    await expect(llm(ownerSite)).rejects.toThrow(/ran at owner, not job/);
  });
});

describe('workflow ids and the routes that parse them', () => {
  it('keep the persisted payload shapes verbatim', () => {
    expect(gradeId('capitals', 12, 'abcdef0123')).toBe('grade-capitals-q12-abcdef0123');
    expect(transformId('deck', 4, 'abcdef0123')).toBe('transform-deck-4-abcdef0123');
    expect(planId('capitals', 'abcdef0123')).toBe('plan-capitals-abcdef0123');
    expect(triviaId('capitals', 'abcdef0123')).toBe('trivia-capitals-abcdef0123');
  });

  it('name the deep link and the deck the badge row shows', () => {
    expect(jobRoute('PlanGenerate', 'plan-x-1', { deckId: 3, deckName: 'x' })).toEqual({ urlPath: '/plan/plan-x-1', deckId: 3, deckName: 'x' });
    expect(jobRoute('TriviaGenerate', 'trivia-x-1', { deckId: 3, deckName: 'x' })).toEqual({ urlPath: '/trivia/gen/trivia-x-1', deckId: 3, deckName: 'x' });
    expect(jobRoute('Transform', 'transform-deck-3-1', { scope: 'deck', targetId: 3, deckName: 'x' })).toEqual({
      urlPath: '/transform/transform-deck-3-1',
      deckId: 3,
      deckName: 'x',
    });
    // Card scope targets a question, so the badge row names no deck id.
    expect(jobRoute('Transform', 'transform-card-9-1', { scope: 'card', targetId: 9, deckName: 'x' })).toMatchObject({ deckId: null });
    expect(jobRoute('GradeAnswer', 'grade-x-q1-1', { deckName: 'x', sessionId: 's1' })).toEqual({
      urlPath: '/grading/grade-x-q1-1?sid=s1',
      deckId: null,
      deckName: 'x',
    });
    expect(jobRoute('GradeAnswer', 'grade-x-q1-1', { deckName: 'x' }).urlPath).toBe('/grading/grade-x-q1-1');
  });
});
