// What the model is shown: the owner's cards and decks, read once at the start
// and carried in the job input. An LLM step runs in the JobCell, which holds
// the agent and no repositories, so the picture travels with the job; it also
// pins what the user reviewed to what the model saw across a retry.
//
// Field ORDER and the dropped-when-empty fields are the Go `cardForTransform`
// and `deckForTransform` struct tags, because this JSON is prompt bytes and
// the free-tier stub keys its canned replies on them: a reordered or extra key
// is a different message.

export const SCOPES = ['card', 'deck', 'reorganize'] as const;
export type TransformScope = (typeof SCOPES)[number];

export interface TransformCard {
  question_id: number;
  type: string;
  topic?: string;
  prompt: string;
  choices?: string[];
  answer: string;
  rubric?: string;
  skeleton?: string;
  language?: string;
  explanation?: string;
  answer_regex?: string;
}

export interface TransformDeck {
  id: number;
  name: string;
  deck_type: string;
  topic?: string;
  interval_minutes?: number;
  cards: TransformCard[];
}

/** The four columns the grade prompt interpolates. Plain text, not JSON, so
 * only the values matter here. */
export interface GradeCard {
  type: string;
  prompt: string;
  answer: string;
  rubric: string;
}

type Loose = Readonly<Record<string, unknown>>;

const asString = (v: unknown): string => (typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v));
const asInt = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : 0);
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);

/** Assigns only a non-empty value, at the position the call appears: the
 * `omitempty` half of a shape, where order is part of the contract. */
function put(target: Record<string, unknown>, key: string, value: string | number | string[]): void {
  if (value === '' || value === 0 || (Array.isArray(value) && value.length === 0)) return;
  target[key] = value;
}

export function transformCard(raw: Loose): TransformCard {
  const out: Record<string, unknown> = {};
  out['question_id'] = asInt(raw['question_id']);
  out['type'] = asString(raw['type']);
  put(out, 'topic', asString(raw['topic']));
  out['prompt'] = asString(raw['prompt']);
  put(out, 'choices', strings(raw['choices']));
  out['answer'] = asString(raw['answer']);
  put(out, 'rubric', asString(raw['rubric']));
  put(out, 'skeleton', asString(raw['skeleton']));
  put(out, 'language', asString(raw['language']));
  put(out, 'explanation', asString(raw['explanation']));
  put(out, 'answer_regex', asString(raw['answer_regex']));
  return out as unknown as TransformCard;
}

export function transformDeck(raw: Loose, cards: readonly TransformCard[]): TransformDeck {
  const out: Record<string, unknown> = {};
  out['id'] = asInt(raw['id']);
  out['name'] = asString(raw['name']);
  out['deck_type'] = asString(raw['deck_type']) || 'srs';
  put(out, 'topic', asString(raw['topic']));
  put(out, 'interval_minutes', asInt(raw['interval_minutes']));
  // Never nil: Go replaces a nil slice so an empty deck reads as `[]`.
  out['cards'] = [...cards];
  return out as unknown as TransformDeck;
}

export function gradeCard(raw: Loose): GradeCard {
  return { type: asString(raw['type']), prompt: asString(raw['prompt']), answer: asString(raw['answer']), rubric: asString(raw['rubric']) };
}
