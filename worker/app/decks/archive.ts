// The `.prepdeck` archive: a zip of meta.json plus cards.csv, reviews.csv
// and, for a trivia deck, trivia_queue.csv. Unlike the plain CSV it carries
// FSRS state, the review log and the queue, so an import restores a deck
// rather than appending to one. `prompt` is the join key across the three
// CSVs because a restore assigns fresh question ids.
import { literal, literalList } from '../../domain/grading/literal.js';
import { parseDict, writeRow } from '../api/csv.js';
import { CSV_COLUMNS, questionToRow, questionsForExport, type DeckIoRepos } from '../api/deckIo.js';
import type { NewQuestion, QuestionType, ReviewResult } from '../entities.js';
import { NotAZip, type CardRepo, type ReviewRepo, type UserRepos, type ZipCodec, type ZipEntry } from '../ports.js';
import { rowCapMessage } from './importLimits.js';

export const FORMAT_VERSION = 1;

/** Appended to `CSV_COLUMNS` in cards.csv; a never-reviewed card has empty
 * cells, and the reader tolerates them. */
const CARD_STATE_COLUMNS = ['step', 'next_due', 'last_review', 'stability', 'difficulty', 'fsrs_state'] as const;

const TRIVIA_QUEUE_COLUMNS = ['queue_position', 'last_answered_at', 'last_answered_correctly'] as const;

const REVIEW_COLUMNS = ['prompt', 'ts', 'result', 'user_answer', 'grader_notes'] as const;

const VALID_NAME = /^[a-z0-9][a-z0-9-]{1,29}$/;
const VALID_NAME_SOURCE = '^[a-z0-9][a-z0-9-]{1,29}$';

const QUESTION_TYPES: readonly string[] = ['code', 'mcq', 'multi', 'short'];

const REQUIRED_ENTRIES = ['cards.csv', 'meta.json', 'reviews.csv'] as const;

/** Every name the reader inflates. Anything else the archive carries stays
 * compressed, the way one `zf.read(name)` per entry leaves it. */
const ARCHIVE_ENTRIES = [...REQUIRED_ENTRIES, 'trivia_queue.csv'] as const;

interface ArchiveMeta {
  format_version: number;
  exported_at: string;
  deck: {
    name: string | null;
    deck_type: string;
    context_prompt: string | null;
    notification_interval_minutes: number | null;
    trivia_session_size: number;
    desired_retention: number | null;
  };
}

const enc = new TextEncoder();
const dec = new TextDecoder('utf-8');

// ---- export ---------------------------------------------------------------

function buildMeta(repos: UserRepos, deckId: number, exportedAt: string): ArchiveMeta {
  const deckType = repos.decks.getType(deckId);
  if (deckType === null) throw new Error(`deck ${deckId} not found`);
  const meta = repos.decks.getMeta(deckId);
  return {
    format_version: FORMAT_VERSION,
    exported_at: exportedAt,
    deck: {
      name: repos.decks.findName(deckId),
      deck_type: deckType,
      context_prompt: meta.context_prompt || null,
      notification_interval_minutes: meta.interval_minutes,
      trivia_session_size: Math.trunc(meta.session_size || 3),
      desired_retention: repos.decks.getDesiredRetention(deckId),
    },
  };
}

/** A number column; an unset one is an empty cell the reader tolerates. */
const numCell = (v: number | null | undefined): string => (v === null || v === undefined ? '' : String(v));

function cardsCsv(repos: DeckIoRepos & { cards: CardRepo }, deckId: number): string {
  const questions = questionsForExport(repos, deckId);
  const stateByPrompt = new Map(repos.cards.listCardStateForDeck(deckId).map((r) => [r.prompt, r]));

  const header = [...CSV_COLUMNS, ...CARD_STATE_COLUMNS];
  let out = writeRow(header);
  for (const q of questions) {
    const s = stateByPrompt.get(q.prompt);
    out += writeRow([
      ...questionToRow(q),
      numCell(s?.step),
      s?.next_due || '',
      s?.last_review || '',
      numCell(s?.stability),
      numCell(s?.difficulty),
      numCell(s?.fsrs_state),
    ]);
  }
  return out;
}

function reviewsCsv(reviews: ReviewRepo, deckId: number): string {
  let out = writeRow(REVIEW_COLUMNS);
  for (const r of reviews.listReviewsForDeck(deckId)) {
    out += writeRow([r.prompt, r.ts, r.result, r.user_answer || '', r.grader_notes || '']);
  }
  return out;
}

function triviaQueueCsv(repos: UserRepos, deckId: number): string {
  let out = writeRow(['prompt', ...TRIVIA_QUEUE_COLUMNS]);
  for (const r of repos.trivia.listQueueForDeck(deckId)) {
    out += writeRow([
      r.prompt,
      String(r.queue_position),
      r.last_answered_at || '',
      r.last_answered_correctly === null ? '' : String(Number(r.last_answered_correctly)),
    ]);
  }
  return out;
}

/** meta.json, cards.csv, reviews.csv, then trivia_queue.csv for a trivia
 * deck. Readers take entries by name, so the order is a convention. */
export function deckToPrepdeck(repos: UserRepos, deckId: number, zip: ZipCodec, exportedAt: string): Uint8Array {
  const meta = buildMeta(repos, deckId, exportedAt);
  const entries: ZipEntry[] = [
    { name: 'meta.json', bytes: enc.encode(JSON.stringify(meta, null, 2) + '\n') },
    { name: 'cards.csv', bytes: enc.encode(cardsCsv(repos, deckId)) },
    { name: 'reviews.csv', bytes: enc.encode(reviewsCsv(repos.reviews, deckId)) },
  ];
  if (repos.decks.getType(deckId) === 'trivia') {
    entries.push({ name: 'trivia_queue.csv', bytes: enc.encode(triviaQueueCsv(repos, deckId)) });
  }
  return zip.write(entries);
}

// ---- import ---------------------------------------------------------------

/** Mirrors `ImportOutcome` with the counters a full restore adds. */
export interface PrepdeckImportOutcome {
  deck_id: number;
  deck_name: string;
  inserted: number;
  skipped_duplicates: number;
  reviews_inserted: number;
  queue_rows_inserted: number;
  errors: string[];
}

const fail = (deckName: string, error: string): PrepdeckImportOutcome => ({
  deck_id: 0,
  deck_name: deckName,
  inserted: 0,
  skipped_duplicates: 0,
  reviews_inserted: 0,
  queue_rows_inserted: 0,
  errors: [error],
});

const cell = (row: Record<string, string | null>, name: string): string => (row[name] ?? '').trim();

/**
 * Restore an archive into a deck named `deckName`, which must not exist:
 * merging a full state restore into a deck with its own FSRS state and
 * review log has no defensible answer, so the caller picks a fresh name.
 */
export function prepdeckToDeck(
  repos: UserRepos,
  deckName: string,
  blob: Uint8Array,
  zip: ZipCodec,
  opts: { rowCap?: number; reviewRowCap?: number; maxEntryBytes?: number; maxTotalBytes?: number } = {},
): PrepdeckImportOutcome {
  const errors: string[] = [];

  if (!VALID_NAME.test(deckName)) return fail(deckName, `invalid deck name ${literal(deckName)}; must match ${VALID_NAME_SOURCE}`);
  if (repos.decks.findId(deckName) !== null) {
    return fail(
      deckName,
      `deck ${literal(deckName)} already exists. Pick a fresh name — .prepdeck imports restore full state, which would merge awkwardly with existing cards.`,
    );
  }

  let entries: ZipEntry[];
  try {
    entries = zip.read(blob, { only: ARCHIVE_ENTRIES, maxEntryBytes: opts.maxEntryBytes, maxTotalBytes: opts.maxTotalBytes });
  } catch (e) {
    if (e instanceof NotAZip) return fail(deckName, `not a valid zip: ${e.message}`);
    throw e;
  }
  const byName = new Map(entries.map((e) => [e.name, e.bytes]));
  const missing = REQUIRED_ENTRIES.filter((n) => !byName.has(n));
  if (missing.length) return fail(deckName, `archive is missing required entries: ${literalList([...missing])}`);

  let meta: Record<string, unknown>;
  try {
    meta = JSON.parse(dec.decode(byName.get('meta.json')!)) as Record<string, unknown>;
  } catch (e) {
    return fail(deckName, `meta.json is not valid JSON: ${e instanceof Error ? e.message : String(e)}`);
  }

  const version = Math.trunc(Number(meta['format_version'] || 0)) || 0;
  if (version > FORMAT_VERSION) {
    return fail(
      deckName,
      `.prepdeck format_version ${version} is newer than this prep build supports (${FORMAT_VERSION}). Upgrade prep or re-export from the source deploy at an older format.`,
    );
  }
  if (version < 1) return fail(deckName, `.prepdeck format_version ${version} is unknown; must be ≥ 1`);

  const deckMeta = (meta['deck'] as Record<string, unknown> | undefined) ?? {};
  const declaredType = String(deckMeta['deck_type'] ?? 'srs').toLowerCase();

  let deckId: number;
  if (declaredType === 'trivia') {
    const interval = Math.trunc(Number(deckMeta['notification_interval_minutes'] || 30)) || 30;
    deckId = repos.decks.createTrivia(deckName, { topic: String(deckMeta['context_prompt'] || ''), intervalMinutes: interval });
    if (deckMeta['trivia_session_size']) {
      const size = Math.trunc(Number(deckMeta['trivia_session_size']));
      if (Number.isFinite(size)) {
        try {
          repos.decks.setTriviaSessionSize(deckId, size);
        } catch (e) {
          if (!(e instanceof RangeError)) throw e;
        }
      }
    }
  } else {
    deckId = repos.decks.create(deckName, { contextPrompt: (deckMeta['context_prompt'] as string | null) ?? null });
  }

  if (deckMeta['desired_retention'] !== null && deckMeta['desired_retention'] !== undefined) {
    const retention = Number(deckMeta['desired_retention']);
    if (Number.isFinite(retention)) {
      try {
        repos.decks.setDesiredRetention(deckId, retention);
      } catch (e) {
        if (!(e instanceof RangeError)) throw e;
      }
    }
  }

  const { inserted, skippedDuplicates, qidByPrompt } = importCards(repos, deckId, declaredType, dec.decode(byName.get('cards.csv')!), errors, opts.rowCap);
  const reviewsInserted = importReviews(repos, dec.decode(byName.get('reviews.csv')!), qidByPrompt, errors, opts.reviewRowCap);

  let queueRowsInserted = 0;
  if (declaredType === 'trivia') {
    const queue = byName.get('trivia_queue.csv');
    if (queue) {
      queueRowsInserted = importTriviaQueue(repos, dec.decode(queue), qidByPrompt, errors, opts.rowCap);
    } else {
      // An archive written before the queue section: rebuild in cards.csv
      // order so the deck is at least studyable, and say what was lost.
      for (const qid of qidByPrompt.values()) {
        repos.trivia.appendCard(qid, deckId);
        queueRowsInserted++;
      }
      errors.push('trivia_queue.csv was missing — queue rebuilt from cards.csv order; per-card answered state was lost.');
    }
  }

  return {
    deck_id: deckId,
    deck_name: deckName,
    inserted,
    skipped_duplicates: skippedDuplicates,
    reviews_inserted: reviewsInserted,
    queue_rows_inserted: queueRowsInserted,
    errors,
  };
}

function importCards(
  repos: UserRepos,
  deckId: number,
  declaredType: string,
  csvText: string,
  errors: string[],
  rowCap: number | undefined,
): { inserted: number; skippedDuplicates: number; qidByPrompt: Map<string, number> } {
  const { rows } = parseDict(csvText);
  const qidByPrompt = new Map<string, number>();
  let inserted = 0;
  let skippedDuplicates = 0;
  const cap = rowCap ?? Infinity;

  for (let index = 0; index < rows.length; index++) {
    if (index >= cap) {
      errors.push(rowCapMessage(cap));
      break;
    }
    const row = rows[index]!;
    const i = index + 2; // row 1 is the header
    const prompt = cell(row, 'prompt');
    if (!prompt) {
      errors.push(`cards.csv row ${i}: missing prompt`);
      continue;
    }
    if (qidByPrompt.has(prompt)) {
      skippedDuplicates++;
      continue;
    }
    // The fallback reads the raw cell, so a whitespace-only one stays truthy
    // and strips to a type no `QuestionType` names.
    const typeRaw = ((row['type'] ?? '') || 'short').trim().toLowerCase();
    if (!QUESTION_TYPES.includes(typeRaw)) {
      errors.push(`cards.csv row ${i}: unknown type ${literal(row['type'] ?? null)}`);
      continue;
    }
    const answer = cell(row, 'answer');
    if (!answer) {
      errors.push(`cards.csv row ${i}: missing answer`);
      continue;
    }
    const choices = cell(row, 'choices')
      .split(/\r\n|\r|\n/)
      .map((s) => s.trim())
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
    let qid: number;
    try {
      qid = repos.questions.add(deckId, question);
    } catch (e) {
      errors.push(`cards.csv row ${i}: write failed — ${e instanceof Error ? e.message : String(e)}`);
      continue;
    }
    qidByPrompt.set(prompt, qid);
    inserted++;
    if (declaredType === 'srs') restoreCardState(repos, qid, row, errors, i);
  }
  return { inserted, skippedDuplicates, qidByPrompt };
}

/** Empty cells stay at the post-insert defaults; a malformed one is reported
 * and skipped rather than sinking the row. */
function restoreCardState(repos: UserRepos, qid: number, row: Record<string, string | null>, errors: string[], rowNum: number): void {
  const num = (name: string, integral: boolean): number | null => {
    const v = cell(row, name);
    if (!v) return null;
    const parsed = integral ? (/^[+-]?\d+$/.test(v) ? Number(v) : NaN) : Number(v);
    if (!Number.isFinite(parsed)) {
      errors.push(`cards.csv row ${rowNum}: bad ${name}=${literal(v)}`);
      return null;
    }
    return parsed;
  };
  const text = (name: string): string | undefined => cell(row, name) || undefined;
  repos.cards.restoreCardState(qid, {
    step: num('step', true) ?? undefined,
    next_due: text('next_due'),
    last_review: text('last_review'),
    stability: num('stability', false) ?? undefined,
    difficulty: num('difficulty', false) ?? undefined,
    fsrs_state: num('fsrs_state', true) ?? undefined,
  });
}

function importReviews(repos: UserRepos, csvText: string, qidByPrompt: Map<string, number>, errors: string[], rowCap: number | undefined): number {
  const { rows } = parseDict(csvText);
  const cap = rowCap ?? Infinity;
  let inserted = 0;
  for (let index = 0; index < rows.length; index++) {
    if (index >= cap) {
      errors.push(`reviews.csv: ${rowCapMessage(cap)}`);
      break;
    }
    const row = rows[index]!;
    const i = index + 2;
    const prompt = cell(row, 'prompt');
    if (!prompt) {
      errors.push(`reviews.csv row ${i}: missing prompt`);
      continue;
    }
    const qid = qidByPrompt.get(prompt);
    if (qid === undefined) {
      errors.push(`reviews.csv row ${i}: prompt ${literal(prompt.slice(0, 40))} not found in deck`);
      continue;
    }
    const result = cell(row, 'result').toLowerCase();
    if (result !== 'right' && result !== 'wrong') {
      errors.push(`reviews.csv row ${i}: bad result ${literal(result)}`);
      continue;
    }
    try {
      repos.reviews.importReview(qid, row['ts'] || '', result as ReviewResult, row['user_answer'] || '', row['grader_notes'] || '');
      inserted++;
    } catch (e) {
      errors.push(`reviews.csv row ${i}: write failed — ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  return inserted;
}

function importTriviaQueue(repos: UserRepos, csvText: string, qidByPrompt: Map<string, number>, errors: string[], rowCap: number | undefined): number {
  const { rows } = parseDict(csvText);
  const pos = (row: Record<string, string | null>): number => {
    const raw = (row['queue_position'] ?? '').trim() || '0';
    return /^[+-]?\d+$/.test(raw) ? Number(raw) : 0;
  };
  // Sorted so the rotation order survives, then numbered from row 2 the way
  // an enumerate over the sorted list does.
  const sorted = [...rows].sort((a, b) => pos(a) - pos(b));
  // A queue row names a card, and the cards are capped, so the lowest
  // positions up to the same cap are every row that could resolve.
  const cap = rowCap ?? Infinity;
  let inserted = 0;
  for (let index = 0; index < sorted.length; index++) {
    if (index >= cap) {
      errors.push(`trivia_queue.csv: ${rowCapMessage(cap)}`);
      break;
    }
    const row = sorted[index]!;
    const i = index + 2;
    const prompt = cell(row, 'prompt');
    const qid = qidByPrompt.get(prompt);
    if (qid === undefined) {
      errors.push(`trivia_queue.csv row ${i}: prompt ${literal(prompt.slice(0, 40))} not in deck`);
      continue;
    }
    const rawPos = (row['queue_position'] ?? '').trim() || '0';
    if (!/^[+-]?\d+$/.test(rawPos)) {
      errors.push(`trivia_queue.csv row ${i}: bad queue_position`);
      continue;
    }
    const lacRaw = (row['last_answered_correctly'] ?? '').trim();
    let lastAnsweredCorrectly: number | null = null;
    if (lacRaw !== '') {
      if (!/^[+-]?\d+$/.test(lacRaw)) {
        errors.push(`trivia_queue.csv row ${i}: bad last_answered_correctly=${literal(lacRaw)}`);
        continue;
      }
      lastAnsweredCorrectly = Number(lacRaw);
    }
    try {
      repos.trivia.importEntry(qid, Number(rawPos), { lastAnsweredAt: row['last_answered_at'] || null, lastAnsweredCorrectly });
      inserted++;
    } catch (e) {
      errors.push(`trivia_queue.csv row ${i}: write failed — ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  return inserted;
}
