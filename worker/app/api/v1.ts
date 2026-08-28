// The public REST surface at /api/v1. Bearer-only; the router applies the
// gate.
import type { Question } from '../entities.js';
import { detail, json, type ApiResult } from '../http.js';
import type { DeckRepo, QuestionRepo, TriviaRepo } from '../ports.js';
import { RequestValidationError, missing, stringTooLong, stringTooShort, stringType } from '../validation.js';
import { csvToDeck, deckToCsv, questionsForExport, type DeckIoRepos } from './deckIo.js';

const NAME_MIN = 2;
const NAME_MAX = 30;

export interface V1Repos extends DeckIoRepos {
  decks: DeckRepo;
  questions: QuestionRepo;
  trivia: TriviaRepo;
}

/** The card fields the JSON view and the CSV exporter agree on. */
export function cardJson(q: Question): Record<string, unknown> {
  return {
    type: q.type,
    topic: q.topic,
    prompt: q.prompt,
    answer: q.answer,
    choices: q.choices,
    rubric: q.rubric,
    skeleton: q.skeleton,
    language: q.language,
    answer_regex: q.answer_regex,
    explanation: q.explanation,
  };
}

export function listDecks(repos: V1Repos): ApiResult {
  const decks = repos.decks.listSummaries().map((s) => ({
    name: s.name,
    type: s.deck_type || 'srs',
    card_count: s.total,
    due: s.due,
    pinned: s.pinned,
  }));
  return json({ decks });
}

interface NewDeckBody {
  name: string;
  context_prompt: string | null;
}

/** `_NewDeckBody`: name 2..30, an optional context prompt. */
function parseNewDeck(body: unknown): NewDeckBody {
  const errors = [];
  const obj = (body ?? {}) as Record<string, unknown>;
  const raw = obj['name'];
  if (raw === undefined || raw === null) errors.push(missing(['body', 'name'], body));
  else if (typeof raw !== 'string') errors.push(stringType(['body', 'name'], raw));
  else if (raw.length < NAME_MIN) errors.push(stringTooShort(['body', 'name'], raw, NAME_MIN));
  else if (raw.length > NAME_MAX) errors.push(stringTooLong(['body', 'name'], raw, NAME_MAX));
  const prompt = obj['context_prompt'];
  if (prompt !== undefined && prompt !== null && typeof prompt !== 'string') errors.push(stringType(['body', 'context_prompt'], prompt));
  if (errors.length) throw new RequestValidationError(errors);
  return { name: raw as string, context_prompt: (prompt as string | null | undefined) ?? null };
}

export function createDeck(repos: V1Repos, body: unknown): ApiResult {
  const parsed = parseNewDeck(body);
  if (repos.decks.findId(parsed.name) !== null) return detail(409, `deck '${parsed.name}' already exists`);
  const deckId = repos.decks.create(parsed.name, { contextPrompt: parsed.context_prompt });
  return json({ name: parsed.name, id: deckId });
}

export function deckMeta(repos: V1Repos, name: string): ApiResult {
  const deckId = repos.decks.findId(name);
  if (deckId === null) return detail(404, 'deck not found');
  const meta = repos.decks.getMeta(deckId);
  return json({
    name,
    type: repos.decks.getType(deckId) || 'srs',
    context_prompt: meta.context_prompt,
    card_count: repos.questions.listInDeck(deckId).length,
  });
}

export function listCards(repos: V1Repos, name: string): ApiResult {
  const deckId = repos.decks.findId(name);
  if (deckId === null) return detail(404, 'deck not found');
  return json({ deck: name, cards: questionsForExport(repos, deckId).map(cardJson) });
}

export function exportCsv(repos: V1Repos, name: string): ApiResult {
  const deckId = repos.decks.findId(name);
  if (deckId === null) return detail(404, 'deck not found');
  return {
    text: deckToCsv(repos, deckId),
    status: 200,
    headers: { 'content-type': 'text/csv; charset=utf-8', 'content-disposition': `attachment; filename="${name}.csv"` },
  };
}

export function importCsv(repos: V1Repos, name: string, csvText: string): ApiResult {
  if (!csvText.trim()) return detail(400, 'empty CSV body');
  return json(csvToDeck(repos, name, csvText));
}
