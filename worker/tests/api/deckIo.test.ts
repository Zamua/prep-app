// The CSV wire format: the writer's dialect on the way out, the reader's
// tolerances on the way in, and the trivia preamble that carries the
// deck-level state a row cannot.
import { describe, expect, it } from 'vitest';
import { parseDict, parseRows, writeRow } from '../../app/api/csv.js';
import { csvToDeck, deckToCsv, questionsForExport, splitPreamble, type DeckIoRepos } from '../../app/api/deckIo.js';
import { cell } from '../repos/setup.js';

const repos = (): DeckIoRepos => {
  const c = cell();
  return c.repos;
};

describe('the csv dialect', () => {
  it('terminates rows with CRLF and quotes only what carries a structural char', () => {
    expect(writeRow(['a', 'b'])).toBe('a,b\r\n');
    expect(writeRow(['with, comma'])).toBe('"with, comma"\r\n');
    expect(writeRow(['line\nbreak'])).toBe('"line\nbreak"\r\n');
    expect(writeRow(['say "hi"'])).toBe('"say ""hi"""\r\n');
    expect(writeRow([''])).toBe('\r\n');
  });

  it('reads quoted newlines and doubled quotes back', () => {
    expect(parseRows('a,"b\nc",d\r\n')).toEqual([['a', 'b\nc', 'd']]);
    expect(parseRows('"say ""hi"""\r\n')).toEqual([['say "hi"']]);
    expect(parseRows('a,b')).toEqual([['a', 'b']]);
  });

  it('reads a blank line as an empty row, which DictReader then skips', () => {
    expect(parseRows('a\n\nb\n')).toEqual([['a'], [], ['b']]);
    expect(parseDict('h\n\nb\n').rows).toEqual([{ h: 'b' }]);
  });

  it('fills a short row with nulls', () => {
    expect(parseDict('a,b,c\n1\n').rows).toEqual([{ a: '1', b: null, c: null }]);
  });
});

describe('the preamble', () => {
  it('lifts `# key: value` lines off the top and lowercases the keys', () => {
    const { preamble, rest } = splitPreamble('# Deck_Type: trivia\n# notification_interval_minutes: 30\ntype,prompt\nshort,q\n');
    expect(preamble).toEqual({ deck_type: 'trivia', notification_interval_minutes: '30' });
    expect(rest).toBe('type,prompt\nshort,q');
  });

  it('is emitted for a trivia deck and omitted for an SRS one', () => {
    const r = repos();
    const trivia = r.decks.createTrivia('quiz', { topic: 'World\nhistory', intervalMinutes: 45 });
    expect(deckToCsv(r, trivia)).toMatch(
      /^# deck_type: trivia\n# notification_interval_minutes: 45\n# trivia_session_size: 3\n# topic_prompt: World history\ntype,topic,/,
    );
    const srs = r.decks.create('plain');
    expect(deckToCsv(r, srs).startsWith('type,topic,prompt')).toBe(true);
  });
});

describe('csvToDeck', () => {
  const HEADER = 'type,topic,prompt,answer,choices,rubric,skeleton,language,answer_regex,explanation\n';

  it('inserts, dedupes by prompt, and reports one error per bad row', () => {
    const r = repos();
    const outcome = csvToDeck(
      r,
      'imported',
      HEADER + 'short,africa,Capital of Ghana?,Accra,,,,,accra,\nmcq,,Capital of Egypt?,Cairo,"Cairo\nGiza",,,,,\nshort,,Capital of Ghana?,Accra,,,,,,\nessay,,Bad type,x,,,,,,\n,,No type,defaults to short,,,,,,\nshort,,,no prompt,,,,,,\nshort,,no answer,,,,,,,\n',
    );
    expect(outcome).toMatchObject({
      deck_name: 'imported',
      inserted: 3,
      skipped_duplicates: 1,
      errors: ["row 5: unknown type 'essay'", 'row 7: missing prompt', 'row 8: missing answer'],
    });
    const cards = questionsForExport(r, outcome.deck_id);
    expect(cards.map((q) => q.type)).toEqual(['short', 'mcq', 'short']);
    expect(cards[1]!.choices).toEqual(['Cairo', 'Giza']);
  });

  it('refuses to mix a declared type into an existing deck of another one', () => {
    const r = repos();
    r.decks.create('mixed');
    const outcome = csvToDeck(r, 'mixed', '# deck_type: trivia\n' + HEADER + 'short,,q,a,,,,,,\n');
    expect(outcome.inserted).toBe(0);
    expect(outcome.errors[0]).toContain("already exists as 'srs'; CSV declares 'trivia'");
  });

  it('queues an imported trivia card so it becomes pickable', () => {
    const r = repos();
    const outcome = csvToDeck(r, 'quiz', '# deck_type: trivia\n# trivia_session_size: 5\n' + HEADER + 'short,,q,a,,,,,,\n');
    expect(outcome.inserted).toBe(1);
    expect(r.decks.getType(outcome.deck_id)).toBe('trivia');
    expect(r.decks.getTriviaSessionSize(outcome.deck_id)).toBe(5);
    expect(r.trivia.listQueueForDeck(outcome.deck_id)).toHaveLength(1);
  });

  it('reports a missing header row rather than inserting nothing quietly', () => {
    const r = repos();
    expect(csvToDeck(r, 'empty-deck', '').errors).toEqual(['CSV has no header row']);
  });

  it('round-trips a deck through export and import', () => {
    const r = repos();
    const source = csvToDeck(r, 'source', HEADER + 'code,python,"Return, or None","def f():\n    ...",,"- uses get","def f():\n    ...",python,,why\n');
    expect(source.inserted).toBe(1);
    const exported = deckToCsv(r, source.deck_id);
    const copy = csvToDeck(r, 'copy', exported);
    expect(copy.inserted).toBe(1);
    expect(questionsForExport(r, copy.deck_id)[0]).toMatchObject(
      Object.fromEntries(Object.entries(questionsForExport(r, source.deck_id)[0]!).filter(([k]) => !['id', 'deck_id', 'created_at'].includes(k))),
    );
  });
});
