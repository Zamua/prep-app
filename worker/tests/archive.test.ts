// The `.prepdeck` codec's refusals and its two order-sensitive sections. The
// happy path is the byte gate in deckio.parity.test.ts; what is here is the
// branches no corpus profile reaches.
import { describe, expect, it } from 'vitest';
import { deckToPrepdeck, prepdeckToDeck, FORMAT_VERSION } from '../app/decks/archive.js';
import { MAX_ZIP_ENTRY_BYTES } from '../app/decks/importLimits.js';
import { ZipEntryTooLarge, type ZipEntry } from '../app/ports.js';
import { FflateZip } from '../runtime/adapters/zip.js';
import { cell } from './repos/setup.js';

const zip = new FflateZip();
const enc = new TextEncoder();
const EXPORTED_AT = '2026-03-14T15:00:00Z';

const CARDS_HEADER = 'type,topic,prompt,answer,choices,rubric,skeleton,language,answer_regex,explanation,step,next_due,last_review,stability,difficulty,fsrs_state\r\n';
const REVIEWS_HEADER = 'prompt,ts,result,user_answer,grader_notes\r\n';

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
  it('a deck name the format does not allow, quoting it as Python does', () => {
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
    expect(prepdeckToDeck(c.repos, 'fresh', minimal(), zip, { maxEntryBytes: MAX_ZIP_ENTRY_BYTES }).errors).toEqual([]);
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

  it('a trivia queue from cards.csv order when the section is missing, and says what was lost', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta({}, { deck_type: 'trivia', notification_interval_minutes: 30 }),
      'cards.csv': CARDS_HEADER + 'short,,one,A,,,,,,,,,,,,\r\nshort,,two,B,,,,,,,,,,,,\r\n',
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
        'short,,stated,A,,,,,,,3,2026-04-01T00:00:00+00:00,2026-03-01T00:00:00+00:00,9.5,4.25,2\r\n' +
        'short,,bare,B,,,,,,,,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER,
    });
    prepdeckToDeck(c.repos, 'fresh', blob, zip);
    const deckId = c.repos.decks.findId('fresh')!;
    const state = new Map(c.repos.cards.listCardStateForDeck(deckId).map((r) => [r.prompt, r]));
    expect(state.get('stated')).toMatchObject({ step: 3, next_due: '2026-04-01T00:00:00+00:00', stability: 9.5, difficulty: 4.25, fsrs_state: 2 });
    expect(state.get('bare')).toMatchObject({ step: 0, last_review: null, stability: null, difficulty: null });
  });

  it('names a malformed state cell without dropping the card', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta(),
      'cards.csv': CARDS_HEADER + 'short,,card,A,,,,,,,two,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER,
    });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip);
    expect(outcome.inserted).toBe(1);
    expect(outcome.errors).toEqual(["cards.csv row 2: bad step='two'"]);
  });

  it('reports a review whose prompt is not in the deck', () => {
    const c = cell();
    const blob = archive({
      'meta.json': meta(),
      'cards.csv': CARDS_HEADER + 'short,,here,A,,,,,,,,,,,,\r\n',
      'reviews.csv': REVIEWS_HEADER + 'gone,2026-03-01T00:00:00+00:00,right,,\r\nhere,2026-03-01T00:00:00+00:00,maybe,,\r\n',
    });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip);
    expect(outcome.reviews_inserted).toBe(0);
    expect(outcome.errors).toEqual(["reviews.csv row 2: prompt 'gone' not found in deck", "reviews.csv row 3: bad result 'maybe'"]);
  });

  it('stops at the row cap and keeps what it inserted', () => {
    const c = cell();
    const rows = Array.from({ length: 4 }, (_, i) => `short,,q${i},a${i},,,,,,,,,,,,\r\n`).join('');
    const blob = archive({ 'meta.json': meta(), 'cards.csv': CARDS_HEADER + rows, 'reviews.csv': REVIEWS_HEADER });
    const outcome = prepdeckToDeck(c.repos, 'fresh', blob, zip, { rowCap: 2 });
    expect(outcome.inserted).toBe(2);
    expect(outcome.errors).toEqual(['stopped at 2 rows; split the file and import again']);
  });
});

describe('deckToPrepdeck', () => {
  it('writes the same bytes twice for an unchanged deck', () => {
    const c = cell();
    const deckId = c.repos.decks.create('d');
    c.repos.questions.add(deckId, { type: 'short', prompt: 'p', answer: 'a' });
    expect(deckToPrepdeck(c.repos, deckId, zip, EXPORTED_AT)).toEqual(deckToPrepdeck(c.repos, deckId, zip, EXPORTED_AT));
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
