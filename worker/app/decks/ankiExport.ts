// Anki `.apkg` export. Every prep question flattens to one Basic note with a
// Front and a Back field, and one never-studied card. The sqlite and the zip are the `ApkgWriter` port's job;
// this half only shapes the rows.
import { sha1Hex } from '../../domain/sha1.js';
import type { Question } from '../entities.js';
import type { ApkgCard, ApkgCollection, ApkgNoteRow } from '../ports.js';

/** Only unique within one `.apkg`; Anki reassigns on import. */
const MODEL_ID = 1_700_000_000_000;
const DECK_ID = 1_700_000_000_001;

/** Anki's `field_checksum`: the first 8 hex digits of SHA-1 over the sort
 * field, as an integer. Anki reads this to find duplicate notes, so the
 * hash has to be the one it specifies, not a convenient one. */
export function fieldChecksum(sortField: string): number {
  return parseInt(sha1Hex(sortField).slice(0, 8), 16);
}

/** (front, back) for one question. Anki note fields carry HTML, so the
 * sections are marked up rather than laid out with newlines. */
export function buildQuestionBody(q: Question): [front: string, back: string] {
  let front = q.prompt;
  if (q.choices && q.choices.length && (q.type === 'mcq' || q.type === 'multi')) {
    front = front + '<br><br>' + q.choices.map((c) => `• ${c}`).join('<br>');
  }

  const back: string[] = [q.answer];
  if (q.explanation) back.push(`<br><br><b>Explanation:</b><br>${q.explanation}`);
  if (q.rubric && q.type === 'code') back.push(`<br><br><b>Rubric:</b><br>${q.rubric}`);
  if (q.skeleton && q.type === 'code') back.push(`<br><br><b>Skeleton (${q.language || 'code'}):</b><br><pre>${q.skeleton}</pre>`);
  if (q.topic) back.push(`<br><br><i>Topic: ${q.topic}</i>`);

  return [front.split('\n').join('<br>'), back.join('').split('\n').join('<br>')];
}

/** The JSON blobs the `col` row carries. Anki refuses a collection whose
 * `models` and `decks` do not cover every note and card written below. */
export function colPayload(deckName: string, nowMs: number, exportedOn: string): Record<string, unknown> {
  const nowS = Math.floor(nowMs / 1000);
  const models = {
    [String(MODEL_ID)]: {
      id: MODEL_ID,
      name: 'Basic (prep export)',
      type: 0,
      mod: nowS,
      usn: -1,
      sortf: 0,
      did: DECK_ID,
      tmpls: [
        {
          name: 'Card 1',
          ord: 0,
          qfmt: '{{Front}}',
          afmt: '{{FrontSide}}<hr id="answer">{{Back}}',
          bqfmt: '',
          bafmt: '',
          did: null,
          bfont: '',
          bsize: 0,
        },
      ],
      flds: [
        { name: 'Front', ord: 0, sticky: false, rtl: false, font: 'Arial', size: 20 },
        { name: 'Back', ord: 1, sticky: false, rtl: false, font: 'Arial', size: 20 },
      ],
      css: '.card { font-family: arial; font-size: 20px; text-align: left; color: black; background: white; }',
      latexPre: '',
      latexPost: '',
      req: [[0, 'any', [0]]],
    },
  };
  const deckDefaults = {
    mod: nowS,
    usn: -1,
    lrnToday: [0, 0],
    revToday: [0, 0],
    newToday: [0, 0],
    timeToday: [0, 0],
    collapsed: false,
    browserCollapsed: false,
    dyn: 0,
    conf: 1,
    extendNew: 10,
    extendRev: 50,
  };
  const decks = {
    '1': { id: 1, name: 'Default', ...deckDefaults, desc: '' },
    [String(DECK_ID)]: { id: DECK_ID, name: deckName, ...deckDefaults, desc: `Exported from prep on ${exportedOn}` },
  };
  const dconf = {
    '1': {
      id: 1,
      name: 'Default',
      replayq: true,
      lapse: { leechFails: 8, minInt: 1, delays: [10], leechAction: 0, mult: 0 },
      rev: { perDay: 100, ease4: 1.3, fuzz: 0.05, minSpace: 1, ivlFct: 1, maxIvl: 36500, bury: false },
      timer: 0,
      maxTaken: 60,
      usn: -1,
      new: { perDay: 20, delays: [1, 10], separate: true, ints: [1, 4, 7], initialFactor: 2500, bury: false, order: 1 },
      mod: 0,
      autoplay: true,
    },
  };
  const conf = {
    nextPos: 1,
    estTimes: true,
    activeDecks: [1],
    sortType: 'noteFld',
    timeLim: 0,
    sortBackwards: false,
    addToCur: true,
    curDeck: 1,
    newBury: true,
    newSpread: 0,
    dueCounts: true,
    curModel: MODEL_ID,
    collapseTime: 1200,
  };
  return { models, decks, dconf, conf, tags: {} };
}

export interface ApkgExport {
  col: ApkgCollection;
  notes: ApkgNoteRow[];
  cards: ApkgCard[];
}

/** Ordered by `Question.id`, the order `questionsForExport` already yields;
 * the note id is the export instant plus the index, so ids stay unique
 * within the file the way Anki's millisecond ids do. */
export function buildApkg(
  deckName: string,
  questions: readonly Question[],
  userId: string,
  nowMs: number,
  exportedOn: string,
): ApkgExport {
  const nowS = Math.floor(nowMs / 1000);
  const payload = colPayload(deckName, nowMs, exportedOn);
  const col: ApkgCollection = {
    id: 1,
    crt: nowS,
    mod: nowS,
    scm: nowMs,
    ver: 11,
    dty: 0,
    usn: -1,
    ls: 0,
    conf: payload['conf'],
    models: payload['models'],
    decks: payload['decks'],
    dconf: payload['dconf'],
    tags: payload['tags'],
  };

  const notes: ApkgNoteRow[] = [];
  const cards: ApkgCard[] = [];
  questions.forEach((q, idx) => {
    const [front, back] = buildQuestionBody(q);
    const noteId = nowMs + idx;
    notes.push({
      id: noteId,
      guid: `prep-${userId.slice(0, 8)}-${q.id}`,
      mid: MODEL_ID,
      mod: nowS,
      usn: -1,
      tags: '',
      flds: front + '\x1f' + back,
      sfld: Array.from(front).slice(0, 200).join(''),
      csum: fieldChecksum(front),
      flags: 0,
      data: '',
    });
    cards.push({
      id: nowMs + 100000 + idx,
      nid: noteId,
      did: DECK_ID,
      ord: 0,
      mod: nowS,
      usn: -1,
      type: 0,
      queue: 0,
      due: idx + 1,
      ivl: 0,
      factor: 0,
      reps: 0,
      lapses: 0,
      left: 0,
      odue: 0,
      odid: 0,
      flags: 0,
      data: '',
    });
  });

  return { col, notes, cards };
}
