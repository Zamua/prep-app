// An archive a user exported from an earlier build still imports.
//
// `tests/fixtures/legacy/` holds `.prepdeck` bytes no writer in this tree can
// produce: meta.json escapes non-ASCII and the float cells carry six
// significant digits. The `.apkg` pair is hand-built rather than exported from
// Anki, and holds a bare `notes` table under each of the two collection names
// the reader looks for, since that one query is all the reader runs. The
// readers are the compatibility surface, so this pins what they accept, not
// what the exporters emit.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { ankiNotesToDeck } from '../app/decks/anki.js';
import { prepdeckToDeck } from '../app/decks/archive.js';
import type { UserRepos } from '../app/ports.js';
import { SqlJsApkg } from '../runtime/adapters/apkg.js';
import { FflateZip } from '../runtime/adapters/zip.js';
import { cell } from './repos/setup.js';

const zip = new FflateZip();
const apkg = new SqlJsApkg();
const LEGACY = join(new URL('.', import.meta.url).pathname, 'fixtures', 'legacy');
const fixture = (name: string): Uint8Array => new Uint8Array(readFileSync(join(LEGACY, name)));

const rows = (c: ReturnType<typeof cell>, sql: string, ...args: unknown[]): Record<string, unknown>[] =>
  c.storage.sql.exec(sql, ...args).toArray();

const cardsOf = (c: ReturnType<typeof cell>, deckId: number): Record<string, unknown>[] =>
  rows(
    c,
    `SELECT q.prompt, q.type, q.topic, q.answer, q.choices, q.rubric, q.skeleton, q.language, q.answer_regex, q.explanation,
            cards.step, cards.next_due, cards.last_review, cards.stability, cards.difficulty, cards.fsrs_state
       FROM questions q LEFT JOIN cards ON cards.question_id = q.id WHERE q.deck_id = ? ORDER BY q.id`,
    deckId,
  );

const importPrepdeck = (name: string): { c: ReturnType<typeof cell>; outcome: ReturnType<typeof prepdeckToDeck> } => {
  const c = cell();
  return { c, outcome: prepdeckToDeck(c.repos as UserRepos, 'restored', fixture(name), zip) };
};

describe('a .prepdeck exported by an earlier build restores', () => {
  it('srs-mixed: every question type, its choices and its rubric', () => {
    const { c, outcome } = importPrepdeck('srs-mixed.prepdeck');
    expect(outcome.errors).toEqual([]);
    expect(outcome.inserted).toBe(4);
    expect(outcome.skipped_duplicates).toBe(0);

    const cards = cardsOf(c, outcome.deck_id);
    expect(cards.map((r) => r['type'])).toEqual(['short', 'mcq', 'multi', 'code']);
    expect(cards[0]).toMatchObject({
      prompt: 'Capital of Peru?',
      answer: 'Lima',
      topic: 'geography',
      answer_regex: '(?i)^lima$',
      explanation: 'Lima has been the capital since 1535.',
    });
    // Both columns hold JSON. The archive's `answer` cell is stored verbatim,
    // so it keeps whatever spacing the exporting build used; `choices` is a
    // newline-separated cell this build re-serialises. Parse rather than
    // compare bytes: the reader owes the value, not the spelling.
    expect(JSON.parse(String(cards[2]!['answer']))).toEqual(['Austria', 'Poland']);
    expect(JSON.parse(String(cards[1]!['choices']))).toEqual(['Bolivia', 'Chile', 'Peru', 'Ecuador']);
    expect(cards[3]).toMatchObject({ language: 'sql', skeleton: 'SELECT ___ FROM ___;', rubric: '- names the table\n- selects all columns' });

    const deck = rows(c, 'SELECT name, deck_type, context_prompt, desired_retention FROM decks WHERE id = ?', outcome.deck_id)[0];
    expect(deck).toMatchObject({ name: 'restored', deck_type: 'srs', context_prompt: 'World capitals and a little SQL.', desired_retention: 0.92 });

  });

  it('fsrs: the scheduling state, at the precision the file carries', () => {
    const { c, outcome } = importPrepdeck('fsrs.prepdeck');
    expect(outcome.errors).toEqual([]);
    expect(outcome.inserted).toBe(3);

    const cards = cardsOf(c, outcome.deck_id);
    // Six significant digits is all the file holds; the reader must not
    // invent precision, and must leave a never-reviewed card at its defaults.
    expect(cards[0]).toMatchObject({
      step: 2,
      next_due: '2026-03-20T15:00:00+00:00',
      last_review: '2026-03-12T15:00:00+00:00',
      stability: 3.12618,
      difficulty: 5.44091,
      fsrs_state: 2,
    });
    expect(cards[1]).toMatchObject({ step: 1, stability: 0.4072, difficulty: 7, fsrs_state: 1 });
    expect(cards[2]).toMatchObject({ step: 0, last_review: null, stability: null, difficulty: null, fsrs_state: 1 });
    expect(rows(c, 'SELECT desired_retention FROM decks WHERE id = ?', outcome.deck_id)[0]).toMatchObject({ desired_retention: 0.87 });

    // reviews.csv joins to cards by prompt, since a restore assigns fresh ids.
    expect(outcome.reviews_inserted).toBe(3);
    expect(rows(c, 'SELECT ts, result, user_answer, grader_notes FROM reviews ORDER BY id')).toEqual([
      { ts: '2026-03-10T09:00:00+00:00', result: 'wrong', user_answer: 'B', grader_notes: 'close' },
      { ts: '2026-03-12T15:00:00+00:00', result: 'right', user_answer: 'A', grader_notes: '' },
      { ts: '2026-03-14T09:00:00+00:00', result: 'right', user_answer: 'B', grader_notes: '' },
    ]);
  });

  it('unicode: escaped meta.json and astral card text survive', () => {
    const { c, outcome } = importPrepdeck('unicode.prepdeck');
    expect(outcome.errors).toEqual([]);
    expect(outcome.inserted).toBe(4);

    // meta.json stores this prompt as \uXXXX escapes.
    expect(rows(c, 'SELECT context_prompt FROM decks WHERE id = ?', outcome.deck_id)[0]).toMatchObject({
      context_prompt: '多言語 / متعدد اللغات',
    });
    const cards = cardsOf(c, outcome.deck_id);
    expect(cards.map((r) => r['prompt'])).toEqual([
      '日本の首都は？',
      'What does 🎴 mean here?',
      'ما هي عاصمة مصر؟',
      'Zwei\u00a0Wörter mit NBSP',
    ]);
    expect(cards[0]!['answer']).toBe('東京');
    expect(cards[1]!['explanation']).toBe('Astral plane: U+1F3B4.');
  });

  it('trivia-queue: the deck type, the rotation order and the answered state', () => {
    const { c, outcome } = importPrepdeck('trivia-queue.prepdeck');
    expect(outcome.errors).toEqual([]);
    expect(outcome.inserted).toBe(3);
    expect(outcome.queue_rows_inserted).toBe(3);

    const deck = rows(c, 'SELECT deck_type, context_prompt, notification_interval_minutes, trivia_session_size FROM decks WHERE id = ?', outcome.deck_id)[0];
    expect(deck).toMatchObject({ deck_type: 'trivia', context_prompt: 'Queue state', notification_interval_minutes: 30, trivia_session_size: 3 });

    const queue = rows(
      c,
      `SELECT q.prompt, t.queue_position, t.last_answered_at, t.last_answered_correctly FROM trivia_queue t
         JOIN questions q ON q.id = t.question_id ORDER BY t.queue_position`,
    );
    expect(queue).toEqual([
      { prompt: 'Answered right', queue_position: 1, last_answered_at: '2026-03-13T10:00:00+00:00', last_answered_correctly: 1 },
      { prompt: 'Answered wrong', queue_position: 2, last_answered_at: '2026-03-13T11:00:00+00:00', last_answered_correctly: 0 },
      { prompt: 'Never answered', queue_position: 3, last_answered_at: null, last_answered_correctly: null },
    ]);
  });

  it('quoting: embedded quotes, commas and newlines come back whole', () => {
    const { c, outcome } = importPrepdeck('quoting.prepdeck');
    expect(outcome.errors).toEqual([]);
    expect(outcome.inserted).toBe(4);

    const cards = cardsOf(c, outcome.deck_id);
    expect(cards.map((r) => [r['prompt'], r['answer']])).toEqual([
      ['He said "hello", then left', 'She replied "goodbye"'],
      ['Line one\nline two', 'Answer with, a comma'],
      // A CRLF inside a quoted cell survives as it was written; the reader
      // does not normalise line endings inside a value.
      ['CRLF inside\r\nthe cell', 'LF inside\nthe cell'],
      ['Trailing quote "', 'Leading "quote'],
    ]);
    expect(cards[3]!['explanation']).toBe('a, b, "c"');
  });

  it('empty: a deck with no cards restores as a deck', () => {
    const { c, outcome } = importPrepdeck('empty.prepdeck');
    expect(outcome.errors).toEqual([]);
    expect(outcome.inserted).toBe(0);
    expect(cardsOf(c, outcome.deck_id)).toEqual([]);
    expect(rows(c, 'SELECT name, deck_type FROM decks WHERE id = ?', outcome.deck_id)[0]).toMatchObject({
      name: 'restored',
      deck_type: 'srs',
    });
  });

  it('names the deck the caller asked for, not the one in the archive', () => {
    const { c, outcome } = importPrepdeck('fsrs.prepdeck');
    expect(outcome.deck_name).toBe('restored');
    expect(rows(c, "SELECT name FROM decks WHERE name = 'fsrs'")).toEqual([]);
  });
});

describe('an .apkg written by Anki imports', () => {
  it('anki-legacy: HTML, entities and media references come off the fields', async () => {
    const c = cell();
    const notes = await apkg.notes(fixture('anki-legacy.apkg'));
    expect(notes.length).toBe(8);

    const outcome = ankiNotesToDeck(c.repos as UserRepos, 'from-anki', notes);
    expect(outcome).toMatchObject({ inserted: 4, skipped_duplicates: 1, cloze_skipped: 0 });
    // A front with no back, a note with no front, and a front that is only an
    // image each name their note rather than landing as a blank card.
    expect(outcome.errors).toEqual([
      'note 1500000000005: no back-side content',
      'note 1500000000006: empty fields',
      'note 1500000000007: empty prompt after HTML strip',
    ]);

    const deckId = Number(rows(c, "SELECT id FROM decks WHERE name = 'from-anki'")[0]!['id']);
    const cards = cardsOf(c, deckId);
    expect(cards.map((r) => [r['prompt'], r['answer']])).toEqual([
      ['What is ACID?', 'Atomicity, Consistency, Isolation, Durability'],
      ['Two\nlines\n\nand a gap', 'Back side & more'],
      ['Block\nEnd', 'one\ntwo'],
      ['With media  here', 'Plain answer'],
    ]);
    expect(cards.every((r) => r['type'] === 'short')).toBe(true);
  });

  it('anki-cloze: cloze notes are counted out, the rest import', async () => {
    const c = cell();
    const notes = await apkg.notes(fixture('anki-cloze.apkg'));
    expect(notes.length).toBe(5);

    const outcome = ankiNotesToDeck(c.repos as UserRepos, 'from-cloze', notes);
    expect(outcome).toMatchObject({ inserted: 3, cloze_skipped: 2, skipped_duplicates: 0 });
    expect(outcome.errors).toEqual([]);

    const deckId = Number(rows(c, "SELECT id FROM decks WHERE name = 'from-cloze'")[0]!['id']);
    const cards = cardsOf(c, deckId);
    expect(cards.map((r) => r['prompt'])).toEqual([
      'Normal note',
      `<escaped> "quotes" 'and' 'apostrophes'`,
      'Three fields',
    ]);
    expect(cards[1]!['answer']).toBe('entities on the back: &amp;');
    // Fields past the second join into the answer.
    expect(cards[2]!['answer']).toBe('second\n\nthird');
  });
});
