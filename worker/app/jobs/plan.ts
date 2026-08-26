// Plan-first generation: an outline the user reviews, then one call per
// accepted item, then one insert per card. Prompts and parsers are the Go
// worker's, transcribed byte for byte, because the free-tier stub keys its
// canned replies on the message it is sent.
//
// The two LLM steps run in the JobCell, which holds no repositories, so
// everything they read about the deck arrives in the job input.
import type { NewQuestion, QuestionType } from '../entities.js';
import type { UserRepos } from '../ports.js';
import { llmStep, type StepOutput, type WriteStepContext } from './registry.js';
import { coerceCard, coercePlanItems, expandProgress, planProgress, planResult, truncate, type GeneratedCard, type PlanItem } from '../../domain/jobs/progress.js';

/** What the plan and expand steps read out of the job input. */
export interface PlanJobInput {
  deckId: number;
  deckName: string;
  /** The deck's description; it seeds the outline and grounds each card. */
  prompt: string;
  /** Above zero caps the outline; the free tier sets it, BYOK leaves it off. */
  maxCards: number;
}

/** The plan step's stored value: the outline plus the round that produced it,
 * so a re-run reads its predecessor's number rather than counting rows. */
export interface PlanRound {
  items: PlanItem[];
  round: number;
}

export const PRIOR_BRIEF_CHARS = 200;

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

export function planJobInput(input: Readonly<Record<string, unknown>>): PlanJobInput {
  return {
    deckId: Number(input['deckId'] ?? 0),
    deckName: str(input['deckName']),
    prompt: str(input['prompt']),
    maxCards: Number(input['maxCards'] ?? 0) || 0,
  };
}

// ---- prompts ----------------------------------------------------------------

const DEFAULT_SIZING = `Decide how many cards to create: let the description guide you. Most
decks want 5-15 cards covering the main concepts; a tightly-scoped
description might warrant only 3, a broad survey might warrant 20+.
Don't pad. Don't skimp.`;

const cappedSizing = (max: number): string => `Create at most ${max} cards. Cover the most important concepts the
description supports within that limit. Don't pad.`;

export function renderPriorPlan(items: readonly PlanItem[]): string {
  let out = '';
  items.forEach((it, i) => {
    out += `${i + 1}. [${it.type ?? ''}] ${it.title} — ${truncate(it.brief, PRIOR_BRIEF_CHARS)}\n`;
  });
  return out;
}

export interface PlanPromptInput extends PlanJobInput {
  priorPlan: readonly PlanItem[];
  feedback: string;
}

export function buildPlanPrompt(input: PlanPromptInput): string {
  if (input.priorPlan.length === 0) {
    const sizing = input.maxCards > 0 ? cappedSizing(input.maxCards) : DEFAULT_SIZING;
    return `You are planning a set of spaced-repetition flashcards for the deck "${input.deckName}".

The user provided this description / topic:

${input.prompt}

${sizing}

Return a JSON array of cards. Each entry is an OBJECT with these fields:

  - "title":    a short label (3-8 words). What the card is about.
  - "brief":    1-2 sentences describing what the question will ask.
  - "type":     one of "code" | "mcq" | "multi" | "short". Pick the type
                that matches the brief best — code for "implement X",
                mcq/multi for fact recall, short for "explain Y".
  - "topic":    optional short tag for grouping (e.g. "concurrency",
                "system design", "behavioral").
  - "language": REQUIRED only when type=="code"; one of
                go|java|python|javascript|typescript|rust|cpp.

Output ONLY the JSON array, no prose, no fences.`;
  }
  let out = `Refine the card plan for deck "${input.deckName}".

Original description:
${input.prompt}

Your previous plan (${input.priorPlan.length} cards):
${renderPriorPlan(input.priorPlan)}

The user wants this changed:
${input.feedback}

Output a NEW JSON array (same field shape: title, brief, type, topic,
language?). Apply the user's feedback. You may add, remove, replace,
or reorder items. Output ONLY the JSON array.`;
  if (input.maxCards > 0) out += `\n\nKeep the plan to at most ${input.maxCards} cards.`;
  return out;
}

const languageHint = (item: PlanItem): string => (item.type === 'code' && item.language ? `\n  language: ${item.language}` : '');

export function buildExpandPrompt(input: PlanJobInput, item: PlanItem): string {
  return `Generate ONE flashcard for deck "${input.deckName}".

Deck description (for grounding):
${input.prompt}

This card was planned as:
  title: ${item.title}
  brief: ${item.brief}
  type:  ${item.type ?? ''}${languageHint(item)}

Write the FULL card content matching the planned title + brief + type.
Output a single JSON object (no prose, no fences). Required fields:

  - "type":     "${item.type ?? ''}"  (use the planned type)
  - "topic":    short string tag
  - "prompt":   the question text. markdown ok.
  - "choices":  array, REQUIRED for mcq/multi, OMIT otherwise.
  - "answer":   string. for multi: a JSON-encoded array of correct choices.
  - "rubric":   2-4 short bullet lines describing what a correct answer
                must demonstrate.
  - "skeleton": OPTIONAL. for code questions only. minimal starter
                scaffold (class signature with empty method bodies) when
                the canonical version of the problem provides one. OMIT
                otherwise. NEVER include placeholder comments inside
                method bodies.
  - "language": REQUIRED for code. one of go|java|python|javascript|
                typescript|rust|cpp. Match the brief.

Output ONLY the JSON object.`;
}

// ---- parsers ------------------------------------------------------------------

/**
 * The JSON payload out of whatever the model returned: a fenced block
 * anywhere in the string, else the first opener to the last matching closer,
 * else the string itself. Transcribed from the Go worker's `extractJSON`,
 * which grew each branch from a failure seen in production.
 */
export function extractJson(raw: string): string {
  const s = raw.trim();
  const start = s.indexOf('```');
  if (start >= 0) {
    let after = s.slice(start + 3);
    const nl = after.indexOf('\n');
    if (nl >= 0) after = after.slice(nl + 1);
    const end = after.indexOf('```');
    return (end >= 0 ? after.slice(0, end) : after).trim();
  }
  for (const [open, close] of [
    ['[', ']'],
    ['{', '}'],
  ] as const) {
    const i = s.indexOf(open);
    if (i >= 0) {
      const j = s.lastIndexOf(close);
      if (j > i) return s.slice(i, j + 1);
    }
  }
  return s;
}

export class PlanParseError extends Error {}

/** The array literal, a `{"plan": [...]}` wrapper, or neither. */
export function parsePlanJson(raw: string): PlanItem[] {
  const body = extractJson(raw);
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch (e) {
    throw new PlanParseError(`plan JSON parse failed: ${e instanceof Error ? e.message : String(e)}: ${truncate(raw, 800)}`);
  }
  const value = Array.isArray(parsed) ? parsed : ((parsed as { plan?: unknown } | null)?.plan ?? null);
  try {
    return coercePlanItems(value);
  } catch (e) {
    throw new PlanParseError(`plan JSON parse failed: ${e instanceof Error ? e.message : String(e)}: ${truncate(raw, 800)}`);
  }
}

/** The trim-and-slice the Go card/verdict/transform parsers share. */
export function sliceJsonObject(raw: string): string {
  let text = raw.trim();
  if (text.startsWith('```json')) text = text.slice('```json'.length);
  else if (text.startsWith('```')) text = text.slice(3);
  if (text.endsWith('```')) text = text.slice(0, -3);
  text = text.trim();
  const open = text.indexOf('{');
  if (open > 0) text = text.slice(open);
  const close = text.lastIndexOf('}');
  if (close >= 0 && close < text.length - 1) text = text.slice(0, close + 1);
  return text;
}

export function parseJsonObject(raw: string): unknown {
  return JSON.parse(sliceJsonObject(raw));
}

export class CardParseError extends Error {}

export function parseCardJson(raw: string): GeneratedCard {
  let parsed: unknown;
  try {
    parsed = parseJsonObject(raw);
  } catch (e) {
    throw new CardParseError(`card JSON parse failed: not JSON: ${e instanceof Error ? e.message : String(e)}: ${truncate(raw, 800)}`);
  }
  try {
    return coerceCard(parsed);
  } catch (e) {
    throw new CardParseError(`card JSON parse failed: ${e instanceof Error ? e.message : String(e)}: ${truncate(raw, 800)}`);
  }
}

// ---- steps ---------------------------------------------------------------------

/** The gate's stored outcome, as the runner writes it. `payload` is the
 * signal's body: the feedback text a re-plan reads. */
interface GateOutcome {
  event?: string;
  payload?: unknown;
}

/** A signal may carry the text bare or under a key; both reach here. */
export function feedbackOf(gateOutput: unknown): string {
  const payload = (gateOutput as GateOutcome | null)?.payload;
  if (typeof payload === 'string') return payload;
  if (payload && typeof payload === 'object') return str((payload as Record<string, unknown>)['feedback']);
  return '';
}

const priorRound = (value: unknown): PlanRound | null => {
  const v = value as PlanRound | null;
  return v && Array.isArray(v.items) ? v : null;
};

export const planStep = llmStep(async (ctx) => {
  const input = planJobInput(ctx.input);
  const prior = priorRound(ctx.outputs['plan']);
  const text = await ctx.agent.complete({
    system: '',
    user: buildPlanPrompt({ ...input, priorPlan: prior?.items ?? [], feedback: feedbackOf(ctx.outputs['gate']) }),
  });
  let items = parsePlanJson(text);
  // The prompt's "at most N" is advisory; the cap is enforced here.
  if (input.maxCards > 0 && items.length > input.maxCards) items = items.slice(0, input.maxCards);
  const round = (prior?.round ?? 0) + 1;
  return { value: { items, round } satisfies PlanRound, items, progress: planProgress(items, round) };
});

export const expandStep = llmStep(async (ctx) => {
  const input = planJobInput(ctx.input);
  const item = ctx.itemInput as PlanItem;
  const text = await ctx.agent.complete({ system: '', user: buildExpandPrompt(input, item) });
  const card = parseCardJson(text);
  // Backfill from the plan when the model dropped the tag.
  if (!card.topic && item.topic) card.topic = item.topic;
  const total = priorRound(ctx.outputs['plan'])?.items.length ?? 0;
  const done = (ctx.outputs['expand'] as unknown[] | undefined)?.length ?? 0;
  return { value: card, progress: expandProgress(done + 1, total) };
});

/** The column form of a generated card. A code card with no language gets the
 * Go worker's `go` default; the type is stored as the model spelled it, since
 * the column is free text and the renderers branch on the four they know. */
export function toNewQuestion(card: GeneratedCard): NewQuestion {
  return {
    type: card.type as QuestionType,
    topic: card.topic ?? null,
    prompt: card.prompt,
    choices: card.choices ?? null,
    answer: card.answer,
    rubric: card.rubric || null,
    skeleton: card.skeleton ?? null,
    language: card.type === 'code' ? card.language || 'go' : (card.language ?? null),
    explanation: card.explanation ?? null,
    answer_regex: card.answer_regex ?? null,
  };
}

/** The deck the job names, created on demand the way the Go insert did. */
export function resolveDeck(repos: UserRepos, input: Readonly<Record<string, unknown>>): number {
  const id = Number(input['deckId'] ?? 0);
  if (id > 0 && repos.decks.findName(id) !== null) return id;
  return repos.decks.getOrCreate(str(input['deckName']));
}

/**
 * One card into the deck, under `<jobId>-insert-<i>`. The key is looked up
 * first and recorded beside the row, so a redelivered step finds the card it
 * already wrote instead of a second copy.
 */
export const planInsert = async (ctx: WriteStepContext): Promise<StepOutput> => {
  const card = coerceCard(ctx.itemInput);
  const deckId = resolveDeck(ctx.repos, ctx.input);
  let qid = ctx.repos.idempotency.findQuestion(ctx.stepKey);
  if (qid === null) {
    qid = ctx.repos.questions.add(deckId, toNewQuestion(card));
    ctx.repos.idempotency.recordQuestion(ctx.stepKey, qid);
  }
  const added = [...((ctx.outputs['insert'] as number[] | undefined) ?? []), qid];
  return { value: qid, progress: planResult(added) };
};
