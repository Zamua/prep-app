// Transform end to end: the scope that auto-applies, the two that gate, and
// the apply itself, which is the only step in phase 4 that rewrites rows the
// user already had.
import { describe, expect, it } from 'vitest';
import { applyStep, BadTransformInput, buildTransformPrompt, goJson, mergeModification, transformJobInput, type TransformCard } from '../../../app/jobs/transform.js';
import { coerceTransformPlan } from '../../../domain/jobs/progress.js';
import { workflowHarness, writeCtx } from './harness.js';
import { cell } from '../../repos/setup.js';

const CARD_ID = 'transform-card-1-0000000010';
const DECK_ID = 'transform-deck-1-0000000011';
const REORG_ID = 'transform-reorganize-0-0000000012';

const snapshotOf = (repos: ReturnType<typeof cell>['repos'], deckId: number): TransformCard[] =>
  repos.questions.listInDeck(deckId).map((c) => ({ question_id: c.id, type: c.type, prompt: c.prompt, answer: c.answer, rubric: c.rubric ?? '' }));

describe('Transform: card scope', () => {
  it('applies the rewrite with no gate in the graph', async () => {
    const h = workflowHarness(() => JSON.stringify({ modifications: [{ question_id: 1, type: 'short', prompt: 'Capital of France?', answer: 'Paris' }], notes: 'tightened it' }));
    const repos = h.repos();
    const deck = repos.decks.create('capitals');
    const qid = repos.questions.add(deck, { type: 'short', prompt: 'france capital', answer: 'paris' });

    await h.start('Transform', CARD_ID, {
      scope: 'card',
      targetId: qid,
      prompt: 'make it a proper question',
      deckName: 'capitals',
      deckContextPrompt: 'european geography',
      cards: snapshotOf(repos, deck),
    });
    expect(await h.run(CARD_ID)).toBe('terminal');

    expect(h.statuses(CARD_ID)).toEqual(['computing', 'applying', 'done']);
    expect(h.progress(CARD_ID)?.progress['scope']).toBe('card');
    expect(repos.questions.get(qid)).toMatchObject({ prompt: 'Capital of France?', answer: 'Paris' });
    expect((h.progress(CARD_ID)?.progress['result'] as { modified_ids: number[] }).modified_ids).toEqual([qid]);
    expect(h.agent.prompts[0]).toContain('**Deck overall context** (what this deck is about per the owner):\neuropean geography');
    expect(h.agent.prompts[0]).toContain('You are improving a single flashcard');
  });
});

describe('Transform: the deck-scope gate', () => {
  const plan = (qid: number) =>
    JSON.stringify({
      modifications: [{ question_id: qid, prompt: 'Capital of France?' }],
      additions: [{ type: 'short', topic: 'geo', prompt: 'Capital of Japan?', answer: 'Tokyo' }],
      deletions: [qid + 1],
      notes: 'one of each',
    });

  const setup = async (h: ReturnType<typeof workflowHarness>) => {
    const repos = h.repos();
    const deck = repos.decks.create('capitals');
    const keep = repos.questions.add(deck, { type: 'short', prompt: 'france capital', answer: 'Paris' });
    const drop = repos.questions.add(deck, { type: 'short', prompt: 'stale', answer: 'x' });
    await h.start('Transform', DECK_ID, { scope: 'deck', targetId: deck, prompt: 'clean it up', deckName: 'capitals', cards: snapshotOf(repos, deck) });
    return { repos, deck, keep, drop };
  };

  it('waits for the apply signal, then writes every part of the plan', async () => {
    let qid = 0;
    const h = workflowHarness(() => plan(qid));
    const { repos, deck, keep, drop } = await setup(h);
    qid = keep;
    expect(await h.run(DECK_ID)).toBe('gated');
    expect(h.progress(DECK_ID)?.status).toBe('awaiting_apply');
    // Nothing is written while the user is still reading the plan.
    expect(repos.questions.get(drop)).not.toBeNull();

    await h.signal(DECK_ID, 'apply');
    expect(await h.run(DECK_ID)).toBe('terminal');

    // The transient the signal wrote and the node's own status are both
    // `applying`, so the poller sees the same literal twice.
    expect(h.statuses(DECK_ID)).toEqual(['computing', 'awaiting_apply', 'applying', 'applying', 'done']);
    expect(repos.questions.get(keep)?.prompt).toBe('Capital of France?');
    expect(repos.questions.get(drop)).toBeNull();
    const added = repos.questions.listInDeck(deck).filter((c) => c.prompt === 'Capital of Japan?');
    expect(added.length).toBe(1);
    expect(h.progress(DECK_ID)?.progress['result']).toMatchObject({ modified_ids: [keep], deleted_ids: [drop], added_ids: [added[0]!.id] });
  });

  it('rejects through the transient status and leaves the deck alone', async () => {
    let qid = 0;
    const h = workflowHarness(() => plan(qid));
    const { repos, keep, drop } = await setup(h);
    qid = keep;
    expect(await h.run(DECK_ID)).toBe('gated');
    await h.signal(DECK_ID, 'reject');
    expect(await h.run(DECK_ID)).toBe('terminal');

    expect(h.statuses(DECK_ID)).toEqual(['computing', 'awaiting_apply', 'rejecting', 'rejected']);
    expect(repos.questions.get(keep)?.prompt).toBe('france capital');
    expect(repos.questions.get(drop)).not.toBeNull();
  });

  it('treats the one-hour deadline as the reject it stands in for', async () => {
    let qid = 0;
    const h = workflowHarness(() => plan(qid));
    const { keep } = await setup(h);
    qid = keep;
    expect(await h.run(DECK_ID)).toBe('gated');
    const gatedAt = h.ledger(DECK_ID).outbox.find((r) => r['status'] === 'awaiting_apply')!['at'];
    expect(Date.parse(String(h.job(DECK_ID)['deadline_at'])) - Date.parse(String(gatedAt))).toBe(3_600_000);

    expect(await h.tick(DECK_ID)).toBe(true);
    expect(await h.run(DECK_ID)).toBe('terminal');
    expect(h.progress(DECK_ID)?.status).toBe('rejected');
  });
});

describe('Transform: reorganize', () => {
  it('creates, renames, moves, adds, deletes, and keeps the trivia queue honest', async () => {
    const state: { plan: string } = { plan: '{}' };
    const h = workflowHarness(() => state.plan);
    const repos = h.repos();
    const alpha = repos.decks.create('alpha');
    const beta = repos.decks.createTrivia('beta', { topic: 'b', intervalMinutes: 30 });
    const moving = repos.questions.add(alpha, { type: 'short', prompt: 'moving', answer: 'm' });
    const doomed = repos.questions.add(alpha, { type: 'short', prompt: 'doomed', answer: 'd' });
    state.plan = JSON.stringify({
      new_decks: [{ name: 'gamma', deck_type: 'trivia', topic: 'g', interval_minutes: 15 }],
      deck_renames: [{ deck_id: alpha, new_name: 'alpha-2' }],
      additions: [{ dest_deck: 'gamma', type: 'short', prompt: 'fresh', answer: 'f' }],
      deletions: [doomed],
      card_moves: [{ question_id: moving, dest_deck: 'gamma' }],
      deck_deletions: [beta],
      notes: 'split it up',
    });

    await h.start('Transform', REORG_ID, {
      scope: 'reorganize',
      targetId: 0,
      prompt: 'split alpha',
      deckName: null,
      decks: [{ id: alpha, name: 'alpha', deck_type: 'srs', cards: snapshotOf(repos, alpha) }],
    });
    expect(await h.run(REORG_ID)).toBe('gated');
    await h.signal(REORG_ID, 'apply');
    expect(await h.run(REORG_ID)).toBe('terminal');

    const gamma = repos.decks.findId('gamma')!;
    expect(repos.decks.getType(gamma)).toBe('trivia');
    expect(repos.decks.getMeta(gamma).interval_minutes).toBe(15);
    expect(repos.decks.findName(alpha)).toBe('alpha-2');
    expect(repos.decks.findId('beta')).toBeNull();
    expect(repos.questions.get(doomed)).toBeNull();
    expect(repos.questions.get(moving)?.deck_id).toBe(gamma);
    // Additions run before moves, so the new card takes the first slot.
    expect(repos.trivia.listQueueForDeck(gamma).map((e) => [e.prompt, e.queue_position])).toEqual([
      ['fresh', 1],
      ['moving', 2],
    ]);
    expect(h.progress(REORG_ID)?.progress['result']).toMatchObject({ created_deck_ids: [gamma], renamed_deck_ids: [alpha], moved_card_ids: [moving], deleted_deck_ids: [beta] });
  });

  it('never clobbers a deck whose name the model reused', async () => {
    const c = cell();
    const taken = c.repos.decks.create('taken');
    const plan = coerceTransformPlan({ new_decks: [{ name: 'taken', deck_type: 'srs' }], deck_renames: [{ deck_id: taken, new_name: 'taken' }] }, 'reorganize');
    const out = await applyStep(
      writeCtx({ repos: c.repos, clock: c.clock, stepKey: 'k', name: 'apply', input: { scope: 'reorganize', targetId: 0, prompt: 'p' }, outputs: { compute: plan } }),
    );
    expect(out.value).toMatchObject({ created_deck_ids: [], renamed_deck_ids: [] });
    expect(c.repos.decks.listSummaries().map((d) => d.name)).toEqual(['taken']);
  });
});

describe('the apply step', () => {
  it('adds each card once however often the step is redelivered', async () => {
    const c = cell();
    const deck = c.repos.decks.create('capitals');
    const plan = coerceTransformPlan({ additions: [{ type: 'short', prompt: 'one', answer: '1' }, { type: 'short', prompt: 'two', answer: '2' }] }, 'deck');
    const ctx = writeCtx({
      repos: c.repos,
      clock: c.clock,
      stepKey: `${DECK_ID}-apply-0`,
      name: 'apply',
      input: { scope: 'deck', targetId: deck, prompt: 'add two' },
      outputs: { compute: plan },
    });

    const first = (await applyStep(ctx)).value as { added_ids: number[] };
    const second = (await applyStep(ctx)).value as { added_ids: number[] };
    expect(second.added_ids).toEqual(first.added_ids);
    expect(c.repos.questions.listInDeck(deck).map((q) => q.prompt)).toEqual(['one', 'two']);
  });

  it('keeps the stored value for a field the model left empty', () => {
    const existing = { id: 7, type: 'code', prompt: 'old prompt', answer: 'old answer', rubric: 'old rubric' } as never;
    const merged = mergeModification(existing, { question_id: 7, type: '', prompt: '', answer: '', rubric: '', topic: 'geo' });
    expect(merged).toMatchObject({ type: 'code', prompt: 'old prompt', answer: 'old answer', topic: 'geo', rubric: null });
  });
});

describe('the transform prompts', () => {
  it('refuses a scope the workflow does not have, and an empty request', () => {
    expect(() => transformJobInput({ scope: 'everything', prompt: 'p' })).toThrow(BadTransformInput);
    expect(() => transformJobInput({ scope: 'deck', prompt: '   ' })).toThrow(BadTransformInput);
  });

  it('shows an empty deck as an empty list rather than a null', () => {
    const prompt = buildTransformPrompt(transformJobInput({ scope: 'deck', targetId: 1, prompt: 'fill it' }));
    expect(prompt).toContain('```json\n[]\n```');
    expect(prompt).not.toContain('**Deck overall context**');
  });

  it('escapes the runes Go escapes, so a card holding markup is the same bytes', () => {
    expect(goJson({ prompt: 'a < b && c > d' })).toBe('{\n  "prompt": "a \\u003c b \\u0026\\u0026 c \\u003e d"\n}');
  });

  it('names the cross-deck operations only in the reorganize prompt', () => {
    const reorg = buildTransformPrompt(transformJobInput({ scope: 'reorganize', targetId: 0, prompt: 'tidy', decks: [] }));
    expect(reorg).toContain('Move cards between decks (card_moves)');
    expect(buildTransformPrompt(transformJobInput({ scope: 'deck', targetId: 1, prompt: 'tidy' }))).not.toContain('card_moves');
  });
});
