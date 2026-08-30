// The `.prepdeck` codec's refusals and its two order-sensitive sections. The
// happy path is tests/legacyImport.test.ts, which reads real archives; what
// is here is the branches no whole archive reaches.
import { zipSync } from 'fflate';
import { describe, expect, it } from 'vitest';
import { deckToPrepdeck, prepdeckToDeck, FORMAT_VERSION } from '../app/decks/archive.js';
import { MAX_ZIP_ENTRY_BYTES, MAX_ZIP_TOTAL_BYTES } from '../app/decks/importLimits.js';
import { ZipEntryTooLarge, type ZipEntry } from '../app/ports.js';
import { FflateZip } from '../runtime/adapters/zip.js';
import { cell } from './repos/setup.js';

const zip = new FflateZip();
const enc = new TextEncoder();
const dec = new TextDecoder();
const EXPORTED_AT = '2026-03-14T15:00:00Z';

const CARDS_HEADER = 'archive_id,type,topic,prompt,answer,choices,rubric,skeleton,language,answer_regex,explanation,step,next_due,last_review,stability,difficulty,fsrs_state,learning_steps,choices_json,suspended\r\n';
const REVIEWS_HEADER = 'archive_id,prompt,ts,result,user_answer,grader_notes\r\n';

function archive(parts: Record<string, string>): Uint8Array {
  return zip.write(Object.entries(parts).map(([name, body]): ZipEntry => ({ name, bytes: enc.encode(body) })));
}

const meta = (over: Record<string, unknown> = {}, deck: Record<string, unknown> = {}): string =>
  JSON.stringify({
    format_version: FORMAT_VERSION,
    exported_at: EXPORTED_AT,
    deck: { name: 'x', deck_type: 'srs', context_prompt: null, notification_interval_minutes: null, trivia_session_size: 3, desired_retention: null, ...deck },
    ...over,
  });

const minimal = (over: Record<string, unknown> = {}, deck: Record<string, unknown> = {}): Uint8Array =>
  archive({ 'meta.json': meta(over, deck), 'cards.csv': CARDS_HEADER, 'reviews.csv': REVIEWS_HEADER });

describe('prepdeckToDeck refuses', () => {
  it('a deck name the format does not allow, quoted in the refusal', () => {
    const c = cell();
    expect(prepdeckToDeck(c.repos, 'NoCaps', minimal(), zip).errors).toEqual([
      "invalid deck name 'NoCaps'; must match ^[a-z0-9][a-z0-9-]{1,29}$",
    ]);
  });

  it('a name that already exists, because a restore is not a merge', () => {
    const c = cell();
    c.repos.decks.create('taken');
    const outcome = prepdeckToDeck(c.repos, 'taken', minimal(), zip);
    expect(outcome.deck_id).toBe(0);
    expect(outcome.errors[0]).toContain("deck 'taken' already exists.");
  });

  it('bytes that are not a zip', () => {
    const c = cell();
    expect(prepdeckToDeck(c.repos, 'fresh', enc.encode('not a zip at all'), zip).errors[0]).toContain('not a valid zip:');
  });

  it('an archive missing a required entry, listing what is gone', () => {
    const c = cell();
    const outcome = prepdeckToDeck(c.repos, 'fresh', archive({ 'meta.json': meta() }), zip);
    expect(outcome.errors).toEqual(["archive is missing required entries: ['cards.csv', 'reviews.csv']"]);
  });

  it('a meta.json that is not JSON', () => {
    const c = cell();
    const blob = archive({ 'meta.json': '{not json', 'cards.csv': CARDS_HEADER, 'reviews.csv': REVIEWS_HEADER });
    expect(prepdeckToDeck(c.repos, 'fresh', blob, zip).errors[0]).toContain('meta.json is not valid JSON:');
  });

  it('a meta.json that is valid JSON but not an object', () => {
    const c = cell();
    const blob = archive({ 'meta.json': 'null', 'cards.csv': CARDS_HEADER, 'reviews.csv': REVIEWS_HEADER });
    expect(prepdeckToDeck(c.repos, 'fresh', blob, zip).errors).toEqual(['meta.json must contain an object']);
  });

  it('an unknown deck type before creating a deck with fresh schedules', () => {
    const c = cell();
    const outcome = prepdeckToDeck(c.repos, 'fresh', minimal({}, { deck_type: 'spaced' }), zip);
    expect(outcome.errors).toEqual(["unknown deck_type 'spaced'; expected 'srs' or 'trivia'"]);
    expect(c.repos.decks.findId('fresh')).toBeNull();
  });

  it('a format written by a newer build', () => {
    const c = cell();
    const outcome = prepdeckToDeck(c.repos, 'fresh', minimal({ format_version: FORMAT_VERSION + 1 }), zip);
    expect(outcome.errors[0]).toContain(`.prepdeck format_version ${FORMAT_VERSION + 1} is newer than this prep build supports (${FORMAT_VERSION}).`);
  });

  it('a format version below one', () => {
    const c = cell();
    expect(prepdeckToDeck(c.repos, 'fresh', minimal({ format_version: 0 }), zip).errors).toEqual([
      '.prepdeck format_version 0 is unknown; must be ≥ 1',
    ]);
  });

  it('an entry that declares more inflated bytes than the cap', () => {
    const c = cell();
    const big = archive({ 'meta.json': meta(), 'cards.csv': CARDS_HEADER, 'reviews.csv': REVIEWS_HEADER });
    expect(() => prepdeckToDeck(c.repos, 'fresh', big, zip, { maxEntryBytes: 8 })).toThrow(ZipEntryTooLarge);
  });

  it('nothing on an entry inside the deployed cap', () => {
    const c = cell();
    expect(prepdeckToDeck(c.repos, 'fresh', minimal(), zip, { maxEntryBytes: MAX_ZIP_ENTRY_BYTES, maxTotalBytes: MAX_ZIP_TOTAL_BYTES }).errors).toEqual([]);
  });

  it('entries each inside the per-entry cap whose sum is not', () => {
    const c = cell();
    const filler = 'x'.repeat(64);
    const blob = archive({ 'meta.json': meta(), 'cards.csv': CARDS_HEADER + filler, 'reviews.csv': REVIEWS_HEADER + filler });
    expect(() => prepdeckToDeck(c.repos, 'fresh', blob, zip, { maxEntryBytes: 4096, maxTotalBytes: 200 })).toThrow(ZipEntryTooLarge);
  });
});

describe('prepdeckToDeck inflates only the sections it reads', () => {
  it('leaves an entry no section names compressed, whatever it declares', () => {
    const c = cell();
    // 16 MiB of zeros, past every ceiling once inflated, under a name the
    // reader never asks for. Deflated it is a few kilobytes.
    const padded = zipSync({
      'meta.json': enc.encode(meta()),
      'cards.csv': enc.encode(CARDS_HEADER),
      'reviews.csv': enc.encode(REVIEWS_HEADER),
      'media/big.bin': new Uint8Array(16 * 1024 * 1024),
    });
    expect(padded.length).toBeLessThan(200 * 1024);
    const outcome = prepdeckToDeck(c.repos, 'fresh', padded, zip, { maxEntryBytes: 4096, maxTotalBytes: 4096 });
    expect(outcome.errors).toEqual([]);
    expect(outcome.deck_id).toBeGreaterThan(0);
  });
});

describe('prepdeckToDeck restores', () => {
  it('a trivia deck with its interval, session size and queue order', () => {
    const c = cell();
    const source = c.repos.decks.createTrivia('src', { topic: 'Distributed systems', intervalMinutes: 45 });
    c.repos.decks.setTriviaSessionSize(source, 4);
    const a = c.repos.questions.add(source, { type: 'short', prompt: 'first', answer: 'A' });
    const b = c.repos.questions.add(source, { type: 'short', prompt: 'second', answer: 'B' });
    c.repos.trivia.importEntry(b, 1, { lastAnsweredAt: '2026-03-13T10:00:00+00:00', lastAnsweredCorrectly: 1 });
    c.repos.trivia.importEntry(a, 2, { lastAnsweredAt: null, lastAnsweredCorrectly: null });

    const outcome = prepdeckToDeck(c.repos, 'restored', deckToPrepdeck(c.repos, source, zip, EXPORTED_AT), zip);
    expect(outcome).toMatchObject({ inserted: 2, queue_rows_inserted: 2, errors: [] });
    const restored = c.repos.decks.findId('restored')!;
    expect(c.repos.decks.getMeta(restored)).toMatchObject({ interval_minutes: 45, session_size: 4 });
    expect(c.repos.trivia.listQueueForDeck(restored).map((e) => [e.queue_position, e.prompt])).toEqual([
      [1, 'second'],
      [2, 'first'],
    ]);
  });

  it('normalizes valid trivia timestamps and clears malformed ones', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta({}, { deck_type: 'trivia', notification_interval_minutes: 30 }),
      'cards.csv': CARDS_HEADER + '1,short,,one,A,,,,,,,,,,,,\r\n2,short,,two,B,,,,,,,,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER,
      'trivia_queue.csv':
        'archive_id,prompt,queue_position,last_answered_at,last_answered_correctly\r\n' +
        '1,one,1,2026-03-14T10:00:00-05:00,1\r\n' +
        '2,two,2,not-a-date,0\r\n',
    });

    const outcome = prepdeckToDeck(c.repos, 'restored', blob, zip);
    expect(outcome).toMatchObject({ inserted: 2, queue_rows_inserted: 2 });
    expect(outcome.errors).toEqual([
      "trivia_queue.csv row 3: bad last_answered_at='not-a-date'; expected ISO-8601 with UTC offset",
    ]);
    const restored = c.repos.decks.findId('restored')!;
    expect(c.repos.trivia.listQueueForDeck(restored).map((row) => row.last_answered_at)).toEqual([
      '2026-03-14T15:00:00+00:00',
      null,
    ]);
  });

  it('a trivia queue from cards.csv order when the section is missing, and says what was lost', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta({}, { deck_type: 'trivia', notification_interval_minutes: 30 }),
      'cards.csv': CARDS_HEADER + '1,short,,one,A,,,,,,,,,,,,\r\n2,short,,two,B,,,,,,,,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER,
    });
    const outcome = prepdeckToDeck(c.repos, 'rebuilt', blob, zip);
    expect(outcome).toMatchObject({ inserted: 2, queue_rows_inserted: 2 });
    expect(outcome.errors).toEqual([
      'trivia_queue.csv was missing — queue rebuilt from cards.csv order; per-card answered state was lost.',
    ]);
  });

  it('every FSRS column, and leaves the defaults where a cell is empty', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta(),
      'cards.csv':
        CARDS_HEADER +
        '1,short,,stated,A,,,,,,,3,2026-04-01T00:00:00+00:00,2026-03-01T00:00:00+00:00,9.5,4.25,2,1\r\n' +
        '2,short,,bare,B,,,,,,,,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER,
    });
    prepdeckToDeck(c.repos, 'fresh', blob, zip);
    const deckId = c.repos.decks.findId('fresh')!;
    const state = new Map(c.repos.cards.listCardStateForDeck(deckId).map((r) => [r.prompt, r]));
    expect(state.get('stated')).toMatchObject({ step: 3, next_due: '2026-04-01T00:00:00+00:00', stability: 9.5, difficulty: 4.25, fsrs_state: 2, learning_steps: 1 });
    expect(state.get('bare')).toMatchObject({ step: 0, last_review: null, stability: null, difficulty: null, learning_steps: 0 });
  });

  it('names a malformed state cell without dropping the card', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta(),
      'cards.csv': CARDS_HEADER + '1,short,,card,A,,,,,,,two,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER,
    });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip);
    expect(outcome.inserted).toBe(1);
    expect(outcome.errors).toEqual(["cards.csv row 2: bad step='two'"]);
  });

  it('leaves safe defaults for malformed scheduler timestamps', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta(),
      'cards.csv': CARDS_HEADER + '1,short,,card,A,,,,,,,0,not-a-date,2026-03-01T00:00:00,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER,
    });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip);
    expect(outcome.errors).toEqual([
      "cards.csv row 2: bad next_due='not-a-date'; expected ISO-8601 with UTC offset",
      "cards.csv row 2: bad last_review='2026-03-01T00:00:00'; expected ISO-8601 with UTC offset",
    ]);

    const deckId = c.repos.decks.findId('fresh')!;
    const [question] = c.repos.questions.listInDeck(deckId);
    expect(c.repos.cards.srsState(question!.id)).toMatchObject({
      next_due: '2026-03-14T15:00:00+00:00',
      last_review: null,
    });
    expect(c.repos.cards.countDue()).toBe(1);
    expect(() => c.repos.reviews.record(question!.id, 'right', 'A')).not.toThrow();
  });

  it('normalizes card and review timestamps to the UTC storage format', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta(),
      'cards.csv':
        CARDS_HEADER +
        '1,short,,card,A,,,,,,,1,2026-03-14T10:10:00-05:00,2026-03-14T16:00:00+01:00,2.3,2.1,1,1\r\n',
      'reviews.csv': REVIEWS_HEADER + '1,card,2026-03-14T10:00:00-05:00,right,A,\r\n',
    });

    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip);
    expect(outcome).toMatchObject({ inserted: 1, reviews_inserted: 1, errors: [] });
    const deckId = c.repos.decks.findId('fresh')!;
    expect(c.repos.cards.listCardStateForDeck(deckId)[0]).toMatchObject({
      next_due: '2026-03-14T15:10:00+00:00',
      last_review: '2026-03-14T15:00:00+00:00',
    });
    expect(c.repos.reviews.listReviewsForDeck(deckId)[0]?.ts).toBe('2026-03-14T15:00:00+00:00');
  });

  it('drops a review with a malformed timestamp', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta(),
      'cards.csv': CARDS_HEADER + '1,short,,card,A,,,,,,,,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER + '1,card,not-a-date,right,A,\r\n',
    });

    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip);
    expect(outcome.reviews_inserted).toBe(0);
    expect(outcome.errors).toEqual(["reviews.csv row 2: bad ts='not-a-date'; expected ISO-8601 with UTC offset"]);
  });

  it('reports a review whose archive identity is not in the deck', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta(),
      'cards.csv': CARDS_HEADER + '1,short,,here,A,,,,,,,,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER + '9,gone,2026-03-01T00:00:00+00:00,right,,\r\n1,here,2026-03-01T00:00:00+00:00,maybe,,\r\n',
    });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip);
    expect(outcome.reviews_inserted).toBe(0);
    expect(outcome.errors).toEqual(["reviews.csv row 2: archive_id '9' not found in deck", "reviews.csv row 3: bad result 'maybe'"]);
  });

  it('stops at the row cap and keeps what it inserted', () => {
    const c = cell();
    const rows = Array.from({ length: 4 }, (_, i) => `${i + 1},short,,q${i},a${i},,,,,,,,,,,,\r\n`).join('');
    const blob = archive({ 'meta.json': meta(), 'cards.csv': CARDS_HEADER + rows, 'reviews.csv': REVIEWS_HEADER });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip, { rowCap: 2 });
    expect(outcome.inserted).toBe(2);
    expect(outcome.errors).toEqual(['stopped at 2 rows; split the file and import again']);
  });

  it('stops at the review cap, which the card cap does not bound', () => {
    const c = cell();
    const reviews = Array.from({ length: 5 }, (_, i) => `1,card,2026-03-0${i + 1}T00:00:00+00:00,right,,\r\n`).join('');
    const blob = archive({
      'meta.json': meta(),
      'cards.csv': CARDS_HEADER + '1,short,,card,A,,,,,,,,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER + reviews,
    });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip, { rowCap: 5000, reviewRowCap: 2 });
    expect(outcome.reviews_inserted).toBe(2);
    expect(outcome.errors).toEqual(['reviews.csv: stopped at 2 rows; split the file and import again']);
  });

  it('stops at the queue cap, keeping the lowest positions', () => {
    const c = cell();
    const cards = Array.from({ length: 4 }, (_, i) => `${i + 1},short,,q${i},a${i},,,,,,,,,,,,\r\n`).join('');
    const queue =
      'archive_id,prompt,queue_position,last_answered_at,last_answered_correctly\r\n' +
      [3, 1, 2, 0].map((p, i) => `${i + 1},q${i},${p},,\r\n`).join('') +
      '9,padding,4,,\r\n';
    const blob = archive({
      'meta.json': meta({}, { deck_type: 'trivia', notification_interval_minutes: 30 }),
      'cards.csv': CARDS_HEADER + cards,
      'reviews.csv': REVIEWS_HEADER,
      'trivia_queue.csv': queue,
    });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip, { rowCap: 4 });
    expect(outcome.queue_rows_inserted).toBe(4);
    const deckId = c.repos.decks.findId('fresh')!;
    expect(c.repos.trivia.listQueueForDeck(deckId).map((e) => e.prompt)).toEqual(['q3', 'q1', 'q2', 'q0']);
    expect(outcome.errors).toEqual(['trivia_queue.csv: stopped at 4 rows; split the file and import again']);
  });

  it('refuses a whitespace-only type the way an empty cell is not refused', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta(),
      'cards.csv': CARDS_HEADER + '1,short,,typed,A,,,,,,,,,,,,\r\n2,"  ",,spaces,B,,,,,,,,,,,,\r\n3,,,blank,C,,,,,,,,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER,
    });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip);
    expect(outcome.inserted).toBe(2);
    expect(outcome.errors).toEqual(["cards.csv row 3: unknown type '  '"]);
  });
});

describe('deckToPrepdeck', () => {
  it('writes the same bytes twice for an unchanged deck', () => {
    const c = cell();
    const deckId = c.repos.decks.create('d');
    c.repos.questions.add(deckId, { type: 'short', prompt: 'p', answer: 'a' });
    expect(deckToPrepdeck(c.repos, deckId, zip, EXPORTED_AT)).toEqual(deckToPrepdeck(c.repos, deckId, zip, EXPORTED_AT));
  });

  it('preserves the learning rung through an archive round trip', () => {
    const c = cell();
    const source = c.repos.decks.create('source');
    const qid = c.repos.questions.add(source, { type: 'short', prompt: 'p', answer: 'a' });
    c.repos.cards.restoreCardState(qid, {
      next_due: '2026-03-14T15:10:00+00:00',
      last_review: '2026-03-14T15:00:00+00:00',
      stability: 2.3065,
      difficulty: 2.11810397,
      fsrs_state: 1,
      learning_steps: 1,
    });

    const blob = deckToPrepdeck(c.repos, source, zip, EXPORTED_AT);
    expect(prepdeckToDeck(c.repos, 'restored', blob, zip).errors).toEqual([]);
    const restored = c.repos.decks.findId('restored')!;
    expect(c.repos.cards.listCardStateForDeck(restored)[0]).toMatchObject({ learning_steps: 1 });
  });

  it('preserves suspension and exact choice text through a v2 round trip', () => {
    const c = cell();
    const source = c.repos.decks.create('source');
    const qid = c.repos.questions.add(source, {
      type: 'mcq',
      prompt: 'Pick one',
      answer: '  padded  ',
      choices: [' leading', 'trailing ', 'two\nlines'],
    });
    c.repos.questions.setSuspended(qid, true);

    const outcome = prepdeckToDeck(c.repos, 'restored', deckToPrepdeck(c.repos, source, zip, EXPORTED_AT), zip);
    expect(outcome.errors).toEqual([]);
    const restored = c.repos.decks.findId('restored')!;
    expect(c.repos.questions.listInDeck(restored)[0]).toMatchObject({
      answer: '  padded  ',
      choices: [' leading', 'trailing ', 'two\nlines'],
      suspended: true,
    });
  });

  it('keeps duplicate-prompt SRS cards attached to their own state and reviews', () => {
    const c = cell();
    const source = c.repos.decks.create('source');
    const first = c.repos.questions.add(source, { type: 'short', prompt: 'same prompt', answer: 'first answer' });
    const second = c.repos.questions.add(source, { type: 'short', prompt: 'same prompt', answer: 'second answer' });
    c.repos.cards.restoreCardState(first, {
      step: 4,
      next_due: '2026-04-01T00:00:00+00:00',
      last_review: '2026-03-01T00:00:00+00:00',
      stability: 9.5,
      difficulty: 4.25,
      fsrs_state: 2,
      learning_steps: 0,
    });
    c.repos.cards.restoreCardState(second, {
      step: 1,
      next_due: '2026-03-14T15:10:00+00:00',
      last_review: '2026-03-02T00:00:00+00:00',
      stability: 2.3065,
      difficulty: 2.11810397,
      fsrs_state: 1,
      learning_steps: 1,
    });
    c.repos.reviews.importReview(first, '2026-03-01T00:00:00+00:00', 'right', 'first review', 'first notes');
    c.repos.reviews.importReview(second, '2026-03-02T00:00:00+00:00', 'wrong', 'second review', 'second notes');

    const outcome = prepdeckToDeck(c.repos, 'restored', deckToPrepdeck(c.repos, source, zip, EXPORTED_AT), zip);
    expect(outcome).toMatchObject({ inserted: 2, skipped_duplicates: 0, reviews_inserted: 2, errors: [] });
    const restored = c.repos.decks.findId('restored')!;
    const answerById = new Map(c.repos.questions.listInDeck(restored).map((q) => [q.id, q.answer]));
    const stateByAnswer = new Map(c.repos.cards.listCardStateForDeck(restored).map((state) => [answerById.get(state.question_id), state]));
    expect(stateByAnswer.get('first answer')).toMatchObject({
      step: 4,
      next_due: '2026-04-01T00:00:00+00:00',
      last_review: '2026-03-01T00:00:00+00:00',
      stability: 9.5,
      difficulty: 4.25,
      fsrs_state: 2,
      learning_steps: 0,
    });
    expect(stateByAnswer.get('second answer')).toMatchObject({
      step: 1,
      next_due: '2026-03-14T15:10:00+00:00',
      last_review: '2026-03-02T00:00:00+00:00',
      stability: 2.3065,
      difficulty: 2.11810397,
      fsrs_state: 1,
      learning_steps: 1,
    });
    expect(c.repos.reviews.listReviewsForDeck(restored).map((r) => [answerById.get(r.question_id), r.result, r.user_answer, r.grader_notes])).toEqual([
      ['first answer', 'right', 'first review', 'first notes'],
      ['second answer', 'wrong', 'second review', 'second notes'],
    ]);
  });

  it('keeps duplicate-prompt trivia cards in their own queue positions', () => {
    const c = cell();
    const source = c.repos.decks.createTrivia('source', { topic: 'topic', intervalMinutes: 30 });
    const first = c.repos.questions.add(source, { type: 'short', prompt: 'same prompt', answer: 'first answer' });
    const second = c.repos.questions.add(source, { type: 'short', prompt: 'same prompt', answer: 'second answer' });
    c.repos.trivia.importEntry(second, 1, { lastAnsweredAt: '2026-03-02T00:00:00+00:00', lastAnsweredCorrectly: 0 });
    c.repos.trivia.importEntry(first, 2, { lastAnsweredAt: '2026-03-01T00:00:00+00:00', lastAnsweredCorrectly: 1 });

    const outcome = prepdeckToDeck(c.repos, 'restored', deckToPrepdeck(c.repos, source, zip, EXPORTED_AT), zip);
    expect(outcome).toMatchObject({ inserted: 2, skipped_duplicates: 0, queue_rows_inserted: 2, errors: [] });
    const restored = c.repos.decks.findId('restored')!;
    const answerById = new Map(c.repos.questions.listInDeck(restored).map((q) => [q.id, q.answer]));
    expect(c.repos.trivia.listQueueForDeck(restored).map((row) => [answerById.get(row.question_id), row.queue_position, row.last_answered_at, row.last_answered_correctly])).toEqual([
      ['second answer', 1, '2026-03-02T00:00:00+00:00', false],
      ['first answer', 2, '2026-03-01T00:00:00+00:00', true],
    ]);
  });

  it('writes archive_id in every v2 relationship file', () => {
    const c = cell();
    const deckId = c.repos.decks.createTrivia('trivia', { topic: 'topic', intervalMinutes: 30 });
    const qid = c.repos.questions.add(deckId, { type: 'short', prompt: 'prompt', answer: 'answer' });
    c.repos.trivia.importEntry(qid, 1);
    const entries = new Map(zip.read(deckToPrepdeck(c.repos, deckId, zip, EXPORTED_AT)).map((entry) => [entry.name, dec.decode(entry.bytes)]));
    const header = (name: string): string => entries.get(name)!.split('\r\n', 1)[0]!;

    expect(header('cards.csv')).toBe(CARDS_HEADER.trim());
    expect(header('reviews.csv')).toBe(REVIEWS_HEADER.trim());
    expect(header('trivia_queue.csv')).toBe('archive_id,prompt,queue_position,last_answered_at,last_answered_correctly');
  });

  it('carries the trivia queue only for a trivia deck', () => {
    const c = cell();
    const srs = c.repos.decks.create('srs');
    expect(zip.read(deckToPrepdeck(c.repos, srs, zip, EXPORTED_AT)).map((e) => e.name)).toEqual(['meta.json', 'cards.csv', 'reviews.csv']);
    const trivia = c.repos.decks.createTrivia('triv', { topic: 't', intervalMinutes: 30 });
    expect(zip.read(deckToPrepdeck(c.repos, trivia, zip, EXPORTED_AT)).map((e) => e.name)).toEqual([
      'meta.json',
      'cards.csv',
      'reviews.csv',
      'trivia_queue.csv',
    ]);
  });
});
