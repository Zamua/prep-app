// Transform: the model proposes a plan over one card, one deck, or the whole
// library, and (past the gate, for the two wide scopes) the plan is written.
// The prompts are fixture keys as well as prompts: the canned LLM keys its
// replies on the exact message, so editing the wording means re-recording.
//
// A JobCell holds the agent and no repositories, so the snapshot travels in
// the job input, which also pins what the user reviewed to what the model
// was shown.
import type { NewQuestion, Question, QuestionType } from '../entities.js';
import type { TransformJobInput, UserRepos } from '../ports.js';
import {
  coerceTransformPlan,
  emptyTransformResult,
  transformApplied,
  transformComputed,
  type CardModification,
  type GeneratedCard,
  type TransformPlan,
  type TransformResult,
} from '../../domain/jobs/progress.js';
import { llmStep, type StepOutput, type WriteStepContext } from './registry.js';
import { parseJsonObject, toNewQuestion } from './plan.js';
import { SCOPES, transformCard, transformDeck, type TransformCard, type TransformDeck, type TransformScope } from '../../domain/jobs/snapshot.js';

export { SCOPES, type TransformCard, type TransformDeck, type TransformScope };

export const DEFAULT_TRIVIA_INTERVAL_MINUTES = 30;

const asString = (v: unknown): string => (typeof v === 'string' ? v : '');

export class BadTransformInput extends Error {}

const asRecord = (raw: unknown): Record<string, unknown> => (typeof raw === 'object' && raw !== null ? (raw as Record<string, unknown>) : {});

const asCard = (raw: unknown): TransformCard => transformCard(asRecord(raw));

const asDeck = (raw: unknown): TransformDeck => {
  const fields = asRecord(raw);
  return transformDeck(fields, (Array.isArray(fields['cards']) ? fields['cards'] : []).map(asCard));
};

/** Re-reads the persisted input on every activation, so a step decides from
 * the rows and never from a value held across a crash. */
export function transformJobInput(input: Readonly<Record<string, unknown>>): TransformJobInput {
  const scope = asString(input['scope']) as TransformScope;
  if (!SCOPES.includes(scope)) throw new BadTransformInput(`unknown scope: ${JSON.stringify(asString(input['scope']))}`);
  if (!asString(input['prompt']).trim()) throw new BadTransformInput('prompt required');
  return {
    scope,
    targetId: Number(input['targetId'] ?? 0),
    prompt: asString(input['prompt']),
    deckName: typeof input['deckName'] === 'string' ? input['deckName'] : null,
    deckContextPrompt: asString(input['deckContextPrompt']),
    cards: (Array.isArray(input['cards']) ? input['cards'] : []).map(asCard),
    decks: (Array.isArray(input['decks']) ? input['decks'] : []).map(asDeck),
  };
}

/** The picture the compute step is shown, read in the owner's cell before the
 * job starts. The queries are the Go loaders': one named card, a deck's
 * unsuspended cards by id, or the whole library by deck name. */
export function transformSnapshot(repos: UserRepos, scope: TransformScope, targetId: number): Pick<TransformJobInput, 'cards' | 'decks'> {
  if (scope === 'reorganize') return { cards: [], decks: repos.decks.listForTransform().map((d) => transformDeck(d, repos.questions.cardsForTransform(d.id))) };
  if (scope === 'deck') return { cards: repos.questions.cardsForTransform(targetId), decks: [] };
  const card = repos.questions.cardForTransform(targetId);
  return { cards: card ? [card] : [], decks: [] };
}

/** The owning deck's standing description, '' when it has none. */
export function deckContextFor(repos: UserRepos, deckId: number): string {
  const name = repos.decks.findName(deckId);
  return name === null ? '' : (repos.decks.getContextPrompt(name) ?? '');
}

// ---- prompts ----------------------------------------------------------------

/** Go's `json.MarshalIndent` escapes the three HTML-significant runes; a card
 * holding `<` would otherwise reach the model as different bytes here. */
export function goJson(value: unknown): string {
  return JSON.stringify(value, null, 2).replace(/[<>&]/g, (c) => `\\u00${c.charCodeAt(0).toString(16)}`);
}

export function deckContextBlock(deckContextPrompt: string): string {
  const trimmed = deckContextPrompt.trim();
  if (!trimmed) return '';
  return `**Deck overall context** (what this deck is about per the owner):
${trimmed}

`;
}

export function cardScopePrompt(card: TransformCard, userPrompt: string, deckContextPrompt: string): string {
  return `You are improving a single flashcard in a spaced-repetition learning app, per the user's request.

${deckContextBlock(deckContextPrompt)}**Current card (JSON):**
\`\`\`json
${goJson(card)}
\`\`\`

**User's request:**
${userPrompt}

If URLs or recent material are referenced, you may use your web-fetch / web-search tools to ground the change.

Return a JSON object describing the new state of THIS card. Shape:

\`\`\`json
{
  "modifications": [{
    "question_id": <id>,
    "type": "code|mcq|multi|short",
    "topic": "...",
    "prompt": "...",
    "choices": ["..."],         // omit for code/short
    "answer": "...",
    "rubric": "...",
    "skeleton": "...",          // optional starter code for code questions
    "language": "...",          // optional, only for code (go|java|python|...)
    "explanation": "...",       // trivia only — 2-4 sentence "deep dive"
    "answer_regex": "..."       // trivia only — case-insensitive fullmatch
  }],
  "notes": "<one short sentence summarizing what changed>"
}
\`\`\`

Preserve fields the user's request didn't ask to change. Output ONLY the JSON object, no commentary or fences.`;
}

export function deckScopePrompt(cards: readonly TransformCard[], userPrompt: string, deckContextPrompt: string): string {
  return `You are applying a deck-wide transformation to a spaced-repetition flashcard deck, per the user's request.

${deckContextBlock(deckContextPrompt)}**Current deck (JSON array of cards):**
\`\`\`json
${goJson(cards)}
\`\`\`

**User's request:**
${userPrompt}

If URLs or recent material are referenced, you may use your web-fetch / web-search tools to ground the change.

Return a JSON object describing the changes to apply. Only include cards that actually need to change. Shape:

\`\`\`json
{
  "modifications": [
    {"question_id": <id>, "type": "...", "topic": "...", "prompt": "...", "choices": [...], "answer": "...", "rubric": "...", "skeleton": "...", "language": "...", "explanation": "...", "answer_regex": "..."}
  ],
  "additions": [
    {"type": "code|mcq|multi|short", "topic": "...", "prompt": "...", "choices": [...], "answer": "...", "rubric": "...", "skeleton": "...", "language": "...", "explanation": "...", "answer_regex": "..."}
  ],
  "deletions": [<question_id>, ...],
  "notes": "<one short sentence summarizing the overall change>"
}
\`\`\`

Field guidance:
- explanation + answer_regex are TRIVIA-only (the cards in the input JSON will only have them set if this is a trivia deck). They surface as a "Deep dive" disclosure (explanation, 2-4 sentences) and the first-pass grader regex (answer_regex, case-insensitive fullmatch). If you change a card's prompt/answer, ALSO update explanation + answer_regex to stay in sync — a stale regex matching the old answer will silently mis-grade.
- For srs cards, leave explanation and answer_regex empty.
- Preserve fields the user's request didn't ask to change.

Output ONLY the JSON object, no commentary or fences. If the request asks for fewer than 1 change, return empty arrays. Cap additions at 15 cards per request.`;
}

export function reorganizeScopePrompt(decks: readonly TransformDeck[], userPrompt: string): string {
  return `You are restructuring a user's flashcard library across multiple decks, per their request.

**Current decks (JSON, with cards):**
\`\`\`json
${goJson(decks)}
\`\`\`

**User's request:**
${userPrompt}

You can:
- Edit cards (modifications) — change prompt/answer/explanation/etc on any existing card.
- Add cards (additions) — each addition specifies dest_deck (the destination deck name; existing OR a name you propose in new_decks).
- Delete cards (deletions) — by question_id.
- Create new decks (new_decks) — name + deck_type ("srs" or "trivia") + topic + interval_minutes (trivia only; default 30).
- Move cards between decks (card_moves) — each move references question_id + dest_deck (name).
- Rename existing decks (deck_renames) — by deck_id + new_name.
- Delete decks (deck_deletions) — by deck_id; cascades through the deck's cards.

Do ONLY what the user's request implies. If the request is "fix typos across all decks", return modifications, no new_decks/moves. If the request is "split deck X into Y and Z", return new_decks for Y/Z and card_moves placing each existing card in its new home; NO modifications unless the user also asked for content edits.

For trivia cards: when you change prompt or answer, also update explanation + answer_regex so they stay in sync. answer_regex is a case-insensitive full-match pattern that should match the new answer + obvious legitimate alternative forms.

If URLs or recent material are referenced, you may use your web-fetch / web-search tools to ground the change.

Return ONLY a JSON object, no commentary or fences. Shape:

\`\`\`json
{
  "modifications": [
    {"question_id": <id>, "type": "...", "topic": "...", "prompt": "...", "choices": [...], "answer": "...", "rubric": "...", "skeleton": "...", "language": "...", "explanation": "...", "answer_regex": "..."}
  ],
  "additions": [
    {"dest_deck": "<deck name>", "type": "code|mcq|multi|short", "topic": "...", "prompt": "...", "answer": "...", ...}
  ],
  "deletions": [<question_id>, ...],
  "new_decks": [
    {"name": "...", "deck_type": "srs|trivia", "topic": "...", "interval_minutes": 30}
  ],
  "card_moves": [
    {"question_id": <id>, "dest_deck": "<deck name>"}
  ],
  "deck_renames": [
    {"deck_id": <id>, "new_name": "..."}
  ],
  "deck_deletions": [<deck_id>, ...],
  "notes": "<one short sentence summarizing what changed>"
}
\`\`\`

Cap additions at 25 per request. If the request implies fewer than one change, return empty arrays. Do not invent operations beyond what the request asks for.`;
}

export function buildTransformPrompt(input: TransformJobInput): string {
  if (input.scope === 'card') {
    const card = input.cards[0];
    if (!card) throw new BadTransformInput(`question ${input.targetId} not found`);
    return cardScopePrompt(card, input.prompt, input.deckContextPrompt);
  }
  // Reorganize is cross-deck; each deck's JSON already carries its own topic,
  // so a single deck's context would be both redundant and ambiguous.
  if (input.scope === 'reorganize') return reorganizeScopePrompt(input.decks, input.prompt);
  return deckScopePrompt(input.cards, input.prompt, input.deckContextPrompt);
}

export class TransformParseError extends Error {}

export function parseTransformPlan(raw: string, scope: string): TransformPlan {
  try {
    return coerceTransformPlan(parseJsonObject(raw), scope);
  } catch (e) {
    throw new TransformParseError(`parse plan: ${e instanceof Error ? e.message : String(e)}`);
  }
}

// ---- steps -----------------------------------------------------------------------

export const computeStep = llmStep(async (ctx) => {
  const input = transformJobInput(ctx.input);
  const text = await ctx.agent.complete({ system: '', user: buildTransformPrompt(input) });
  const plan = parseTransformPlan(text, input.scope);
  return { value: plan, progress: transformComputed(plan) };
});

/** Keeps the stored value for a field the model left empty, and overwrites the
 * rest: the COALESCE(NULLIF(...)) shape of the Go update. */
export function mergeModification(existing: Question, m: CardModification): NewQuestion {
  const answer = Array.isArray(m.answer) ? (m.answer.length ? m.answer : existing.answer) : m.answer || existing.answer;
  return {
    type: (m.type || existing.type) as QuestionType,
    topic: m.topic ?? null,
    prompt: m.prompt || existing.prompt,
    choices: m.choices ?? null,
    answer,
    rubric: m.rubric || null,
    skeleton: m.skeleton ?? null,
    language: m.language ?? null,
    explanation: m.explanation ?? null,
    answer_regex: m.answer_regex ?? null,
  };
}

const deckNames = (repos: UserRepos): Map<string, number> => new Map(repos.decks.listSummaries().map((d) => [d.name, d.id]));

/** Creates the decks the plan proposes, skipping a name already taken: an
 * existing deck is never clobbered, which would surprise the user and leave
 * the rest of the plan pointing at the wrong rows. */
function applyNewDecks(repos: UserRepos, plan: TransformPlan, names: Map<string, number>, result: TransformResult): void {
  for (const op of plan.new_decks) {
    if (names.has(op.name)) continue;
    const id =
      op.deck_type === 'trivia'
        ? repos.decks.createTrivia(op.name, { topic: op.topic ?? '', intervalMinutes: op.interval_minutes && op.interval_minutes > 0 ? op.interval_minutes : DEFAULT_TRIVIA_INTERVAL_MINUTES })
        : repos.decks.create(op.name, { contextPrompt: op.topic ?? null });
    names.set(op.name, id);
    result.created_deck_ids.push(id);
  }
}

function applyRenames(repos: UserRepos, plan: TransformPlan, names: Map<string, number>, result: TransformResult): void {
  for (const op of plan.deck_renames) {
    if (names.has(op.new_name)) continue;
    const oldName = repos.decks.findName(op.deck_id);
    if (oldName === null) continue;
    if (!repos.decks.rename(oldName, op.new_name)) continue;
    names.delete(oldName);
    names.set(op.new_name, op.deck_id);
    result.renamed_deck_ids.push(op.deck_id);
  }
}

function applyModifications(repos: UserRepos, plan: TransformPlan, result: TransformResult): void {
  for (const m of plan.modifications) {
    // A cell holds one user, so the row's existence is the ownership check the
    // Go activity spent a query on; a hallucinated id is skipped.
    const existing = repos.questions.get(m.question_id);
    if (existing === null) continue;
    // `replace`, not `update`: a modification carries the card's whole new
    // shape, explanation included.
    repos.questions.replace(m.question_id, mergeModification(existing, m));
    result.modified_ids.push(m.question_id);
  }
}

function applyAdditions(repos: UserRepos, plan: TransformPlan, input: TransformJobInput, names: Map<string, number>, stepKey: string, result: TransformResult): void {
  plan.additions.forEach((addition, i) => {
    const destId = addition.dest_deck ? (names.get(addition.dest_deck) ?? 0) : input.scope === 'deck' ? input.targetId : 0;
    if (!destId) return;
    const key = `${stepKey}-add-${i}`;
    let qid = repos.idempotency.findQuestion(key);
    if (qid === null) {
      qid = repos.questions.add(destId, toNewQuestion(addition));
      repos.idempotency.recordQuestion(key, qid);
      if (repos.decks.getType(destId) === 'trivia') repos.trivia.appendCard(qid, destId);
    }
    result.added_ids.push(qid);
  });
}

function applyMoves(repos: UserRepos, plan: TransformPlan, names: Map<string, number>, result: TransformResult): void {
  for (const move of plan.card_moves) {
    const destId = names.get(move.dest_deck) ?? 0;
    if (!destId) continue;
    const question = repos.questions.get(move.question_id);
    if (question === null || question.deck_id === destId) continue;
    const queued = repos.trivia.listQueueForDeck(question.deck_id).some((e) => e.question_id === move.question_id);
    if (repos.questions.moveToDeck([move.question_id], destId) === 0) continue;
    // A trivia deck's card must sit in its queue. The reverse (dropping the
    // queue row on a move into an SRS deck) has no repository method; the row
    // is left behind, and every queue read joins on the deck, so it is inert.
    if (repos.decks.getType(destId) === 'trivia' && !queued) repos.trivia.appendCard(move.question_id, destId);
    result.moved_card_ids.push(move.question_id);
  }
}

/**
 * The plan, written: every op in ONE transaction, so a throw part-way leaves
 * the library exactly as the user last saw it. Partial application is not a
 * state the ledger can reach.
 *
 * The whole result is recorded under the step key, because most of these ops
 * are idempotent by "check the state, skip if it is already there" and a skip
 * has no id to report. A redelivered step answers from that record instead of
 * re-deriving a result its first run already earned.
 */
export const applyStep = async (ctx: WriteStepContext): Promise<StepOutput> => {
  const done = ctx.repos.idempotency.findStepResult(ctx.stepKey) as TransformResult | null;
  if (done) return { value: done, progress: transformApplied(done) };
  const result = ctx.repos.tx.sync(() => applyPlan(ctx));
  return { value: result, progress: transformApplied(result) };
};

function applyPlan(ctx: WriteStepContext): TransformResult {
  const input = transformJobInput(ctx.input);
  const plan = coerceTransformPlan(ctx.outputs['compute'], input.scope);
  const result = emptyTransformResult();
  const names = deckNames(ctx.repos);

  applyNewDecks(ctx.repos, plan, names, result);
  applyRenames(ctx.repos, plan, names, result);
  applyModifications(ctx.repos, plan, result);
  applyAdditions(ctx.repos, plan, input, names, ctx.stepKey, result);
  for (const qid of plan.deletions) if (ctx.repos.questions.delete(qid)) result.deleted_ids.push(qid);
  applyMoves(ctx.repos, plan, names, result);
  for (const deckId of plan.deck_deletions) {
    const name = ctx.repos.decks.findName(deckId);
    if (name !== null && ctx.repos.decks.delete(name) > 0) result.deleted_deck_ids.push(deckId);
  }
  ctx.repos.idempotency.recordStepResult(ctx.stepKey, result);
  return result;
}
