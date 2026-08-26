// The seam between lane C's routes and lane B's handlers: what a route puts
// in a job input is all an LLM step ever sees, because that step runs in the
// JobCell, which holds the agent and no repositories. Every other suite fakes
// one side of this, so a route that omits the snapshot shows the model an
// empty library and nothing goes red. Here the real route builds the input and
// the real handler consumes it.
import { beforeEach, describe, expect, it } from 'vitest';
import type { AgentPort, AgentRequest, JobInputs, JobKind, UserRepos } from '../../app/ports.js';
import { registerWorkflowSteps } from '../../app/jobs/index.js';
import { StepRegistry, type LlmStepContext } from '../../app/jobs/registry.js';
import { UserCell } from '../../runtime/cells/UserCell.js';
import { KIND_HEADER, SUBJECT_HEADER } from '../../runtime/cells/router.js';
import { composeWith } from '../../runtime/compose.js';
import { fakeCellState } from '../fakes/sqlStorage.js';
import { fakeEnv, req } from '../helpers.js';

const USER = 'parity@example.com';
const DECK = 'distributed-systems';

class CapturingRunner {
  readonly starts: { kind: JobKind; input: Record<string, unknown> }[] = [];
  async start<K extends JobKind>(kind: K, input: JobInputs[K]): Promise<{ workflowId: string }> {
    this.starts.push({ kind, input: input as unknown as Record<string, unknown> });
    return { workflowId: `${kind}-0123456789` };
  }
  async signal(): Promise<null> {
    return null;
  }
  async status(): Promise<null> {
    return null;
  }
  async terminate(): Promise<void> {}
}

/** Answers nothing usable: the assertion is the prompt it was handed. */
class PromptSpy implements AgentPort {
  readonly prompts: string[] = [];
  async complete(request: AgentRequest): Promise<string> {
    this.prompts.push(request.user);
    return '{}';
  }
}

interface Harness {
  runner: CapturingRunner;
  repos: UserRepos;
  post(path: string, body?: Record<string, string>): Promise<Response>;
  json(path: string, body: unknown): Promise<Response>;
  /** The prompt the registered LLM handler builds from the captured input. */
  prompt(name: string, input: Record<string, unknown>): Promise<string>;
}

async function harness(): Promise<Harness> {
  const env = fakeEnv();
  const runner = new CapturingRunner();
  const c = composeWith(env, { runner: () => runner });
  const state = fakeCellState();
  const cell = new UserCell(state, env);
  await state.ready();
  await cell.seed('reader', USER, null);
  const identity = { [SUBJECT_HEADER]: USER, 'x-prep-display-name': 'Parity', [KIND_HEADER]: 'fake' };
  const send = (path: string, init: RequestInit = {}) => cell.fetch(req(path, { ...init, headers: { ...identity, ...(init.headers as Record<string, string>) } }));
  const registry = new StepRegistry();
  registerWorkflowSteps(registry);
  return {
    runner,
    repos: c.userRepos(state.fake, c.clock),
    post: (path, body = {}) =>
      send(path, { method: 'POST', body: new URLSearchParams(body).toString(), headers: { 'content-type': 'application/x-www-form-urlencoded' } }),
    json: (path, body) => send(path, { method: 'POST', body: JSON.stringify(body), headers: { 'content-type': 'application/json' } }),
    prompt: async (name, input) => {
      const agent = new PromptSpy();
      const ctx: LlmStepContext = {
        site: 'job',
        jobId: 'job-1',
        kind: 'Transform',
        owner: USER,
        stepKey: 'job-1-step-0',
        name,
        idx: 0,
        item: 0,
        input,
        outputs: {},
        itemInput: null,
        clock: c.clock,
        agent,
        signal: AbortSignal.timeout(5_000),
      };
      await registry.get(name)(ctx).catch(() => undefined);
      const prompt = agent.prompts[0];
      if (prompt === undefined) throw new Error(`${name} never called the model`);
      return prompt;
    },
  };
}

describe('a Transform start carries the picture its compute step is shown', () => {
  let h: Harness;
  beforeEach(async () => {
    h = await harness();
  });

  it('deck scope sends the deck as the Go loader read it: unsuspended, by id', async () => {
    expect((await h.post(`/deck/${DECK}/transform`, { prompt: 'split the raft cards' })).status).toBe(303);
    const input = h.runner.starts[0]!.input;
    const deckId = h.repos.decks.findId(DECK)!;
    const expected = h.repos.questions
      .listInDeck(deckId)
      .filter((c) => !c.suspended)
      .map((c) => c.id)
      .sort((a, b) => a - b);
    expect(expected.length).toBeGreaterThan(0);
    expect((input['cards'] as { question_id: number }[]).map((c) => c.question_id)).toEqual(expected);
    expect(input['decks']).toEqual([]);

    const prompt = await h.prompt('compute', input);
    for (const q of h.repos.questions.listInDeck(deckId).filter((c) => !c.suspended)) expect(prompt).toContain(q.prompt);
  });

  it('deck scope threads the deck context prompt the model reads the cards against', async () => {
    await h.post('/deck/world-history/transform', { prompt: 'tighten them' });
    const input = h.runner.starts[0]!.input;
    const context = h.repos.decks.getContextPrompt('world-history')!;
    expect(context).not.toBe('');
    expect(input['deckContextPrompt']).toBe(context);
    expect(await h.prompt('compute', input)).toContain(`**Deck overall context** (what this deck is about per the owner):\n${context}`);
  });

  it('card scope sends the one card the user named', async () => {
    const qid = h.repos.questions.listInDeck(h.repos.decks.findId(DECK)!)[0]!.id;
    expect((await h.post(`/question/${qid}/improve`, { prompt: 'make it sharper' })).status).toBe(303);
    const input = h.runner.starts[0]!.input;
    expect((input['cards'] as { question_id: number }[]).map((c) => c.question_id)).toEqual([qid]);
    expect(await h.prompt('compute', input)).toContain(h.repos.questions.get(qid)!.prompt);
  });

  it('reorganize sends every deck by name, each with its own cards', async () => {
    expect((await h.post('/reorganize', { prompt: 'merge the small decks' })).status).toBe(303);
    const input = h.runner.starts[0]!.input;
    const decks = input['decks'] as { name: string; cards: unknown[] }[];
    expect(decks.map((d) => d.name)).toEqual([...h.repos.decks.listSummaries().map((d) => d.name)].sort());
    expect(decks.some((d) => d.cards.length > 0)).toBe(true);
    expect(input['cards']).toEqual([]);

    const prompt = await h.prompt('compute', input);
    for (const d of decks) expect(prompt).toContain(`"name": "${d.name}"`);
  });
});

describe('a GradeAnswer start carries the question its grade step is shown', () => {
  it('sends the four columns the Go activity loaded', async () => {
    const h = await harness();
    const deckId = h.repos.decks.findId(DECK)!;
    const q = h.repos.questions.listInDeck(deckId).find((c) => c.type === 'short' || c.type === 'code')!;
    const res = await h.json(`/api/study/decks/${DECK}/submit`, { question_id: q.id, answer: 'a guess', version: null });
    expect(res.status).toBe(200);
    const input = h.runner.starts[0]!.input;
    expect(input['card']).toEqual({ type: q.type, prompt: q.prompt, answer: q.answer, rubric: q.rubric ?? '' });

    const prompt = await h.prompt('grade', input);
    expect(prompt).toContain(q.prompt);
    expect(prompt).toContain(q.answer);
  });
});
