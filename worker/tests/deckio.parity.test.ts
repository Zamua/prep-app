// Deck interchange against the Python corpus, both directions.
//
// `tests/parity/goldens/deckio/profiles.json` describes the decks; the Python
// oracle built them through its repositories and this builds the same rows
// through these, so a difference is the codec's and not the seed's.
//
// `.csv` and `.prepdeck` compare as bytes. `.apkg` cannot: Python's `csum` is
// `abs(hash(front))` under a per-process hash salt and its zip stamps the wall
// clock, so the gate is the canonical dump the oracle writes, with `csum` the
// one excluded column.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { beforeAll, describe, expect, it } from 'vitest';
import { questionsForExport, csvToDeck, deckToCsv } from '../app/api/deckIo.js';
import { ankiNotesToDeck } from '../app/decks/anki.js';
import { buildApkg } from '../app/decks/ankiExport.js';
import { deckToPrepdeck, prepdeckToDeck } from '../app/decks/archive.js';
import type { NewQuestion, QuestionType } from '../app/entities.js';
import type { UserRepos } from '../app/ports.js';
import { SqlJsApkg, sqlEngine } from '../runtime/adapters/apkg.js';
import { FflateZip } from '../runtime/adapters/zip.js';
import { cell, PARITY_NOW, USER } from './repos/setup.js';

const GOLDENS = join(new URL('..', import.meta.url).pathname, '..', 'tests', 'parity', 'goldens', 'deckio');
const golden = (rel: string): Uint8Array => new Uint8Array(readFileSync(join(GOLDENS, rel)));
const goldenJson = <T>(rel: string): T => JSON.parse(readFileSync(join(GOLDENS, rel), 'utf8')) as T;

interface CardSpec {
  type: string;
  topic: string | null;
  prompt: string;
  answer: string;
  choices: string[] | null;
  rubric: string | null;
  skeleton: string | null;
  language: string | null;
  answer_regex: string | null;
  explanation: string | null;
  state: Record<string, number | string | null> | null;
  reviews: { ts: string; result: string; user_answer: string; grader_notes: string }[];
  queue: { position: number; last_answered_at: string | null; last_answered_correctly: number | null } | null;
}

interface Profile {
  name: string;
  deck: {
    name: string;
    type: string;
    context_prompt: string | null;
    interval_minutes?: number;
    session_size?: number;
    desired_retention: number | null;
  };
  cards: CardSpec[];
}

interface ApkgSource {
  name: string;
  collection: string;
  notes: { id: number; flds: string }[];
}

const spec = goldenJson<{ profiles: Profile[]; apkg_sources: ApkgSource[] }>('profiles.json');

const zip = new FflateZip();
const apkg = new SqlJsApkg();
const EXPORTED_AT = '2026-03-14T15:00:00Z';
const NOW_MS = PARITY_NOW.getTime();

/** The shared description inserted through this app's repositories, in the
 * order the oracle inserts it, so question ids line up. */
function buildDeck(repos: UserRepos, profile: Profile): number {
  const d = profile.deck;
  let deckId: number;
  if (d.type === 'trivia') {
    deckId = repos.decks.createTrivia(d.name, { topic: d.context_prompt ?? '', intervalMinutes: d.interval_minutes ?? 30 });
    repos.decks.setTriviaSessionSize(deckId, d.session_size ?? 3);
  } else {
    deckId = repos.decks.create(d.name, { contextPrompt: d.context_prompt });
  }
  if (d.desired_retention !== null) repos.decks.setDesiredRetention(deckId, d.desired_retention);

  for (const c of profile.cards) {
    const question: NewQuestion = {
      type: c.type as QuestionType,
      topic: c.topic,
      prompt: c.prompt,
      answer: c.answer,
      choices: c.choices,
      rubric: c.rubric,
      skeleton: c.skeleton,
      language: c.language,
      answer_regex: c.answer_regex,
      explanation: c.explanation,
    };
    const qid = repos.questions.add(deckId, question);
    if (c.state) {
      repos.cards.restoreCardState(qid, {
        step: c.state['step'] as number,
        next_due: c.state['next_due'] as string,
        last_review: c.state['last_review'] as string,
        stability: c.state['stability'] as number,
        difficulty: c.state['difficulty'] as number,
        fsrs_state: c.state['fsrs_state'] as number,
      });
    }
    for (const r of c.reviews) repos.reviews.importReview(qid, r.ts, r.result as 'right' | 'wrong', r.user_answer, r.grader_notes);
    if (c.queue) {
      repos.trivia.importEntry(qid, c.queue.position, {
        lastAnsweredAt: c.queue.last_answered_at,
        lastAnsweredCorrectly: c.queue.last_answered_correctly,
      });
    }
  }
  return deckId;
}

/** The oracle's dump, rebuilt here: entry names in order, the media map, and
 * every collection table as `SELECT *` in id order without `csum`. */
async function dumpApkg(blob: Uint8Array): Promise<unknown> {
  const entries = zip.read(blob);
  const names = entries.map((e) => e.name);
  const collection = entries.find((e) => e.name === 'collection.anki21' || e.name === 'collection.anki2')!;
  const SQL = await sqlEngine();
  const db = new SQL.Database(collection.bytes);
  const tables: Record<string, Record<string, unknown>[]> = {};
  try {
    for (const table of ['col', 'notes', 'cards', 'revlog', 'graves']) {
      const result = db.exec(`SELECT * FROM ${table}`);
      const rows: Record<string, unknown>[] = [];
      if (result.length) {
        const columns = result[0]!.columns;
        for (const values of result[0]!.values) {
          const row: Record<string, unknown> = {};
          columns.forEach((c, i) => {
            row[c] = values[i] ?? null;
          });
          if (table === 'notes') delete row['csum'];
          if (table === 'col') for (const c of ['conf', 'models', 'decks', 'dconf', 'tags']) row[c] = JSON.parse(row[c] as string);
          rows.push(row);
        }
      }
      if (table !== 'col' && rows.length && 'id' in rows[0]!) rows.sort((a, b) => Number(a['id']) - Number(b['id']));
      tables[table] = rows;
    }
  } finally {
    db.close();
  }
  const media = entries.find((e) => e.name === 'media')!;
  return { entries: names, media: new TextDecoder().decode(media.bytes), tables };
}

/** What the oracle's `deck_rows` records, off this app's tables. */
function deckRows(c: ReturnType<typeof cell>, deckName: string): unknown {
  const sql = c.storage.sql;
  const deck = sql
    .exec(
      `SELECT id, name, deck_type, context_prompt, notification_interval_minutes, trivia_session_size, desired_retention
         FROM decks WHERE name = ?`,
      deckName,
    )
    .toArray()[0];
  if (!deck) return { deck: null, cards: [], state: [], reviews: [], queue: [] };
  const deckId = Number(deck['id']);
  const questions = sql
    .exec(
      `SELECT id, type, topic, prompt, answer, choices, rubric, skeleton, language, answer_regex, explanation, suspended
         FROM questions WHERE deck_id = ? ORDER BY id`,
      deckId,
    )
    .toArray();
  const promptOf = new Map(questions.map((q) => [Number(q['id']), q['prompt']]));
  const keyed = (rows: Record<string, unknown>[]): unknown[] =>
    rows.map((r) => {
      const { question_id, ...rest } = r;
      return { ...rest, prompt: promptOf.get(Number(question_id)) ?? null };
    });
  const state = sql
    .exec(
      `SELECT c.question_id, c.step, c.next_due, c.last_review, c.stability, c.difficulty, c.fsrs_state
         FROM cards c JOIN questions q ON q.id = c.question_id WHERE q.deck_id = ? ORDER BY c.question_id`,
      deckId,
    )
    .toArray();
  const reviews = sql
    .exec(
      `SELECT r.question_id, r.ts, r.result, r.user_answer, r.grader_notes FROM reviews r
         JOIN questions q ON q.id = r.question_id WHERE q.deck_id = ? ORDER BY r.id`,
      deckId,
    )
    .toArray();
  const queue = sql
    .exec(
      `SELECT t.question_id, t.queue_position, t.last_answered_at, t.last_answered_correctly FROM trivia_queue t
         JOIN questions q ON q.id = t.question_id WHERE q.deck_id = ? ORDER BY t.queue_position`,
      deckId,
    )
    .toArray();
  const { id: _id, ...deckFields } = deck;
  return {
    deck: deckFields,
    cards: questions.map(({ id: _qid, ...rest }) => rest),
    state: keyed(state),
    reviews: keyed(reviews),
    queue: keyed(queue),
  };
}

const decoder = new TextDecoder();

beforeAll(async () => {
  await sqlEngine();
});

describe('the export direction against the Python corpus', () => {
  for (const profile of spec.profiles) {
    it(`${profile.name}: csv and prepdeck are byte-identical, apkg dumps equal`, async () => {
      const c = cell();
      const deckId = buildDeck(c.repos, profile);

      expect(deckToCsv(c.repos, deckId)).toBe(decoder.decode(golden(`${profile.name}.csv`)));
      expect(deckToPrepdeck(c.repos, deckId, zip, EXPORTED_AT)).toEqual(golden(`${profile.name}.prepdeck`));

      const { col, notes, cards } = buildApkg(profile.deck.name, questionsForExport(c.repos, deckId), USER, NOW_MS, EXPORTED_AT.slice(0, 10));
      expect(await dumpApkg(await apkg.build(col, notes, cards))).toEqual(goldenJson(`${profile.name}.apkg.dump.json`));
    });
  }
});

describe('the import direction against the Python corpus', () => {
  for (const profile of spec.profiles) {
    it(`${profile.name}: csv, prepdeck and apkg read back to the same rows`, async () => {
      const c = cell();
      const deckId = buildDeck(c.repos, profile);
      const expected = goldenJson<Record<string, { outcome: unknown; rows: unknown }>>(`${profile.name}.import.json`);

      const csvOutcome = csvToDeck(c.repos, 'from-csv', deckToCsv(c.repos, deckId));
      expect(csvOutcome).toEqual(expected['csv']!.outcome);
      expect(deckRows(c, 'from-csv')).toEqual(expected['csv']!.rows);

      const prepdeckOutcome = prepdeckToDeck(c.repos, 'from-prepdeck', golden(`${profile.name}.prepdeck`), zip);
      expect(prepdeckOutcome).toEqual(expected['prepdeck']!.outcome);
      expect(deckRows(c, 'from-prepdeck')).toEqual(expected['prepdeck']!.rows);

      const { col, notes, cards } = buildApkg(profile.deck.name, questionsForExport(c.repos, deckId), USER, NOW_MS, EXPORTED_AT.slice(0, 10));
      const apkgOutcome = ankiNotesToDeck(c.repos, 'from-apkg', await apkg.notes(await apkg.build(col, notes, cards)));
      expect(apkgOutcome).toEqual(expected['apkg']!.outcome);
      expect(deckRows(c, 'from-apkg')).toEqual(expected['apkg']!.rows);
    });
  }

  for (const source of spec.apkg_sources) {
    it(`${source.name}: the generated collection imports the way Python read it`, async () => {
      const c = cell();
      const expected = goldenJson<Record<string, { outcome: unknown; rows: unknown }>>(`${source.name}.import.json`);
      const notes = await apkg.notes(golden(`${source.name}.apkg`));
      expect(notes).toEqual(source.notes);
      expect(ankiNotesToDeck(c.repos, 'from-apkg', notes)).toEqual(expected['apkg']!.outcome);
      expect(deckRows(c, 'from-apkg')).toEqual(expected['apkg']!.rows);
    });
  }
});
