// What a request costs in SQLite rows read, the unit the platform bills.
//
// Two pins over the same recorded day. The plans pin always runs: every loop
// the study and authoring requests run is bounded by current work, never by
// how much history the account holds. The workerd pin replays the day in
// workerd and holds each request to a rows-read ceiling on a lived-in account
// and on one with three times its history; it runs where a workerd binary is
// found (`PREP_WORKERD`, or the `workerd` package).
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { workerEnv } from '../api/harness.js';
import { buildAccount, DECKS, NOW, REALISTIC, type Account, type AccountShape } from './account.js';
import { Day } from './day.js';
import { unboundedLoops } from './plans.js';
import { Recorder, snapshot, type Snapshot } from './recorder.js';
import { locateWorkerd, Workerd, type Tally } from './workerd.js';

/** The requests a study session and card authoring send, and their ceilings. */
const CEILINGS: Record<string, number> = {
  'session begin': 32,
  'review submit': 30,
  'session advance': 45,
  'mcp add card': 36,
  'in-app add card': 35,
  'mcp edit card': 30,
};

interface RecordedDay {
  account: Account;
  snap: Snapshot;
  rec: Recorder;
}

async function recordDay(shape: AccountShape): Promise<RecordedDay> {
  const account = await buildAccount(workerEnv(), shape);
  const snap = snapshot(account.storage);
  const rec = new Recorder(account.storage);
  const day = new Day(account.env, account);
  await rec.during('warm', () => day.dashboard());
  for (const deck of [DECKS[1]!, DECKS[2]!]) {
    await rec.during('session begin', () => day.begin(deck));
    for (let i = 0; i < 6; i++) {
      await rec.during('review submit', async () => expect(await day.submit(i % 4 !== 3)).toBe(true));
      await rec.during('session advance', () => day.advance());
    }
  }
  const added: number[] = [];
  for (let i = 0; i < 2; i++) {
    await rec.during('mcp add card', async () => void added.push(await day.mcpAddCard(DECKS[i]!, i)));
    await rec.during('in-app add card', () => day.appAddCard(DECKS[i]!, i));
    await rec.during('mcp edit card', () => day.mcpEditCard(added[i]!, i));
    await rec.during('dashboard', () => day.dashboard());
    await rec.during('deck page', () => day.deckPage(DECKS[1]!));
  }
  return { account, snap, rec };
}

const HISTORY: AccountShape = { ...REALISTIC, history: 3 };

let lived: RecordedDay;
let older: RecordedDay;

beforeAll(async () => {
  lived = await recordDay(REALISTIC);
  older = await recordDay(HISTORY);
}, 60_000);

describe('the plans behind a study session and card authoring', () => {
  for (const label of Object.keys(CEILINGS)) {
    it(`${label}: every loop is bounded by current work, not history`, () => {
      const statements = lived.rec.statements.filter((s) => s.label === label);
      expect(statements.length).toBeGreaterThan(0);
      expect(unboundedLoops(lived.account.storage.db, statements)).toEqual([]);
    });
  }
});

const WORKERD = locateWorkerd();

describe.skipIf(WORKERD === null)('rows read per request, measured by workerd', () => {
  let workerd: Workerd;
  const tallies = new Map<RecordedDay, Map<string, Tally>>();
  const perRequest = (day: RecordedDay, label: string) => {
    const t = tallies.get(day)!.get(label)!;
    return t.read / t.requests;
  };

  beforeAll(async () => {
    workerd = await Workerd.start(WORKERD!);
    for (const day of [lived, older]) tallies.set(day, await workerd.replay(day.snap, day.rec.statements, day.rec.counts));
    if (process.env['PREP_ROWS_REPORT']) {
      const rows = [...tallies.get(lived)!.keys()].map((label) => ({
        label,
        read: Math.round(perRequest(lived, label)),
        'read, 3x history': Math.round(perRequest(older, label)),
        written: Math.round(tallies.get(lived)!.get(label)!.written / tallies.get(lived)!.get(label)!.requests),
      }));
      console.table(rows);
    }
  }, 60_000);

  afterAll(() => workerd?.stop());

  for (const [label, ceiling] of Object.entries(CEILINGS)) {
    it(`${label}: at most ${ceiling} rows, with three times the history too`, () => {
      expect(perRequest(lived, label)).toBeLessThanOrEqual(ceiling);
      expect(perRequest(older, label)).toBeLessThanOrEqual(ceiling);
    });
  }

  it('dashboard: one row per card it totals and two per card due, never the review history', () => {
    for (const day of [lived, older]) {
      const db = day.account.storage.db;
      const cards = (db.prepare('SELECT COUNT(*) AS n FROM questions').get() as { n: number }).n;
      const due = (db.prepare('SELECT COUNT(*) AS n FROM cards WHERE next_due <= ?').get(NOW.replace('Z', '+00:00')) as { n: number }).n;
      expect(perRequest(day, 'dashboard')).toBeLessThanOrEqual(1.5 * cards + 2 * due + 200);
    }
  });

  it("deck page: bounded by that deck's own cards and their reviews", () => {
    for (const day of [lived, older]) {
      const db = day.account.storage.db;
      const deck = day.account.deckIds.get(DECKS[1]!)!;
      const cards = (db.prepare('SELECT COUNT(*) AS n FROM questions WHERE deck_id = ?').get(deck) as { n: number }).n;
      const reviews = (db.prepare('SELECT COUNT(*) AS n FROM reviews r JOIN questions q ON q.id = r.question_id WHERE q.deck_id = ?').get(deck) as { n: number }).n;
      expect(perRequest(day, 'deck page')).toBeLessThanOrEqual(4 * cards + 1.5 * reviews + 100);
    }
  });
});
