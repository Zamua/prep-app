// The phase-4 routes through a real user cell over the SqlStorage fake, with
// a scripted runner standing in for the JobCell. The rendered HTML is the
// real one, because the polling contract (a non-terminal fragment carries
// `hx-trigger`, a terminal one carries none) is an attribute, not a context
// key, and it is what stops the browser hammering a finished job.
import { beforeEach, describe, expect, it } from 'vitest';
import type { JobInputs, JobKind, JobStatus, Renderer, UserRepos, WorkflowRunner } from '../../app/ports.js';
import { RunnerUnavailable } from '../../app/ports.js';
import { UserCell } from '../../runtime/cells/UserCell.js';
import { KIND_HEADER, SUBJECT_HEADER } from '../../runtime/cells/router.js';
import { composeWith } from '../../runtime/compose.js';
import { fakeCellState } from '../fakes/sqlStorage.js';
import { fakeEnv, req, type Rendered } from '../helpers.js';

const USER = 'seed@example.com';
const PLAN_WID = 'plan-world-capitals-0123456789';
const TRIVIA_WID = 'trivia-world-history-0123456789';

interface Started {
  kind: JobKind;
  input: Record<string, unknown>;
}

/** The JobCell, scripted: what a start returns, what a status reads and what
 * a signal answers, without a cell or an alarm anywhere. */
class ScriptedRunner implements WorkflowRunner {
  readonly starts: Started[] = [];
  readonly signals: { id: string; name: string; payload?: unknown }[] = [];
  readonly statuses = new Map<string, JobStatus>();
  /** Keyed by event name: what the post-signal status comes back as. */
  readonly replies = new Map<string, JobStatus | null>();
  workflowId = 'transform-deck-2-0123456789';
  startError: Error | null = null;
  signalError: Error | null = null;

  async start<K extends JobKind>(kind: K, input: JobInputs[K]): Promise<{ workflowId: string }> {
    this.starts.push({ kind, input: input as unknown as Record<string, unknown> });
    if (this.startError) throw this.startError;
    return { workflowId: this.workflowId };
  }

  async signal(id: string, event: { name: string; payload?: unknown }): Promise<JobStatus | null> {
    this.signals.push({ id, name: event.name, payload: event.payload });
    if (this.signalError) throw this.signalError;
    return this.replies.get(event.name) ?? null;
  }

  async status(id: string): Promise<JobStatus | null> {
    return this.statuses.get(id) ?? null;
  }

  async terminate(): Promise<void> {}
}

function recording(inner: Renderer): Renderer & { calls: Rendered[] } {
  const calls: Rendered[] = [];
  return {
    calls,
    render(template, context) {
      calls.push({ template, context });
      return inner.render(template, context);
    },
  };
}

interface Harness {
  runner: ScriptedRunner;
  repos: UserRepos;
  rendered(): Rendered;
  get(path: string): Promise<Response>;
  post(path: string, body?: Record<string, string>): Promise<Response>;
}

/** No shared tier configured, so nothing but a stored key can fund a call. */
const NO_TIER = { PREP_FREE_INFERENCE_BASE_URL: '', PREP_FREE_INFERENCE_API_KEY: '', PREP_FREE_INFERENCE_MODEL: '' };

async function harness(profile = 'reader', overrides: Record<string, string> = {}): Promise<Harness> {
  const env = fakeEnv(overrides);
  const runner = new ScriptedRunner();
  const base = composeWith(env, {});
  const renderer = recording(base.renderer);
  const c = composeWith(env, { renderer, runner: () => runner });
  const state = fakeCellState();
  const cell = new UserCell(state, env);
  await cell.seed(profile, USER, null);
  renderer.calls.length = 0;
  const identity = { [SUBJECT_HEADER]: USER, 'x-prep-display-name': 'Seed', [KIND_HEADER]: 'fake' };
  const send = (path: string, init: RequestInit = {}) => cell.fetch(req(path, { ...init, headers: { ...identity, ...(init.headers as Record<string, string>) } }));
  return {
    runner,
    repos: c.userRepos(state.fake, c.clock),
    rendered: () => {
      const call = renderer.calls[renderer.calls.length - 1];
      if (!call) throw new Error('nothing was rendered');
      return call;
    },
    get: (path) => send(path),
    post: (path, body = {}) =>
      send(path, {
        method: 'POST',
        body: new URLSearchParams(body).toString(),
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
      }),
  };
}

const status = (s: string, progress: Record<string, unknown> = {}): JobStatus => ({ status: s, progress });

describe('the plan surface', () => {
  let h: Harness;
  beforeEach(async () => {
    h = await harness();
  });

  it('renders the polling page with the flat progress the partial reads', async () => {
    h.runner.statuses.set(PLAN_WID, status('planning', { plan: null, round: 1, total: 4 }));
    const res = await h.get(`/plan/${PLAN_WID}`);
    expect(res.status).toBe(200);
    const { template, context } = h.rendered();
    expect(template).toBe('plan.html');
    expect(context['wid']).toBe(PLAN_WID);
    expect(context['deck_name']).toBe('world-capitals');
    expect(context['progress']).toEqual({ plan: null, round: 1, total: 4, status: 'planning' });
  });

  it('sends a page with nothing left to poll back to the deck', async () => {
    const res = await h.get(`/plan/${PLAN_WID}`);
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe('/deck/world-capitals');
  });

  it('refuses a malformed id and 404s one naming a deck this cell does not have', async () => {
    expect((await h.get('/plan/nonsense')).status).toBe(400);
    expect((await h.get('/plan/plan-someone-elses-0123456789')).status).toBe(404);
  });

  it('answers gone as JSON when no row backs the id', async () => {
    const res = await h.get(`/plan/${PLAN_WID}/status`);
    expect(await res.json()).toEqual({ status: 'gone' });
  });

  it('answers the live progress as JSON', async () => {
    h.runner.statuses.set(PLAN_WID, status('generating', { total: 4, generated_count: 2 }));
    expect(await (await h.get(`/plan/${PLAN_WID}/status`)).json()).toEqual({ total: 4, generated_count: 2, status: 'generating' });
  });

  it('signals accept and renders the transient status the signal returned', async () => {
    h.runner.statuses.set(PLAN_WID, status('awaiting_feedback', { plan: [], round: 1 }));
    h.runner.replies.set('accept', status('accepting', { plan: [], round: 1 }));
    const res = await h.post(`/plan/${PLAN_WID}/accept`);
    expect(res.status).toBe(200);
    expect(h.runner.signals).toEqual([{ id: PLAN_WID, name: 'accept', payload: undefined }]);
    const { template, context } = h.rendered();
    expect(template).toBe('partials/plan_progress.html');
    expect((context['progress'] as Record<string, unknown>)['status']).toBe('accepting');
  });

  it('signals reject and feedback, carrying the text with the feedback event', async () => {
    h.runner.replies.set('reject', status('rejecting'));
    h.runner.replies.set('feedback', status('replanning', { round: 2 }));
    await h.post(`/plan/${PLAN_WID}/reject`);
    await h.post(`/plan/${PLAN_WID}/feedback`, { feedback: '  more on quorums  ' });
    expect(h.runner.signals.map((s) => [s.name, s.payload])).toEqual([
      ['reject', undefined],
      ['feedback', 'more on quorums'],
    ]);
  });

  it('refuses feedback that is only whitespace', async () => {
    expect((await h.post(`/plan/${PLAN_WID}/feedback`, { feedback: '   ' })).status).toBe(400);
    expect(h.runner.signals).toEqual([]);
  });
});

describe('the plan polling contract', () => {
  let h: Harness;
  beforeEach(async () => {
    h = await harness();
  });

  const fragment = async (s: string): Promise<string> => {
    h.runner.statuses.set(PLAN_WID, status(s, { plan: [], total: 0, round: 1 }));
    return (await h.get(`/plan/${PLAN_WID}/fragment`)).text();
  };

  it.each(['planning', 'replanning', 'accepting', 'generating', 'applying', 'rejecting'])('keeps polling on %s', async (s) => {
    expect(await fragment(s)).toContain('hx-trigger="every 2s"');
  });

  it.each(['awaiting_feedback', 'done', 'rejected', 'failed', 'gone'])('stops polling on %s', async (s) => {
    expect(await fragment(s)).not.toContain('hx-trigger');
  });

  it('stops polling when the row is gone entirely', async () => {
    expect(await (await h.get(`/plan/${PLAN_WID}/fragment`)).text()).not.toContain('hx-trigger');
  });
});

describe('the transform surface', () => {
  let h: Harness;
  let deckId: number;
  let wid: string;
  beforeEach(async () => {
    h = await harness();
    deckId = h.repos.decks.findId('distributed-systems')!;
    wid = `transform-deck-${deckId}-0123456789`;
  });

  it('builds the diff preview from the live rows the plan names', async () => {
    const qid = h.repos.questions.listInDeck(deckId)[0]!.id;
    h.runner.statuses.set(
      wid,
      status('awaiting_apply', { plan: { modifications: [{ question_id: qid, prompt: 'A sharper prompt' }], deletions: [qid], card_moves: [] } }),
    );
    const res = await h.get(`/transform/${wid}`);
    expect(res.status).toBe(200);
    const { template, context } = h.rendered();
    expect(template).toBe('transform.html');
    expect(context['scope']).toBe('deck');
    expect(context['target_id']).toBe(deckId);
    expect(context['deck_name']).toBe('distributed-systems');
    const diffs = context['modification_diffs'] as { question_id: number; deck_name: string; old: Record<string, string>; new: Record<string, string> }[];
    expect(diffs).toHaveLength(1);
    expect(diffs[0]!.deck_name).toBe('distributed-systems');
    expect(diffs[0]!.new['prompt']).toBe('A sharper prompt');
    // Every other field falls through to the live value, so the template can
    // walk one key set and align the two columns.
    expect(diffs[0]!.new['answer']).toBe(diffs[0]!.old['answer']);
    expect(context['deletion_decks']).toEqual({ [String(qid)]: 'distributed-systems' });
  });

  it('renders gone when nothing backs the id', async () => {
    await h.get(`/transform/${wid}`);
    expect((h.rendered().context['progress'] as Record<string, unknown>)['status']).toBe('gone');
  });

  it('404s a scope target this cell does not own and 400s a bad scope', async () => {
    expect((await h.get('/transform/transform-deck-999999-0123456789')).status).toBe(404);
    expect((await h.get('/transform/transform-card-999999-0123456789')).status).toBe(404);
    expect((await h.get('/transform/transform-sideways-1-0123456789')).status).toBe(400);
  });

  it('lets a reorganize id through on the caller being the caller', async () => {
    const res = await h.get('/transform/transform-reorganize-0-0123456789');
    expect(res.status).toBe(200);
    expect(h.rendered().context['deck_name']).toBe('');
  });

  it('answers status as the progress and desc pair the client expects', async () => {
    h.runner.statuses.set(wid, status('computing', { scope: 'deck' }));
    expect(await (await h.get(`/transform/${wid}/status`)).json()).toEqual({ progress: { scope: 'deck', status: 'computing' }, desc: {} });
    const empty = await harness();
    expect(await (await empty.get(`/transform/${wid}/status`)).json()).toEqual({ progress: null, desc: {} });
  });

  it('signals apply and reject, rendering the status the signal produced', async () => {
    h.runner.replies.set('apply', status('applying', { scope: 'deck' }));
    const res = await h.post(`/transform/${wid}/apply`);
    expect(res.status).toBe(200);
    expect(h.rendered().template).toBe('partials/transform_progress.html');
    expect((h.rendered().context['progress'] as Record<string, unknown>)['status']).toBe('applying');
    h.runner.signalError = new Error('cell unreachable');
    expect((await h.post(`/transform/${wid}/reject`)).status).toBe(500);
  });
});

describe('the transform polling contract', () => {
  let h: Harness;
  let wid: string;
  beforeEach(async () => {
    h = await harness();
    wid = `transform-deck-${h.repos.decks.findId('distributed-systems')}-0123456789`;
  });

  const fragment = async (s: string): Promise<string> => {
    h.runner.statuses.set(wid, status(s, { scope: 'deck' }));
    return (await h.get(`/transform/${wid}/fragment`)).text();
  };

  it.each(['computing', 'applying', 'rejecting'])('keeps polling on %s', async (s) => {
    expect(await fragment(s)).toContain('hx-trigger="every 2s"');
  });

  it.each(['awaiting_apply', 'done', 'rejected', 'failed', 'gone'])('stops polling on %s', async (s) => {
    expect(await fragment(s)).not.toContain('hx-trigger');
  });
});

describe('the three transform starts', () => {
  let h: Harness;
  beforeEach(async () => {
    h = await harness();
  });

  it('starts a deck transform against the deck it materialises', async () => {
    const res = await h.post('/deck/distributed-systems/transform', { prompt: '  split the raft cards  ' });
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe('/transform/transform-deck-2-0123456789');
    expect(h.runner.starts).toMatchObject([
      { kind: 'Transform', input: { scope: 'deck', targetId: h.repos.decks.findId('distributed-systems'), prompt: 'split the raft cards', deckName: 'distributed-systems', decks: [] } },
    ]);
    // The snapshot itself is pinned in tests/jobs/startInput.test.ts; here it
    // only has to be there, since an empty one still renders a happy 303.
    expect((h.runner.starts[0]!.input['cards'] as unknown[]).length).toBeGreaterThan(0);
  });

  it('refuses an empty prompt and reports a start that could not book a job', async () => {
    expect((await h.post('/deck/distributed-systems/transform', { prompt: '  ' })).status).toBe(400);
    h.runner.startError = new RunnerUnavailable('Transform workflows are not available on this deploy');
    expect((await h.post('/deck/distributed-systems/transform', { prompt: 'go' })).status).toBe(500);
  });

  it('renders the reorganize form over the decks it has, sorted by slug', async () => {
    expect((await h.get('/reorganize')).status).toBe(200);
    const decks = h.rendered().context['decks'] as { name: string; topic: string }[];
    expect(decks.map((d) => d.name)).toEqual(['distributed-systems', 'scratch', 'world-capitals', 'world-history']);
    expect(decks.find((d) => d.name === 'world-history')!.topic).toBe('World history from antiquity to 1900.');
  });

  it('starts a reorganize with no deck and no target', async () => {
    const res = await h.post('/reorganize', { prompt: 'merge the small decks' });
    expect(res.status).toBe(303);
    expect(h.runner.starts[0]!.input).toMatchObject({ scope: 'reorganize', targetId: 0, prompt: 'merge the small decks', deckName: null, cards: [] });
    expect((h.runner.starts[0]!.input['decks'] as unknown[]).length).toBeGreaterThan(0);
  });

  it('re-renders its own form when nothing funds a reorganize, where the other two throw', async () => {
    const bare = await harness('reader', NO_TIER);
    const res = await bare.post('/reorganize', { prompt: 'merge them' });
    expect(res.status).toBe(403);
    expect(bare.rendered().template).toBe('reorganize.html');
    expect(bare.rendered().context['form']).toEqual({ prompt: 'merge them' });
    expect(bare.runner.starts).toEqual([]);
    expect((await bare.post('/deck/distributed-systems/transform', { prompt: 'go' })).status).toBe(403);
  });
});

describe('trivia batch generation', () => {
  let h: Harness;
  let deckId: number;
  beforeEach(async () => {
    h = await harness();
    deckId = h.repos.decks.findId('world-history')!;
    h.runner.workflowId = TRIVIA_WID;
  });

  it('starts a job on the deck topic and sends the caller to the polling page', async () => {
    const res = await h.post(`/trivia/decks/${deckId}/generate`);
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe(`/trivia/gen/${TRIVIA_WID}`);
    // The generate step holds no repositories, so the tier's cap and the
    // deck's prompts are read here or the model never sees them.
    expect(h.runner.starts).toEqual([
      {
        kind: 'TriviaGenerate',
        input: {
          deckId,
          deckName: 'world-history',
          topic: 'World history from antiquity to 1900.',
          batchSize: 5,
          existing: [
            "Which empire's western half fell in 476?",
            'Who introduced movable-type printing to Europe around 1450?',
            'In which year was Magna Carta sealed?',
          ],
        },
      },
    ]);
  });

  it('404s a deck id this cell does not own', async () => {
    expect((await h.post('/trivia/decks/999999/generate')).status).toBe(404);
    expect((await h.post('/trivia/decks/not-a-number/generate')).status).toBe(404);
  });

  it('reads a job whose row is gone as done, because the cards it wrote are in the deck', async () => {
    const res = await h.get(`/trivia/gen/${TRIVIA_WID}/status`);
    expect(await res.json()).toEqual({ status: 'done', deck_name: 'world-history' });
  });

  it('renders the page and the fragment over the same progress', async () => {
    h.runner.statuses.set(TRIVIA_WID, status('applying', { total: 25, inserted: 4 }));
    expect((await h.get(`/trivia/gen/${TRIVIA_WID}`)).status).toBe(200);
    expect(h.rendered().template).toBe('trivia/generating.html');
    await h.get(`/trivia/gen/${TRIVIA_WID}/fragment`);
    expect(h.rendered().template).toBe('partials/trivia_generating_progress.html');
    expect(h.rendered().context['progress']).toEqual({ total: 25, inserted: 4, status: 'applying', deck_name: 'world-history' });
  });

  it('400s a malformed id and 404s one naming a deck this cell does not have', async () => {
    expect((await h.get('/trivia/gen/nonsense')).status).toBe(400);
    expect((await h.get('/trivia/gen/trivia-not-a-deck-0123456789')).status).toBe(404);
  });
});

describe('the trivia polling contract', () => {
  let h: Harness;
  beforeEach(async () => {
    h = await harness();
  });

  const fragment = async (s: string): Promise<string> => {
    h.runner.statuses.set(TRIVIA_WID, status(s, { total: 25 }));
    return (await h.get(`/trivia/gen/${TRIVIA_WID}/fragment`)).text();
  };

  it.each(['starting', 'generating', 'applying'])('keeps polling on %s', async (s) => {
    expect(await fragment(s)).toContain('hx-trigger="every 1.5s"');
  });

  it.each(['done', 'failed'])('stops polling on %s', async (s) => {
    expect(await fragment(s)).not.toContain('hx-trigger');
  });
});

describe('the masthead badge over real job rows', () => {
  it('shows a job the moment its row lands, sorts action-required first, and drops it once terminal ages out', async () => {
    const h = await harness();
    const deckId = h.repos.decks.findId('distributed-systems')!;
    const wid = `transform-deck-${deckId}-0123456789`;
    h.repos.jobs.register({ workflowId: wid, workflowType: 'transform', deckId, deckName: 'distributed-systems', urlPath: `/transform/${wid}`, initialStatus: 'computing' });

    expect(await (await h.get('/api/active-workflows-badge')).text()).toContain(`/transform/${wid}`);
    const rows = () => (h.rendered().context['workflows'] as { workflow_id: string; display_status: string }[]).map((w) => [w.workflow_id, w.display_status]);

    h.repos.jobs.updateStatus(wid, 'awaiting_apply');
    await h.get('/api/active-workflows-badge');
    expect(rows()[0]).toEqual([wid, 'review']);

    h.repos.jobs.updateStatus(wid, 'done');
    h.repos.jobs.setTerminalAt(wid, '2020-01-01T00:00:00+00:00');
    expect(await (await h.get('/api/active-workflows-badge')).text()).not.toContain(`/transform/${wid}`);
  });
});
