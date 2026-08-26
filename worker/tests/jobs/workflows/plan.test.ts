// Plan-first generation end to end: the outline, the gate's four exits, the
// batched expansion, and the insert key that survives a redelivery.
import { describe, expect, it } from 'vitest';
import { buildPlanPrompt, extractJson, parsePlanJson, planInsert, PlanParseError, toNewQuestion } from '../../../app/jobs/plan.js';
import { coerceCard } from '../../../domain/jobs/progress.js';
import { workflowHarness, writeCtx, type Script } from './harness.js';
import { cell } from '../../repos/setup.js';

const ID = 'plan-capitals-0000000001';
const INPUT = { deckId: 1, deckName: 'capitals', prompt: 'the capitals of europe' };

const planJson = (titles: readonly string[]) => JSON.stringify(titles.map((t) => ({ title: t, brief: `about ${t}`, type: 'short', topic: 'geo' })));

const cardJson = (title: string) => JSON.stringify({ type: 'short', topic: 'geo', prompt: `What about ${title}?`, answer: title, rubric: ['names it'] });

/** The expand prompt names its item, so a reply can depend on which one. */
const titleOf = (prompt: string): string => /^ {2}title: (.*)$/m.exec(prompt)?.[1] ?? '';

const script =
  (rounds: readonly (readonly string[])[], failing: readonly string[] = []): Script =>
  (prompt, call) => {
    if (prompt.startsWith('Generate ONE flashcard')) {
      const title = titleOf(prompt);
      return failing.includes(title) ? new Error(`expand ${title} blew up`) : cardJson(title);
    }
    return planJson(rounds[Math.min(call, rounds.length - 1)] ?? []);
  };

const seedDeck = (h: ReturnType<typeof workflowHarness>) => h.repos().decks.create('capitals');

describe('PlanGenerate: the accepted path', () => {
  it('plans, gates, expands in item order and inserts one card per expansion', async () => {
    const h = workflowHarness(script([['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot']]));
    seedDeck(h);
    await h.start('PlanGenerate', ID, INPUT);
    expect(await h.run(ID)).toBe('gated');
    expect(h.progress(ID)?.status).toBe('awaiting_feedback');
    expect(h.progress(ID)?.progress['round']).toBe(1);
    expect((h.progress(ID)?.progress['plan'] as unknown[]).length).toBe(6);

    await h.signal(ID, 'accept');
    expect(await h.run(ID)).toBe('terminal');

    expect(h.statuses(ID)).toEqual(['planning', 'awaiting_feedback', 'accepting', 'generating', 'applying', 'done']);
    expect(h.stepKeys(ID, 'expand')).toEqual([0, 1, 2, 3, 4, 5].map((i) => `${ID}-expand-${i}`));
    expect(h.stepKeys(ID, 'insert')).toEqual([0, 1, 2, 3, 4, 5].map((i) => `${ID}-insert-${i}`));

    const repos = h.repos();
    const deck = repos.decks.findId('capitals')!;
    expect(repos.questions.listInDeck(deck).map((c) => c.answer)).toEqual(['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot']);
    const done = h.progress(ID)!;
    expect(done.status).toBe('done');
    expect(done.progress['generated_count']).toBe(6);
    expect(done.progress['total']).toBe(6);
    expect((done.progress['result'] as { added_ids: number[] }).added_ids.length).toBe(6);
  });

  it('materialises one row per item and runs them in batch order, four at a time', async () => {
    const h = workflowHarness(script([['a1', 'a2', 'a3', 'a4', 'b1', 'b2']]));
    seedDeck(h);
    await h.start('PlanGenerate', ID, INPUT);
    await h.run(ID);
    await h.signal(ID, 'accept');

    // `runnableRows` is pinned over synthetic rows in the schedule tests; what
    // a real run adds is that the six items land in two groups of four, and no
    // group-1 row leaves `pending` while a group-0 row still is.
    const snapshots: string[][] = [];
    for (let i = 0; i < 40; i++) {
      const job = h.job(ID);
      if (job['state'] === 'terminal' && h.jobStorage(ID).alarmAt === null) break;
      if (!(await h.tick(ID))) break;
      snapshots.push(h.stepStatuses(ID, 'expand'));
    }
    expect(snapshots.length).toBeGreaterThan(4);
    for (const statuses of snapshots) {
      if (statuses.length === 0) continue;
      const firstGroupOpen = statuses.slice(0, 4).some((s) => s === 'pending');
      const secondGroupStarted = statuses.slice(4).some((s) => s !== 'pending');
      expect(firstGroupOpen && secondGroupStarted, statuses.join(',')).toBe(false);
    }
  });

  it("skips a failed expansion rather than its siblings, and counts what landed", async () => {
    const h = workflowHarness(script([['alpha', 'bravo', 'charlie']], ['bravo']));
    seedDeck(h);
    await h.start('PlanGenerate', ID, INPUT);
    await h.run(ID);
    await h.signal(ID, 'accept');
    expect(await h.run(ID)).toBe('terminal');

    expect(h.stepStatuses(ID, 'expand')).toEqual(['done', 'skipped', 'done']);
    expect(h.stepKeys(ID, 'insert')).toEqual([`${ID}-insert-0`, `${ID}-insert-1`]);
    const done = h.progress(ID)!;
    expect(done.status).toBe('done');
    expect(done.progress['generated_count']).toBe(2);
    expect(done.progress['total']).toBe(3);
  });

  it('fails the job when every expansion failed', async () => {
    const h = workflowHarness(script([['alpha', 'bravo']], ['alpha', 'bravo']));
    seedDeck(h);
    await h.start('PlanGenerate', ID, INPUT);
    await h.run(ID);
    await h.signal(ID, 'accept');
    expect(await h.run(ID)).toBe('terminal');
    expect(h.progress(ID)?.status).toBe('failed');
    expect(h.progress(ID)?.progress['error']).toBe('every card expansion failed');
  });
});

describe('PlanGenerate: the gate', () => {
  it('rejects through the transient status and writes nothing', async () => {
    const h = workflowHarness(script([['alpha']]));
    seedDeck(h);
    await h.start('PlanGenerate', ID, INPUT);
    await h.run(ID);
    await h.signal(ID, 'reject');
    expect(await h.run(ID)).toBe('terminal');
    expect(h.statuses(ID)).toEqual(['planning', 'awaiting_feedback', 'rejecting', 'rejected']);
    expect(h.repos().questions.listInDeck(h.repos().decks.findId('capitals')!)).toEqual([]);
  });

  it('re-plans on feedback, bumps the round and keeps the one deadline', async () => {
    const h = workflowHarness(script([['alpha', 'bravo'], ['charlie']]));
    seedDeck(h);
    await h.start('PlanGenerate', ID, INPUT);
    await h.run(ID);
    const deadline = String(h.job(ID)['deadline_at']);
    const gatedAt = h.ledger(ID).outbox.find((r) => r['status'] === 'awaiting_feedback')!['at'];
    expect(Date.parse(deadline) - Date.parse(String(gatedAt))).toBe(24 * 3_600_000);

    await h.signal(ID, 'feedback', { feedback: 'fewer cards please' });
    expect(await h.run(ID)).toBe('gated');
    expect(h.statuses(ID)).toEqual(['planning', 'awaiting_feedback', 'replanning', 'awaiting_feedback']);
    expect(h.progress(ID)?.progress['round']).toBe(2);
    expect((h.progress(ID)?.progress['plan'] as { title: string }[]).map((p) => p.title)).toEqual(['charlie']);
    // The Go single-timer rule: a re-run returns to the gate it already had.
    expect(h.job(ID)['deadline_at']).toBe(deadline);
    // The second plan call is the refine prompt, carrying the prior outline
    // and the words the user typed, which is the whole point of the round.
    expect(h.agent.prompts[1]).toContain('Refine the card plan for deck "capitals"');
    expect(h.agent.prompts[1]).toContain('1. [short] alpha — about alpha');
    expect(h.agent.prompts[1]).toContain('The user wants this changed:\nfewer cards please');

    await h.signal(ID, 'accept');
    expect(await h.run(ID)).toBe('terminal');
    expect(h.stepKeys(ID, 'expand')).toEqual([`${ID}-expand-0`]);
    expect(h.repos().questions.listInDeck(h.repos().decks.findId('capitals')!).map((c) => c.answer)).toEqual(['charlie']);
  });

  it('keeps the prior plan when the re-plan fails, and stays on the gate', async () => {
    const h = workflowHarness((_prompt, call) => (call === 0 ? planJson(['alpha']) : new Error('upstream is busy')));
    seedDeck(h);
    await h.start('PlanGenerate', ID, INPUT);
    await h.run(ID);
    await h.signal(ID, 'feedback', 'try again');
    expect(await h.run(ID)).toBe('gated');
    // A bare string is a payload too: the signal carries it either way.
    expect(h.agent.prompts[1]).toContain('The user wants this changed:\ntry again');

    const at = h.progress(ID)!;
    expect(at.status).toBe('awaiting_feedback');
    expect(at.progress['error']).toBe('replan failed: upstream is busy');
    expect((at.progress['plan'] as { title: string }[]).map((p) => p.title)).toEqual(['alpha']);
    expect(at.progress['round']).toBe(1);
  });

  it('treats the deadline as the reject it stands in for', async () => {
    const h = workflowHarness(script([['alpha']]));
    seedDeck(h);
    const started = h.clock.now().getTime();
    await h.start('PlanGenerate', ID, INPUT);
    expect(await h.run(ID)).toBe('gated');

    // The gate's only wake is its deadline, so one tick spends the whole 24h.
    expect(await h.tick(ID)).toBe(true);
    expect(h.clock.now().getTime() - started).toBeGreaterThanOrEqual(24 * 3_600_000);
    expect(await h.run(ID)).toBe('terminal');
    expect(h.statuses(ID)).toEqual(['planning', 'awaiting_feedback', 'rejecting', 'rejected']);
  });
});

describe('the plan prompt and parser', () => {
  it('asks for a sized outline, and caps the plan the model returned', async () => {
    const h = workflowHarness(script([['a', 'b', 'c', 'd']]));
    seedDeck(h);
    await h.start('PlanGenerate', ID, { ...INPUT, maxCards: 2 });
    await h.run(ID);
    expect(h.agent.prompts[0]).toContain('Create at most 2 cards.');
    expect((h.progress(ID)?.progress['plan'] as unknown[]).length).toBe(2);
  });

  it('lets the description size the plan when nothing caps it', () => {
    const prompt = buildPlanPrompt({ deckId: 1, deckName: 'd', prompt: 'p', maxCards: 0, priorPlan: [], feedback: '' });
    expect(prompt).toContain('Most\ndecks want 5-15 cards');
    expect(prompt).not.toContain('Create at most');
  });

  it('reads the array out of a fenced block, a chatty preamble, or a wrapper object', () => {
    const items = [{ title: 't', brief: 'b', type: 'short' }];
    expect(parsePlanJson('```json\n' + JSON.stringify(items) + '\n```')).toEqual(items);
    expect(parsePlanJson('Here is the plan: ' + JSON.stringify(items))).toEqual(items);
    expect(parsePlanJson(JSON.stringify({ plan: items }))).toEqual(items);
    expect(extractJson('prose {"a": 1} trailing')).toBe('{"a": 1}');
  });

  it('refuses an empty plan and unparseable output', () => {
    expect(() => parsePlanJson('[]')).toThrow(PlanParseError);
    expect(() => parsePlanJson('sorry, no')).toThrow(PlanParseError);
  });
});

describe('the insert step', () => {
  it('writes one card under its key however often it is redelivered', async () => {
    const c = cell();
    const deck = c.repos.decks.create('capitals');
    const card = coerceCard({ type: 'short', prompt: 'Q', answer: 'A', rubric: 'r' });
    const ctx = writeCtx({
      repos: c.repos,
      clock: c.clock,
      stepKey: `${ID}-insert-0`,
      name: 'insert',
      kind: 'PlanGenerate',
      input: { deckId: deck, deckName: 'capitals' },
      itemInput: card,
    });

    const first = await planInsert(ctx);
    const second = await planInsert(ctx);
    expect(second.value).toBe(first.value);
    expect(c.repos.questions.listInDeck(deck).length).toBe(1);
    expect(c.repos.idempotency.findQuestion(`${ID}-insert-0`)).toBe(first.value);
  });

  it('gives a code card the Go default language and drops an empty rubric', () => {
    expect(toNewQuestion(coerceCard({ type: 'code', prompt: 'p', answer: 'a' }))).toMatchObject({ language: 'go', rubric: null });
    expect(toNewQuestion(coerceCard({ type: 'short', prompt: 'p', answer: ['x', 'y'], rubric: ['one', 'two'] }))).toMatchObject({
      answer: ['x', 'y'],
      rubric: '- one\n- two',
    });
  });
});
