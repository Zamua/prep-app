// Trivia batch generation end to end: the counters the progress bar reads,
// the per-deck prompt dedupe, and the two ways a batch is refused.
import { describe, expect, it } from 'vitest';
import { buildTriviaPrompt, DEFAULT_BATCH_SIZE, NO_CARDS, parseTriviaJson, TriviaParseError } from '../../../app/jobs/trivia.js';
import { workflowHarness } from './harness.js';

const ID = 'trivia-facts-0000000003';

const pairs = (items: readonly (readonly [string, string])[]) => JSON.stringify(items.map(([q, a]) => ({ q, a, e: `because ${a}` })));

const seedTriviaDeck = (h: ReturnType<typeof workflowHarness>) => h.repos().decks.createTrivia('facts', { topic: 'assorted facts', intervalMinutes: 30 });

const input = (extra: Record<string, unknown> = {}) => ({ deckId: 1, deckName: 'facts', topic: 'assorted facts', ...extra });

describe('TriviaGenerate', () => {
  it('inserts each pair into the deck queue and counts what landed', async () => {
    const h = workflowHarness(() =>
      pairs([
        ['Capital of France?', 'Paris'],
        ['Capital of Japan?', 'Tokyo'],
      ]),
    );
    const deck = seedTriviaDeck(h);
    await h.start('TriviaGenerate', ID, input());
    expect(await h.run(ID)).toBe('terminal');

    expect(h.statuses(ID)).toEqual(['generating', 'applying', 'done']);
    expect(h.stepKeys(ID, 'insert')).toEqual([`${ID}-insert-0`, `${ID}-insert-1`]);
    const repos = h.repos();
    expect(repos.questions.listInDeck(deck).map((c) => c.prompt)).toEqual(['Capital of France?', 'Capital of Japan?']);
    // A trivia card is only reachable through its queue row.
    expect(repos.trivia.listQueueForDeck(deck).map((e) => e.queue_position)).toEqual([1, 2]);
    expect(h.progress(ID)?.progress).toMatchObject({ status: 'done', total: 2, generated_count: 2, inserted: 2, skipped_dups: 0, skipped_invalid: 0 });
  });

  it('skips a prompt the deck already has, whatever its case and padding', async () => {
    const h = workflowHarness(() =>
      pairs([
        ['  capital of FRANCE?  ', 'Paris'],
        ['Capital of Japan?', 'Tokyo'],
        ['Capital of Japan?', 'Tokyo again'],
      ]),
    );
    const deck = seedTriviaDeck(h);
    h.repos().questions.add(deck, { type: 'short', prompt: 'Capital of France?', answer: 'Paris' });

    await h.start('TriviaGenerate', ID, input());
    expect(await h.run(ID)).toBe('terminal');

    // The first is the seeded card, the third is the second one again.
    expect(h.repos().questions.listInDeck(deck).map((c) => c.prompt)).toEqual(['Capital of France?', 'Capital of Japan?']);
    expect(h.progress(ID)?.progress).toMatchObject({ inserted: 1, skipped_dups: 2, skipped_invalid: 0, total: 3 });
  });

  it('counts a pair with no answer as invalid and keeps going', async () => {
    const h = workflowHarness(() => JSON.stringify([{ q: 'Q1', a: '' }, { q: '', a: 'A2' }, { q: 'Q3', a: 'A3' }]));
    const deck = seedTriviaDeck(h);
    await h.start('TriviaGenerate', ID, input());
    expect(await h.run(ID)).toBe('terminal');

    expect(h.repos().questions.listInDeck(deck).map((c) => c.prompt)).toEqual(['Q3']);
    expect(h.progress(ID)?.progress).toMatchObject({ status: 'done', inserted: 1, skipped_dups: 0, skipped_invalid: 2 });
  });

  it('fails with the Go message when the model returned nothing usable', async () => {
    const h = workflowHarness(() => '[]');
    seedTriviaDeck(h);
    await h.start('TriviaGenerate', ID, input());
    expect(await h.run(ID)).toBe('terminal');
    expect(h.statuses(ID)).toEqual(['generating', 'failed']);
    expect(h.progress(ID)?.progress['error']).toBe(NO_CARDS);
  });

  it('caps an over-long batch at the size the caller asked for', async () => {
    const h = workflowHarness(() => pairs([['a', '1'], ['b', '2'], ['c', '3'], ['d', '4']]));
    const deck = seedTriviaDeck(h);
    await h.start('TriviaGenerate', ID, input({ batchSize: 2 }));
    expect(await h.run(ID)).toBe('terminal');
    expect(h.agent.prompts[0]).toContain('Generate exactly 2 questions on the topic:');
    expect(h.repos().questions.listInDeck(deck).map((c) => c.prompt)).toEqual(['a', 'b']);
    expect(h.progress(ID)?.progress['total']).toBe(2);
  });

  it('tells the model which prompts the deck already carries', async () => {
    const h = workflowHarness(() => pairs([['new one', 'yes']]));
    seedTriviaDeck(h);
    await h.start('TriviaGenerate', ID, input({ existing: ['Capital of France?', 'Capital of Japan?'] }));
    await h.run(ID);
    expect(h.agent.prompts[0]).toContain('- Capital of France?\n- Capital of Japan?\n');
    expect(h.agent.prompts[0]).toContain(`Generate exactly ${DEFAULT_BATCH_SIZE} questions`);
  });
});

describe('the trivia prompt and parser', () => {
  it('says so when the deck is empty', () => {
    expect(buildTriviaPrompt(25, 'topic', [])).toContain('(none yet — this is the first batch)');
  });

  it('reads the array out of a fenced block and refuses output without one', () => {
    expect(parseTriviaJson('```json\n[{"q":"Q","a":"A"}]\n```')).toEqual([{ q: 'Q', a: 'A' }]);
    expect(parseTriviaJson('here you go [{"q":"Q","a":"A","e":"E"}]')).toEqual([{ q: 'Q', a: 'A', e: 'E' }]);
    expect(() => parseTriviaJson('sorry')).toThrow(TriviaParseError);
    expect(() => parseTriviaJson('[not json]')).toThrow(TriviaParseError);
  });
});
