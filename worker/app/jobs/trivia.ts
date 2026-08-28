// Trivia batch generation: one call for N pairs, then one insert per pair.
// Linear, no gate: a trivia user types a topic and gets a deck back.
//
// The prompt is a fixture key as well as a prompt: the canned LLM keys its
// replies on the exact message, so editing the wording means re-recording.
// The existing-prompts block it needs is a read of the owner's deck, so the
// route puts it in the job input: the JobCell holds the agent and no
// repositories.
import { coerceTriviaPairs, triviaGenerated, triviaProgress, type TriviaCounts, type TriviaPair } from '../../domain/jobs/progress.js';
import { llmStep, type StepOutput, type WriteStepContext } from './registry.js';
import { resolveDeck } from './plan.js';

export const DEFAULT_BATCH_SIZE = 25;
/** How many of the deck's prompts the dedupe block lists. */
export const EXISTING_PROMPT_LIMIT = 200;
export const NO_CARDS = 'the AI returned 0 cards';

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

export interface TriviaJobInput {
  deckId: number;
  deckName: string;
  topic: string;
  /** Zero means the default; the free tier sets it, BYOK leaves it off. */
  batchSize: number;
  /** The deck's current prompts, so the model is told what not to repeat. */
  existing: string[];
}

export function triviaJobInput(input: Readonly<Record<string, unknown>>): TriviaJobInput {
  return {
    deckId: Number(input['deckId'] ?? 0),
    deckName: str(input['deckName']),
    topic: str(input['topic']),
    batchSize: Number(input['batchSize'] ?? 0) || 0,
    existing: Array.isArray(input['existing']) ? (input['existing'] as unknown[]).filter((v): v is string => typeof v === 'string') : [],
  };
}

export function existingBlock(prompts: readonly string[]): string {
  if (prompts.length === 0) return '(none yet — this is the first batch)';
  return prompts
    .slice(0, EXISTING_PROMPT_LIMIT)
    .map((p) => `- ${p}\n`)
    .join('');
}

export function buildTriviaPrompt(batch: number, topic: string, prompts: readonly string[]): string {
  return `You are generating short-answer trivia questions for a notification-driven flashcard app. Each card has a Q (the prompt), an A (the short answer), and an E (a deeper explanation revealed when the user taps "Deep dive").

Generate exactly ${batch} questions on the topic:

${topic.trim()}

Constraints:
- Question (q): default to <= 140 chars so the push-notification preview reads well. When the topic naturally calls for a snippet, table, or multi-line formatted content, the q MAY be longer and include markdown fenced code blocks — keep the FIRST line a short plain-language summary so the notification preview stays meaningful.
- Answer (a): short enough to type on a phone in a few seconds. A few words, a number, an identifier, a brief phrase, a small expression. Not full sentences.
- Explanation (e): 2-4 sentences, ~300 chars. Surface the WHY: context, causation, why this matters, common misconception, or a memorable hook. Treat the user as smart and curious — go beyond restating the answer.
- Cover varied sub-areas of the topic AND vary the recall shape across cards — different facets, angles, or skill probes that the topic naturally supports. Pick shapes that fit; if the deck's topic prompt explicitly calls out shapes, follow it. Aim for 3-5 DISTINCT shapes across the batch and don't let any single shape exceed ~30% of the cards.

  Concrete examples of shape variety (illustrative — adapt to YOUR topic; a music topic doesn't need code traces, a history topic doesn't need complexity drills):
    {"q": "Predict the output:\\n\`\`\`python\\nx = [3,1,4,1,5]\\nprint(sorted(set(x)))\\n\`\`\`", "a": "[1, 3, 4, 5]", "e": "..."}
    {"q": "Pythonic way to count occurrences in an iterable", "a": "Counter(items)", "e": "..."}
    {"q": "Worst-case time complexity of list.pop(0)", "a": "O(n)", "e": "..."}
    {"q": "Two sorted arrays, find the median in O(log n). Pattern?", "a": "binary search on partition", "e": "..."}
    {"q": "What's wrong?\\n\`\`\`python\\nfor i in range(len(a)):\\n    a.append(a[i])\\n\`\`\`", "a": "infinite loop — appending while iterating", "e": "..."}
    {"q": "Goal: only one goroutine reads from a channel at a time. Primitive?", "a": "sync.Mutex", "e": "..."}
- Don't duplicate any EXACT existing question. Drilling the same underlying concept from a DIFFERENT shape is encouraged — e.g., the same idea probed via a code trace AND a complexity drill AND an idiom card builds deeper recall than any single angle. Vary the shape, not just the wording.
- Existing questions to avoid duplicating exactly:

${existingBlock(prompts)}

Output ONLY a valid JSON array, with no surrounding prose or markdown fences around the JSON itself. The q field MAY contain markdown (including fenced code blocks) inside the JSON string — escape newlines as \\n. Format:

[
  {"q": "Question text?", "a": "Short answer", "e": "2-4 sentence explanation."},
  ...
]
`;
}

export class TriviaParseError extends Error {}

/** Fence-stripped, then the first `[` to the last `]`: models wrap JSON in
 * prose often enough that the strict parse is not worth the failures. */
export function parseTriviaJson(raw: string): TriviaPair[] {
  let text = raw.trim();
  text = text.replace(/^```(?:json)?\s*/i, '');
  text = text.replace(/\s*```\s*$/, '');
  const start = text.indexOf('[');
  const end = text.lastIndexOf(']');
  if (start < 0 || end < 0 || end < start) throw new TriviaParseError('agent returned unparseable JSON: no JSON array');
  try {
    return coerceTriviaPairs(JSON.parse(text.slice(start, end + 1)));
  } catch (e) {
    throw new TriviaParseError(`agent returned unparseable JSON: ${e instanceof Error ? e.message : String(e)}`);
  }
}

/** The batch this job asks the model for; an uncapped tier leaves the input
 * at zero, which is the default the Go workflow substituted. */
export function triviaBatchSize(input: Readonly<Record<string, unknown>>): number {
  return triviaJobInput(input).batchSize || DEFAULT_BATCH_SIZE;
}

export const generateStep = llmStep(async (ctx) => {
  const input = triviaJobInput(ctx.input);
  const batch = triviaBatchSize(ctx.input);
  const text = await ctx.agent.complete({ system: '', user: buildTriviaPrompt(batch, input.topic, input.existing) });
  let pairs = parseTriviaJson(text);
  // The prompt's "exactly N" is advisory; the cap is enforced here.
  if (input.batchSize > 0 && pairs.length > batch) pairs = pairs.slice(0, batch);
  if (pairs.length === 0) throw new Error(NO_CARDS);
  return { value: pairs, items: pairs, progress: triviaGenerated(pairs.length) };
});

/** What one insert row did, and what the counters are built back up from. */
export type InsertOutcome = 'inserted' | 'duplicate' | 'invalid';

export interface TriviaInsertValue {
  outcome: InsertOutcome;
  question_id: number;
}

/**
 * The three counters, rebuilt from the ledger rather than carried. A row that
 * errored out is not `done`, so it leaves no value behind; the gap between
 * this row's ordinal and the number of values before it is exactly how many
 * of its predecessors failed.
 */
export function countsBefore(item: number, prior: readonly TriviaInsertValue[]): TriviaCounts {
  const of = (outcome: InsertOutcome) => prior.filter((v) => v?.outcome === outcome).length;
  return { inserted: of('inserted'), skipped_dups: of('duplicate'), skipped_invalid: of('invalid') + (item - prior.length) };
}

/**
 * One pair into the deck, under `<jobId>-insert-<i>`. The key is checked
 * first: without it a redelivered row finds the card it inserted itself and
 * records a duplicate, so the counters the terminal screen shows would drift
 * from the deck the user is reading.
 */
export const triviaInsert = async (ctx: WriteStepContext): Promise<StepOutput> => {
  const pair = (ctx.itemInput ?? {}) as TriviaPair;
  const prompt = (pair.q ?? '').trim();
  const answer = (pair.a ?? '').trim();
  const explanation = (pair.e ?? '').trim();
  const counts = countsBefore(ctx.item, (ctx.outputs['insert'] as TriviaInsertValue[] | undefined) ?? []);

  let value: TriviaInsertValue;
  if (!prompt || !answer) {
    counts.skipped_invalid += 1;
    value = { outcome: 'invalid', question_id: 0 };
  } else {
    value = ctx.repos.tx.sync((): TriviaInsertValue => {
      const already = ctx.repos.idempotency.findQuestion(ctx.stepKey);
      if (already !== null) return { outcome: 'inserted', question_id: already };
      const deckId = resolveDeck(ctx.repos, ctx.input);
      // Dedupe on the deck's live rows, so a second batch cannot repeat the
      // first: the same LOWER(TRIM(prompt)) compare the Go insert made.
      const duplicate = ctx.repos.questions.findByPrompt(deckId, prompt);
      if (duplicate !== null) return { outcome: 'duplicate', question_id: duplicate };
      const qid = ctx.repos.questions.add(deckId, { type: 'short', topic: ctx.input['topic'] ? String(ctx.input['topic']) : null, prompt, answer, explanation: explanation || null });
      ctx.repos.idempotency.recordQuestion(ctx.stepKey, qid);
      ctx.repos.trivia.appendCard(qid, deckId);
      return { outcome: 'inserted', question_id: qid };
    });
    if (value.outcome === 'inserted') counts.inserted += 1;
    else counts.skipped_dups += 1;
  }
  return { value, progress: triviaProgress(counts) };
};
