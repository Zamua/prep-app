// The `.apkg` a deck export writes. Anki reads the package's sqlite, so the
// gate is the rows as they land in it: a note whose model, field count or
// deck is not the one the `col` row declares is refused by the importer, and
// the guid is what makes a second import an update instead of a duplicate.
import { describe, expect, it } from 'vitest';
import { buildApkg, buildQuestionBody, fieldChecksum } from '../app/decks/ankiExport.js';
import type { Question } from '../app/entities.js';
import { SqlJsApkg, sqlEngine } from '../runtime/adapters/apkg.js';
import { FflateZip } from '../runtime/adapters/zip.js';

const NOW_MS = Date.parse('2026-03-14T15:00:00Z');
const SUBJECT = 'user_2abcdefghijklmno';

const question = (over: Partial<Question> & { id: number }): Question => ({
  deck_id: 1,
  type: 'short',
  topic: null,
  prompt: 'p',
  choices: null,
  answer: 'a',
  rubric: null,
  created_at: '2026-03-01T00:00:00+00:00',
  suspended: false,
  skeleton: null,
  language: null,
  explanation: null,
  answer_regex: null,
  ...over,
});

const QUESTIONS: readonly Question[] = [
  question({ id: 1, prompt: 'Capital of Peru?', answer: 'Lima', topic: 'capitals' }),
  question({ id: 2, type: 'mcq', prompt: 'Which one?\nsecond line', choices: ['Austria', 'Poland'], answer: 'Austria' }),
  question({ id: 3, type: 'multi', prompt: '日本の首都は？ 🎴', choices: ['Kyoto', 'Tokyo'], answer: '["Tokyo"]' }),
  question({ id: 4, type: 'code', prompt: 'Reverse a list', answer: 'l[::-1]', language: 'python', skeleton: 'def f(l):', rubric: 'must not mutate', explanation: 'slicing' }),
];

const built = buildApkg('capitals', QUESTIONS, SUBJECT, NOW_MS, '2026-03-14');

describe('the collection a deck export writes', () => {
  it('declares every model, deck and config its rows point at', () => {
    const models = built.col.models as Record<string, { id: number; flds: unknown[]; tmpls: unknown[]; did: number }>;
    const decks = built.col.decks as Record<string, { id: number }>;
    const dconf = built.col.dconf as Record<string, unknown>;
    const conf = built.col.conf as { curModel: number; curDeck: number };

    for (const note of built.notes) {
      const model = models[String(note.mid)];
      expect(model, `note ${note.id} names model ${note.mid}`).toBeDefined();
      // Anki drops a note whose field count is not the model's.
      expect(note.flds.split('\x1f')).toHaveLength(model!.flds.length);
      expect(decks[String(model!.did)]).toBeDefined();
    }
    for (const card of built.cards) {
      const note = built.notes.find((n) => n.id === card.nid);
      expect(note, `card ${card.id} names note ${card.nid}`).toBeDefined();
      expect(decks[String(card.did)]).toBeDefined();
      expect(card.ord).toBeLessThan(models[String(note!.mid)]!.tmpls.length);
    }
    expect(models[String(conf.curModel)]).toBeDefined();
    expect(decks[String(conf.curDeck)]).toBeDefined();
    expect(Object.keys(dconf)).toContain('1');
  });

  it('gives every note and card its own id, and every note a stable guid', () => {
    const ids = [...built.notes.map((n) => n.id), ...built.cards.map((c) => c.id)];
    expect(new Set(ids).size).toBe(ids.length);
    expect(built.notes.map((n) => n.guid)).toEqual(['prep-user_2ab-1', 'prep-user_2ab-2', 'prep-user_2ab-3', 'prep-user_2ab-4']);
    // The guid is what a re-import matches on, so it may not move with the clock.
    const later = buildApkg('capitals', QUESTIONS, SUBJECT, NOW_MS + 86_400_000, '2026-03-15');
    expect(later.notes.map((n) => n.guid)).toEqual(built.notes.map((n) => n.guid));
  });

  it('sorts and checksums on the front field, which is the model’s sort field', () => {
    const models = built.col.models as Record<string, { sortf: number }>;
    for (const note of built.notes) {
      const front = note.flds.split('\x1f')[models[String(note.mid)]!.sortf]!;
      expect(note.csum).toBe(fieldChecksum(front));
      expect(front.startsWith(String(note.sfld))).toBe(true);
    }
    // Anki's own duplicate check is the checksum: same front, same number.
    expect(fieldChecksum('Capital of Peru?')).toBe(1899195448);
  });

  it('writes every question as a never-studied card in the exported deck', () => {
    const decks = built.col.decks as Record<string, { name: string }>;
    expect(built.cards).toHaveLength(QUESTIONS.length);
    for (const card of built.cards) {
      expect(decks[String(card.did)]!.name).toBe('capitals');
      // type 0 / queue 0 is Anki's "new"; a due position, no interval, no reps.
      expect({ type: card.type, queue: card.queue, ivl: card.ivl, reps: card.reps, lapses: card.lapses }).toEqual({ type: 0, queue: 0, ivl: 0, reps: 0, lapses: 0 });
    }
    expect(built.cards.map((c) => c.due)).toEqual([1, 2, 3, 4]);
  });

  it('marks the sections of a card up rather than laying them out with newlines', () => {
    expect(buildQuestionBody(QUESTIONS[1]!)[0]).toBe('Which one?<br>second line<br><br>• Austria<br>• Poland');
    const [, back] = buildQuestionBody(QUESTIONS[3]!);
    expect(back).toContain('<b>Explanation:</b><br>slicing');
    expect(back).toContain('<b>Rubric:</b><br>must not mutate');
    expect(back).toContain('<b>Skeleton (python):</b><br><pre>def f(l):</pre>');
    expect(back).not.toContain('\n');
  });
});

describe('the package those rows are written into', () => {
  it('lands in the collection sqlite as one col row, four notes and four cards', async () => {
    const blob = await new SqlJsApkg().build(built.col, built.notes, built.cards);
    const collection = new FflateZip().read(blob).find((e) => e.name === 'collection.anki21')!.bytes;
    const SQL = await sqlEngine();
    const db = new SQL.Database(collection);
    try {
      const one = (sql: string) => db.exec(sql)[0]!;
      expect(one('SELECT count(*) FROM col').values).toEqual([[1]]);
      expect(one('SELECT id, guid, mid, flds, sfld, csum FROM notes ORDER BY id').values).toEqual(
        built.notes.map((n) => [n.id, n.guid, n.mid, n.flds, n.sfld, n.csum]),
      );
      expect(one('SELECT id, nid, did, ord, due FROM cards ORDER BY id').values).toEqual(
        built.cards.map((c) => [c.id, c.nid, c.did, c.ord, c.due]),
      );
      // An importer refuses a schema it does not recognise, so the tables and
      // the version the `col` row claims both have to be there.
      expect(one("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").values.flat()).toEqual(['cards', 'col', 'graves', 'notes', 'revlog']);
      expect(one('SELECT ver FROM col').values).toEqual([[11]]);
      for (const column of ['models', 'decks', 'dconf', 'conf'] as const) {
        expect(() => JSON.parse(String(one(`SELECT ${column} FROM col`).values[0]![0])), column).not.toThrow();
      }
    } finally {
      db.close();
    }
  });

  it('reads back through our own importer as the questions that went in', async () => {
    const blob = await new SqlJsApkg().build(built.col, built.notes, built.cards);
    const notes = await new SqlJsApkg().notes(blob);
    expect(notes.map((n) => n.flds.split('\x1f')[0])).toEqual(QUESTIONS.map((q) => buildQuestionBody(q)[0]));
  });
});
