// The four workflows' value objects, and the progress payloads their partials
// read. Pure: a handler coerces model output into these shapes and returns the
// progress keys it owns; the runner merges those in ledger order and never
// learns what a key means.
//
// The key names (`plan`, `round`, `total`, `generated_count`, `inserted`,
// `skipped_dups`, `skipped_invalid`, `result`, `notes`) are read by the
// partials, and key PRESENCE is itself part of the shape: a partial reads
// `key in progress` to tell "not yet" from "zero", so a field that is always
// written stays written and one that is dropped when empty stays dropped.

export type JsonRecord = Record<string, unknown>;

const isDict = (v: unknown): v is JsonRecord => typeof v === 'object' && v !== null && !Array.isArray(v);
const asString = (v: unknown): string => (typeof v === 'string' ? v : '');
const asInt = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : 0);
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);
const asInts = (v: unknown): number[] => (Array.isArray(v) ? v.map(asInt).filter((n) => n !== 0) : []);
const dicts = (v: unknown): JsonRecord[] => (Array.isArray(v) ? v.filter(isDict) : []);

/** Truncation with a real ellipsis, not three dots. */
export function truncate(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n)}…`;
}

/** Assigns the keys whose value is not empty: the `omitempty` half of a shape. */
function withOptional<T extends object>(base: T, optional: JsonRecord): T {
  const target = base as unknown as JsonRecord;
  for (const [key, value] of Object.entries(optional)) {
    if (value === undefined || value === '' || value === 0 || (Array.isArray(value) && value.length === 0)) continue;
    target[key] = value;
  }
  return base;
}

// ---- plan-first generation --------------------------------------------------

export interface PlanItem {
  title: string;
  brief: string;
  type?: string;
  topic?: string;
  language?: string;
}

/** A card as the model returns it. `answer` keeps its list form: the question
 * repository encodes a `multi` answer the way the rest of the app does. */
export interface GeneratedCard {
  type: string;
  topic?: string;
  prompt: string;
  choices?: string[];
  answer: string | string[];
  rubric: string;
  skeleton?: string;
  language?: string;
  explanation?: string;
  answer_regex?: string;
}

export class BadOutput extends Error {}

/** A rubric list becomes the `- item` lines the card column stores. */
function coerceRubric(v: unknown): string {
  if (typeof v === 'string') return v;
  if (Array.isArray(v))
    return v
      .filter((x): x is string => typeof x === 'string')
      .map((s) => `- ${s}`)
      .join('\n');
  return '';
}

const coerceAnswer = (v: unknown): string | string[] => (typeof v === 'string' ? v : Array.isArray(v) ? strings(v) : '');

export function coercePlanItems(value: unknown): PlanItem[] {
  if (!Array.isArray(value)) throw new BadOutput('plan is not an array');
  if (value.length === 0) throw new BadOutput('plan is empty');
  return value.map((raw) => {
    if (!isDict(raw)) throw new BadOutput('plan entry is not an object');
    return withOptional<PlanItem>({ title: asString(raw['title']).trim(), brief: asString(raw['brief']).trim() }, {
      type: asString(raw['type']).trim(),
      topic: asString(raw['topic']),
      language: asString(raw['language']),
    });
  });
}

/** The permissive read: every field the model may have set, nothing required.
 * The transform plan uses it, because its apply keeps the stored value for a
 * field the model left empty rather than refusing the whole op. */
export function cardFields(raw: JsonRecord): GeneratedCard {
  return withOptional<GeneratedCard>(
    { type: asString(raw['type']), prompt: asString(raw['prompt']), answer: coerceAnswer(raw['answer']), rubric: coerceRubric(raw['rubric']) },
    {
      topic: asString(raw['topic']),
      choices: strings(raw['choices']),
      skeleton: asString(raw['skeleton']),
      language: asString(raw['language']),
      explanation: asString(raw['explanation']),
      answer_regex: asString(raw['answer_regex']),
    },
  );
}

/** Throws `BadOutput` without the three fields a generated card cannot do
 * without, which is where the Go expansion gave up too. */
export function coerceCard(value: unknown): GeneratedCard {
  if (!isDict(value)) throw new BadOutput('card is not an object');
  const card = cardFields(value);
  const answered = Array.isArray(card.answer) ? card.answer.length > 0 : card.answer !== '';
  if (!card.type || !card.prompt || !answered) throw new BadOutput('missing required fields (type/prompt/answer)');
  return card;
}

/** The plan step's keys. The counters are present from the first round because
 * Go's zero-valued asInts marshal, and the partial reads them by name. */
export const planProgress = (items: readonly PlanItem[], round: number): JsonRecord => ({
  plan: items.map((i) => ({ ...i })),
  round,
  total: items.length,
  generated_count: 0,
});

export const expandProgress = (generated: number, total: number): JsonRecord => ({ generated_count: generated, total });

export const planResult = (addedIds: readonly number[]): JsonRecord => ({ result: { status: 'completed', added_ids: [...addedIds] } });

// ---- trivia ------------------------------------------------------------------

export interface TriviaPair {
  q: string;
  a: string;
  e?: string;
}

export interface TriviaCounts {
  inserted: number;
  skipped_dups: number;
  skipped_invalid: number;
}

export function coerceTriviaPairs(value: unknown): TriviaPair[] {
  if (!Array.isArray(value)) throw new BadOutput('trivia batch is not an array');
  return value.filter(isDict).map((raw) => withOptional<TriviaPair>({ q: asString(raw['q']), a: asString(raw['a']) }, { e: asString(raw['e']) }));
}

/** Before the call returns: the batch it was asked for, beside the counters
 * Go's zero-valued asInts marshalled. */
export const triviaStarting = (total: number): JsonRecord => ({ total, generated_count: 0, inserted: 0, skipped_dups: 0, skipped_invalid: 0 });

export const triviaGenerated = (total: number): JsonRecord => ({ total, generated_count: total, inserted: 0, skipped_dups: 0, skipped_invalid: 0 });

export const triviaProgress = (counts: TriviaCounts): JsonRecord => ({ ...counts });

// ---- grading -------------------------------------------------------------------

export type GradeResult = 'right' | 'wrong';

export interface Verdict {
  result: GradeResult;
  feedback: string;
  model_answer_summary: string;
}

export const MODEL_ANSWER_SUMMARY_CHARS = 400;

/** Anything but a literal `right` grades wrong, and a missing summary falls
 * back to the model answer: the Go parser's two forgiving rules. */
export function coerceVerdict(value: unknown, modelAnswer: string): Verdict {
  if (!isDict(value)) throw new BadOutput('verdict is not an object');
  const summary = asString(value['model_answer_summary']);
  return {
    result: asString(value['result']) === 'right' ? 'right' : 'wrong',
    feedback: asString(value['feedback']),
    model_answer_summary: summary || truncate(modelAnswer, MODEL_ANSWER_SUMMARY_CHARS),
  };
}

export interface SrsState {
  step: number;
  next_due: string;
  interval_minutes: number;
}

export interface GradeAnswerResult {
  question_id: number;
  user_answer: string;
  idk: boolean;
  verdict: Verdict;
  state: SrsState;
}

export const gradeResult = (result: GradeAnswerResult): JsonRecord => ({ result: { ...result } });

// ---- transform --------------------------------------------------------------------

export interface CardModification extends GeneratedCard {
  question_id: number;
}

export interface CardAddition extends GeneratedCard {
  dest_deck?: string;
}

export interface NewDeckOp {
  name: string;
  deck_type: string;
  topic?: string;
  interval_minutes?: number;
}

export interface CardMoveOp {
  question_id: number;
  dest_deck: string;
}

export interface DeckRenameOp {
  deck_id: number;
  new_name: string;
}

export interface TransformPlan {
  scope: string;
  modifications: CardModification[];
  additions: CardAddition[];
  deletions: number[];
  new_decks: NewDeckOp[];
  card_moves: CardMoveOp[];
  deck_renames: DeckRenameOp[];
  deck_deletions: number[];
  notes?: string;
}

export interface TransformResult {
  modified_ids: number[];
  added_ids: number[];
  deleted_ids: number[];
  created_deck_ids: number[];
  renamed_deck_ids: number[];
  moved_card_ids: number[];
  deleted_deck_ids: number[];
}

export const emptyTransformResult = (): TransformResult => ({
  modified_ids: [],
  added_ids: [],
  deleted_ids: [],
  created_deck_ids: [],
  renamed_deck_ids: [],
  moved_card_ids: [],
  deleted_deck_ids: [],
});

export function coerceTransformPlan(value: unknown, scope: string): TransformPlan {
  if (!isDict(value)) throw new BadOutput('transform plan is not an object');
  const modifications: CardModification[] = [];
  for (const raw of dicts(value['modifications'])) {
    const qid = asInt(raw['question_id']);
    // A hallucinated or missing id has nothing to update; the apply would
    // skip it anyway, so it never reaches the preview.
    if (qid) modifications.push({ ...cardFields(raw), question_id: qid });
  }
  const additions: CardAddition[] = dicts(value['additions']).map((raw) => withOptional<CardAddition>(cardFields(raw), { dest_deck: asString(raw['dest_deck']) }));
  const newDecks: NewDeckOp[] = [];
  for (const raw of dicts(value['new_decks'])) {
    const name = asString(raw['name']).trim();
    if (name) {
      newDecks.push(
        withOptional<NewDeckOp>({ name, deck_type: asString(raw['deck_type']) || 'srs' }, { topic: asString(raw['topic']), interval_minutes: asInt(raw['interval_minutes']) }),
      );
    }
  }
  const moves: CardMoveOp[] = [];
  for (const raw of dicts(value['card_moves'])) {
    const qid = asInt(raw['question_id']);
    const dest = asString(raw['dest_deck']);
    if (qid && dest) moves.push({ question_id: qid, dest_deck: dest });
  }
  const renames: DeckRenameOp[] = [];
  for (const raw of dicts(value['deck_renames'])) {
    const deckId = asInt(raw['deck_id']);
    const name = asString(raw['new_name']).trim();
    if (deckId && name) renames.push({ deck_id: deckId, new_name: name });
  }
  return withOptional<TransformPlan>(
    {
      scope,
      modifications,
      additions,
      deletions: asInts(value['deletions']),
      new_decks: newDecks,
      card_moves: moves,
      deck_renames: renames,
      deck_deletions: asInts(value['deck_deletions']),
    },
    { notes: asString(value['notes']) },
  );
}

export const transformComputed = (plan: TransformPlan): JsonRecord => ({ plan: { ...plan } });

export const transformApplied = (result: TransformResult): JsonRecord => ({ result: { ...result } });
