// The graphs as data: well formed, retrying only what is safe to retry, and
// naming only status literals the partials actually render.
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
  // A retried model call bills again; a write lands once under its key.
  it('never retry an LLM call, and do retry a write', () => {
    const nodes = graphs.flatMap(([kind, g]) => g.nodes.map((n) => [`${kind}.${n.name}`, n] as const));
    expect(nodes.some(([, n]) => n.kind === 'llm') && nodes.some(([, n]) => n.kind === 'write')).toBe(true);
    for (const [name, n] of nodes) {
      if (n.kind === 'llm') expect(n.retry?.attempts, name).toBe(1);
      if (n.kind === 'write') expect(n.retry?.attempts, name).toBeGreaterThan(1);
    }
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
