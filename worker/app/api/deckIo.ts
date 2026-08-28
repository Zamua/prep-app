// CSV import/export for decks. One CSV per deck; trivia decks carry a
// `# key: value` preamble above the header so the importer can rebuild the
// deck shape.
import type { NewQuestion, Question, QuestionType } from '../entities.js';
import type { DeckRepo, QuestionRepo, TriviaRepo } from '../ports.js';
import { rowCapMessage } from '../decks/importLimits.js';
import { parseCsvRecords, writeRow } from './csv.js';

export const CSV_COLUMNS = ['type', 'topic', 'prompt', 'answer', 'choices', 'rubric', 'skeleton', 'language', 'answer_regex', 'explanation'] as const;

const QUESTION_TYPES: readonly string[] = ['code', 'mcq', 'multi', 'short'];

export interface ImportOutcome {
  deck_id: number;
  deck_name: string;
  inserted: number;
  skipped_duplicates: number;
  errors: string[];
}

export interface DeckIoRepos {
  decks: DeckRepo;
  questions: QuestionRepo;
  trivia: TriviaRepo;
}

/** Full questions in id order: the CSV and the JSON card list agree on the field set. */
export function questionsForExport(repos: DeckIoRepos, deckId: number): Question[] {
  const ids = repos.questions
    .listInDeck(deckId)
    .map((c) => c.id)
    .sort((a, b) => a - b);
  const out: Question[] = [];
  for (const id of ids) {
    const q = repos.questions.get(id);
    if (q) out.push(q);
  }
  return out;
}

export function questionToRow(q: Question): string[] {
  const cell: Record<string, string> = {
    type: q.type,
    topic: q.topic || '',
    prompt: q.prompt,
    answer: q.answer,
    choices: q.choices && q.choices.length ? q.choices.join('\n') : '',
    rubric: q.rubric || '',
    skeleton: q.skeleton || '',
    language: q.language || '',
    answer_regex: q.answer_regex || '',
    explanation: q.explanation || '',
  };
  return CSV_COLUMNS.map((c) => cell[c] ?? '');
}

/** Only trivia decks carry deck-level state worth preserving in the file. */
function buildPreamble(repos: DeckIoRepos, deckId: number): string {
  if (repos.decks.getType(deckId) !== 'trivia') return '';
  const meta = repos.decks.getMeta(deckId);
  const lines = ['# deck_type: trivia'];
  if (meta.interval_minutes !== null) lines.push(`# notification_interval_minutes: ${Math.trunc(meta.interval_minutes)}`);
  lines.push(`# trivia_session_size: ${Math.trunc(meta.session_size)}`);
  if (meta.context_prompt) {
    const topic = meta.context_prompt.split('\n').join(' ').split('\r').join(' ').trim();
    lines.push(`# topic_prompt: ${topic}`);
  }
  return lines.join('\n') + '\n';
}

export function deckToCsv(repos: DeckIoRepos, deckId: number): string {
  let out = buildPreamble(repos, deckId);
  out += writeRow(CSV_COLUMNS);
  for (const q of questionsForExport(repos, deckId)) out += writeRow(questionToRow(q));
  return out;
}

/** Leading `# key: value` lines, keys lowercased and values stripped. */
export function splitPreamble(csvText: string): { preamble: Record<string, string>; rest: string } {
  const preamble: Record<string, string> = {};
  // A trailing newline ends the last line rather than opening an empty one.
  const lines = csvText.split(/\r\n|\r|\n/);
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  let i = 0;
  while (i < lines.length) {
    const stripped = lines[i]!.trim();
    if (!stripped) {
      i++;
      continue;
    }
    if (!stripped.startsWith('#')) break;
    const kv = stripped.slice(1).trim();
    const at = kv.indexOf(':');
    if (at >= 0) preamble[kv.slice(0, at).trim().toLowerCase()] = kv.slice(at + 1).trim();
    i++;
  }
  return { preamble, rest: lines.slice(i).join('\n') };
}

/** A preamble value as a whole number: the whole string, sign allowed. */
function preambleInt(raw: string | undefined, fallback: number): number {
  const s = (raw ?? '').trim();
  if (!/^[+-]?\d+$/.test(s)) return fallback;
  return Number(s);
}

const cell = (row: Record<string, string | null>, name: string): string => (row[name] ?? '').trim();

export function csvToDeck(
  repos: DeckIoRepos,
  deckName: string,
  csvText: string,
  opts: { contextPrompt?: string | null; rowCap?: number } = {},
): ImportOutcome {
  const { preamble, rest } = splitPreamble(csvText);
  const declaredType = (preamble['deck_type'] || 'srs').toLowerCase();
  const contextPrompt = opts.contextPrompt ?? null;

  let deckId: number;
  const existingId = repos.decks.findId(deckName);
  if (existingId !== null) {
    const existingType = repos.decks.getType(existingId) || 'srs';
    if (existingType !== declaredType) {
      return {
        deck_id: existingId,
        deck_name: deckName,
        inserted: 0,
        skipped_duplicates: 0,
        errors: [
          `deck '${deckName}' already exists as '${existingType}'; CSV declares '${declaredType}'. ` +
            'Pick a different name or import into a fresh deck.',
        ],
      };
    }
    deckId = existingId;
  } else if (declaredType === 'trivia') {
    const interval = preambleInt(preamble['notification_interval_minutes'] ?? '30', 30);
    const topic = preamble['topic_prompt'] || contextPrompt || '';
    deckId = repos.decks.createTrivia(deckName, { topic, intervalMinutes: interval });
    if ('trivia_session_size' in preamble) {
      const size = preambleInt(preamble['trivia_session_size'], NaN);
      if (Number.isFinite(size)) {
        try {
          repos.decks.setTriviaSessionSize(deckId, size);
        } catch (e) {
          if (!(e instanceof RangeError)) throw e;
        }
      }
    }
  } else {
    deckId = repos.decks.getOrCreate(deckName);
  }

  if (contextPrompt && declaredType !== 'trivia' && !repos.decks.getContextPrompt(deckName)) {
    repos.decks.updateContextPrompt(deckName, contextPrompt);
  }

  const existing = new Set(repos.questions.promptsInDeck(deckId));
  let inserted = 0;
  let skippedDuplicates = 0;
  const errors: string[] = [];

  const { fieldnames, rows } = parseCsvRecords(rest);
  if (!fieldnames) {
    return { deck_id: deckId, deck_name: deckName, inserted: 0, skipped_duplicates: 0, errors: ['CSV has no header row'] };
  }

  const cap = opts.rowCap ?? Infinity;
  for (let index = 0; index < rows.length; index++) {
    if (index >= cap) {
      errors.push(rowCapMessage(cap));
      break;
    }
    const row = rows[index]!;
    const i = index + 2; // row 1 is the header
    const prompt = cell(row, 'prompt');
    if (!prompt) {
      errors.push(`row ${i}: missing prompt`);
      continue;
    }
    if (existing.has(prompt)) {
      skippedDuplicates++;
      continue;
    }
    const typeRaw = cell(row, 'type').toLowerCase() || 'short';
    if (!QUESTION_TYPES.includes(typeRaw)) {
      errors.push(`row ${i}: unknown type '${typeRaw}'`);
      continue;
    }
    const answer = cell(row, 'answer');
    if (!answer) {
      errors.push(`row ${i}: missing answer`);
      continue;
    }
    const choices = cell(row, 'choices')
      .split(/\r\n|\r|\n/)
      .map((ln) => ln.trim())
      .filter(Boolean);
    const question: NewQuestion = {
      type: typeRaw as QuestionType,
      topic: cell(row, 'topic') || null,
      prompt,
      answer,
      choices: choices.length ? choices : null,
      rubric: cell(row, 'rubric') || null,
      skeleton: cell(row, 'skeleton') || null,
      language: cell(row, 'language') || null,
      answer_regex: cell(row, 'answer_regex') || null,
      explanation: cell(row, 'explanation') || null,
    };
    try {
      const qid = repos.questions.add(deckId, question);
      existing.add(prompt);
      inserted++;
      if (declaredType === 'trivia') repos.trivia.appendCard(qid, deckId);
    } catch (e) {
      // A failed row is reported and the import continues; one bad row
      // must not cost the user the other several hundred.
      errors.push(`row ${i}: write failed \u2014 ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  return { deck_id: deckId, deck_name: deckName, inserted, skipped_duplicates: skippedDuplicates, errors };
}
