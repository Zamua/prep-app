// The Anki note mapping. `stripHtml` is checked against Python itself rather
// than against a table someone typed twice: it is five regex passes and an
// ordered entity table, and a transcription slip in any of them is silent.
import { describe, expect, it } from 'vitest';
import { ankiNotesToDeck, stripHtml } from '../app/decks/anki.js';
import { MAX_APKG_UPLOAD_BYTES, MAX_CSV_UPLOAD_BYTES, MAX_IMPORT_ROWS, MAX_PREPDECK_UPLOAD_BYTES, rowCapMessage, uploadTooLarge } from '../app/decks/importLimits.js';
import { pythonJson } from './pyoracle.js';
import { cell } from './repos/setup.js';

const STRIP_CASES: readonly string[] = [
  '',
  'plain text',
  'a<br>b',
  'a<BR/>c',
  'a< br />d',
  'one<br><br>two',
  '<p>para</p><p>two</p>',
  '<div>block</div>',
  '<li>one</li><li>two</li>',
  '<h1>head</h1><h6>six</h6>',
  '< /p>not a block end',
  '</ p>a block end',
  'sound [sound:beep.mp3] gone',
  'anki [anki:play:q:0] gone',
  'MIXED [SOUND:x.mp3] case',
  '[sound:unclosed',
  '<img src="x.png" alt="y">',
  '<span style="a>b">tricky</span>',
  '&nbsp;&amp;&lt;&gt;&quot;&#39;&apos;',
  '&amp;nbsp; stays a literal entity',
  '&amp;amp;',
  '  leading and trailing  ',
  'line\n\n\n\nblank runs collapse',
  '  a  \n\n  b  \n\n\n  c  ',
  '\n\n\n',
  'trailing newline\n',
  'tabs\there',
  '日本語<br>テキスト',
  'emoji 🎴<br>🎴',
  'RTL: مرحبا<br>بالعالم',
  '<b>bold</b> and <i>italic</i>',
  'a<br>\n<br>b',
  'unclosed <b>tag',
  '<>empty tag',
  'nbsp inside',
  'line separator',
];

const PY_STRIP = `
import json, logging, os
os.environ.setdefault('PREP_AUTH_MODE', 'fake')
logging.disable(logging.CRITICAL)
from prep.decks.anki import _strip_html
cases = json.loads(${JSON.stringify(JSON.stringify(STRIP_CASES))})
print(json.dumps([_strip_html(c) for c in cases]))
`;

describe('stripHtml', () => {
  it('matches Python over every pass it makes', () => {
    const expected = pythonJson<string[]>(PY_STRIP);
    expect(STRIP_CASES.map(stripHtml)).toEqual(expected);
  });
});

const note = (id: number, ...fields: string[]) => ({ id, flds: fields.join('\x1f') });

describe('ankiNotesToDeck', () => {
  it('maps the first field to the prompt and joins the rest with a blank line', () => {
    const c = cell();
    const outcome = ankiNotesToDeck(c.repos, 'imported', [note(1, 'Front', 'Back one', 'Back two')]);
    expect(outcome).toMatchObject({ deck_name: 'imported', inserted: 1, skipped_duplicates: 0, cloze_skipped: 0, errors: [] });
    const deckId = c.repos.decks.findId('imported')!;
    const card = c.repos.questions.listInDeck(deckId)[0]!;
    expect(card).toMatchObject({ type: 'short', prompt: 'Front', answer: 'Back one\n\nBack two' });
  });

  it('drops empty middle fields before the join', () => {
    const c = cell();
    ankiNotesToDeck(c.repos, 'd', [note(1, 'Front', '', 'Only this')]);
    expect(c.repos.questions.listInDeck(c.repos.decks.findId('d')!)[0]!.answer).toBe('Only this');
  });

  it('counts a cloze note rather than erroring on it', () => {
    const c = cell();
    const outcome = ankiNotesToDeck(c.repos, 'd', [note(1, 'The capital of {{c1::France}}', 'x'), note(2, 'Plain', 'y')]);
    expect(outcome).toMatchObject({ inserted: 1, cloze_skipped: 1, errors: [] });
  });

  it('only treats a numbered cloze marker as cloze', () => {
    const c = cell();
    expect(ankiNotesToDeck(c.repos, 'd', [note(1, '{{c::no digits}}', 'x')])).toMatchObject({ inserted: 1, cloze_skipped: 0 });
  });

  it('names each unusable note in errors and imports the rest', () => {
    const c = cell();
    const outcome = ankiNotesToDeck(c.repos, 'd', [
      note(11, ''),
      note(12, '<img src="x.png">', 'answer'),
      note(13, 'Only a front'),
      note(14, 'Good', 'Answer'),
    ]);
    expect(outcome.inserted).toBe(1);
    expect(outcome.errors).toEqual([
      'note 11: empty fields',
      'note 12: empty prompt after HTML strip',
      'note 13: no back-side content',
    ]);
  });

  it('skips a prompt already in the deck, and a repeat within one file', () => {
    const c = cell();
    ankiNotesToDeck(c.repos, 'd', [note(1, 'Front', 'A')]);
    const outcome = ankiNotesToDeck(c.repos, 'd', [note(2, 'Front', 'B'), note(3, 'Front', 'C'), note(4, 'Other', 'D')]);
    expect(outcome).toMatchObject({ inserted: 1, skipped_duplicates: 2 });
  });

  it('appends to an existing deck rather than making a second one', () => {
    const c = cell();
    const first = ankiNotesToDeck(c.repos, 'd', [note(1, 'A', 'a')]);
    const second = ankiNotesToDeck(c.repos, 'd', [note(2, 'B', 'b')]);
    expect(second.deck_id).toBe(first.deck_id);
    expect(c.repos.questions.listInDeck(first.deck_id)).toHaveLength(2);
  });

  it('stops at the cap, keeps what it inserted, and says so', () => {
    const c = cell();
    const notes = Array.from({ length: 5 }, (_, i) => note(i + 1, `Q${i}`, `A${i}`));
    const outcome = ankiNotesToDeck(c.repos, 'd', notes, { noteCap: 3 });
    expect(outcome.inserted).toBe(3);
    expect(outcome.errors).toEqual(['stopped at 3 rows; split the file and import again']);
    expect(c.repos.questions.listInDeck(outcome.deck_id)).toHaveLength(3);
  });

  it('says nothing about a cap the file did not reach', () => {
    const c = cell();
    expect(ankiNotesToDeck(c.repos, 'd', [note(1, 'A', 'a')], { noteCap: 1 }).errors).toEqual([]);
  });

  it('groups the digits of the cap the way the message the user reads does', () => {
    expect(rowCapMessage(MAX_IMPORT_ROWS)).toBe('stopped at 5,000 rows; split the file and import again');
  });

  it('reports a refused write per note instead of sinking the import', () => {
    const c = cell();
    const deckId = c.repos.decks.getOrCreate('d');
    const original = c.repos.questions.add.bind(c.repos.questions);
    let calls = 0;
    c.repos.questions.add = (id, q) => {
      if (++calls === 1) throw new Error('disk on fire');
      return original(id, q);
    };
    const outcome = ankiNotesToDeck(c.repos, 'd', [note(1, 'A', 'a'), note(2, 'B', 'b')]);
    expect(outcome).toMatchObject({ deck_id: deckId, inserted: 1 });
    expect(outcome.errors).toEqual(['note 1: write failed — disk on fire']);
  });
});

describe('the measured ceilings', () => {
  it('names the megabytes of each cap in the message the importer page shows', () => {
    expect(uploadTooLarge(MAX_APKG_UPLOAD_BYTES)).toBe('That file is too large. The limit is 8 MB.');
    expect(uploadTooLarge(MAX_PREPDECK_UPLOAD_BYTES)).toBe('That file is too large. The limit is 2 MB.');
    expect(uploadTooLarge(MAX_CSV_UPLOAD_BYTES)).toBe('That file is too large. The limit is 1.5 MB.');
  });
});
