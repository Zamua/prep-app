// The read cache against the rows it stands in for. Randomized sequences of
// writes, reviews, study steps, clock moves, rollbacks, dropped caches and
// changes made behind the cache's back; after every step the alarm's plan
// inputs, the wake they arm, and the reads a request repeats must equal the
// same reads made directly on the rows, and every session's held pick floor
// must lie at or below each card the session could still be shown.
import { describe, expect, it } from 'vitest';
import type { NotificationPrefs } from '../../app/entities.js';
import { WORKFLOW_TYPE } from '../../app/jobs/graph.js';
import { canGenerate, nextWakeAt, readWakeInputs } from '../../app/notify/wake.js';
import type { UserRepos } from '../../app/ports.js';
import { SeededRandom, SeededSessionIds } from '../../runtime/adapters/random.js';
import { DUE_BUCKET } from '../../runtime/adapters/sql/cardRepo.js';
import { HeldPickFloors } from '../../runtime/adapters/sql/dueFloor.js';
import { migrate, USER_MIGRATIONS, userRepos } from '../../runtime/adapters/sql/index.js';
import { ReadCachingStorage, readCacheOf } from '../../runtime/adapters/sql/readCache.js';
import { Db } from '../../runtime/adapters/sql/storage.js';
import { FakeCellStorage } from '../fakes/sqlStorage.js';
import { D, H, M, MutableClock, TEST_NOW, USER } from './setup.js';

const iso = (ms: number) => new Date(ms).toISOString().replace('.000Z', '+00:00');

function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

interface World {
  raw: FakeCellStorage;
  storage: ReadCachingStorage;
  clock: MutableClock;
  /** Through the cache. */
  repos: UserRepos;
  /** Straight to the rows. */
  direct: UserRepos;
  db: Db;
  sessions: Map<string, number>;
  n: number;
}

function world(seed: number): World {
  const raw = new FakeCellStorage();
  migrate(raw.sql, USER_MIGRATIONS);
  const storage = new ReadCachingStorage(raw);
  const clock = new MutableClock(TEST_NOW);
  let counter = 0;
  const sessionIds = new SeededSessionIds({ get: async () => counter, set: async (n) => void (counter = n) });
  const repos = userRepos(storage, { clock, random: new SeededRandom(seed), fuzz: false, sessionIds });
  const direct = userRepos(raw, { clock, random: new SeededRandom(seed), fuzz: false, sessionIds });
  return { raw, storage, clock, repos, direct, db: new Db(raw.sql), sessions: new Map(), n: 0 };
}

const ids = (w: World, sql: string, ...b: unknown[]): number[] => w.db.all<{ id: number }>(sql, ...b).map((r) => Number(r.id));

function seedWorld(w: World, r: () => number): void {
  const now = w.clock.now().getTime();
  w.repos.prefs.upsert(USER, { email: USER, displayName: 'Seed' });
  w.repos.pushSubs.upsert('https://push.example.test/1', 'p', 'a');
  w.repos.prefs.setNotificationPrefs({ ...w.repos.prefs.getNotificationPrefs(), mode: 'when-ready', threshold: 3 });
  for (const name of ['alpha', 'beta', 'gamma']) {
    const deck = w.repos.decks.create(name);
    for (let i = 0; i < 6 + Math.floor(r() * 12); i++) {
      const qid = w.repos.questions.add(deck, { type: 'short', prompt: `${name}-${++w.n}`, answer: 'a' });
      w.repos.cards.restoreCardState(qid, { next_due: iso(now + Math.floor((r() - 0.7) * 10) * H + Math.floor(r() * 60) * M) });
    }
  }
  const trivia = w.repos.decks.createTrivia('quiz', { topic: 'facts', intervalMinutes: 30 });
  for (let i = 0; i < 6; i++) w.repos.trivia.appendCard(w.repos.questions.add(trivia, { type: 'short', prompt: `t-${++w.n}`, answer: 'a' }), trivia);
}

const srsDecks = (w: World) => ids(w, "SELECT id FROM decks WHERE COALESCE(deck_type, 'srs') = 'srs' ORDER BY id");
const triviaDecks = (w: World) => ids(w, "SELECT id FROM decks WHERE deck_type = 'trivia' ORDER BY id");
const srsCards = (w: World) => ids(w, "SELECT q.id FROM questions q JOIN decks d ON d.id = q.deck_id WHERE COALESCE(d.deck_type, 'srs') = 'srs' ORDER BY q.id");
const triviaCards = (w: World) => ids(w, 'SELECT question_id AS id FROM trivia_queue ORDER BY question_id');

/** What the original two statements would choose from for this session now. */
function candidates(w: World, sid: string, deck: number): Set<number> {
  const now = iso(w.clock.now().getTime());
  const eligible = `q.deck_id = ? AND COALESCE(q.suspended, 0) = 0
    AND NOT EXISTS (SELECT 1 FROM study_session_answers a WHERE a.session_id = ? AND a.question_id = q.id)`;
  const first = w.db.first<{ bucket: string }>(
    `SELECT ${DUE_BUCKET} AS bucket FROM cards CROSS JOIN questions q ON q.id = cards.question_id
      WHERE cards.next_due <= ? AND ${eligible} ORDER BY cards.next_due LIMIT 1`,
    now,
    deck,
    sid,
  );
  if (first === null) return new Set();
  return new Set(
    ids(
      w,
      `SELECT q.id FROM cards CROSS JOIN questions q ON q.id = cards.question_id
        WHERE cards.next_due >= ? AND cards.next_due < ? || char(1114111) AND cards.next_due <= ?
          AND ${DUE_BUCKET} = ? AND ${eligible}`,
      first.bucket,
      first.bucket,
      now,
      first.bucket,
      deck,
      sid,
    ),
  );
}

type Step = (w: World, r: () => number) => Promise<void> | void;

/** A card a session is showing cannot be deleted; the refusal is a step too. */
function refusable(fn: () => unknown): void {
  try {
    fn();
  } catch (e) {
    if (!(e instanceof Error && /FOREIGN KEY/.test(e.message))) throw e;
  }
}

const pick = <T>(r: () => number, xs: readonly T[]): T | undefined => (xs.length ? xs[Math.floor(r() * xs.length)] : undefined);

const write = (w: World, r: () => number, repos: UserRepos): void => {
  const qid = pick(r, srsCards(w));
  const roll = r();
  if (qid !== undefined && roll < 0.4) repos.cards.restoreCardState(qid, { next_due: iso(w.clock.now().getTime() + Math.floor((r() - 0.6) * 48) * H) });
  else if (qid !== undefined && roll < 0.6) repos.questions.setSuspended(qid, r() < 0.5);
  else if (roll < 0.8) repos.prefs.setNotificationPrefs({ ...repos.prefs.getNotificationPrefs(), threshold: Math.floor(r() * 5) });
  else {
    const deck = pick(r, srsDecks(w));
    if (deck !== undefined) repos.decks.setNotificationsEnabled(deck, r() < 0.5);
  }
};

const STEPS: Record<string, [weight: number, step: Step]> = {
  review: [
    8,
    (w, r) => {
      const qid = pick(r, srsCards(w));
      if (qid !== undefined) w.repos.reviews.record(qid, r() < 0.7 ? 'right' : 'wrong', 'a');
    },
  ],
  study: [
    12,
    async (w, r) => {
      const open = [...w.sessions].filter(([sid]) => w.repos.sessions.get(sid)?.status === 'active');
      if (open.length === 0 || r() < 0.15) {
        const deck = pick(r, srsDecks(w));
        if (deck === undefined) return;
        const sid = await w.repos.sessions.create(deck, 'x');
        w.sessions.set(sid, deck);
        return;
      }
      const [sid, deck] = pick(r, open)!;
      let s = w.repos.sessions.get(sid)!;
      if (s.current_question_id !== null && s.state === 'awaiting-answer') {
        const qid = s.current_question_id;
        const result = r() < 0.7 ? 'right' : 'wrong';
        w.repos.tx.sync(() => {
          const recorded = w.repos.reviews.record(qid, result, 'a');
          w.repos.sessions.recordAnswerSync(sid, qid, s.version, 'a', { result }, recorded as unknown as Record<string, unknown>);
        });
        s = w.repos.sessions.get(sid)!;
      }
      const expected = candidates(w, sid, deck);
      w.repos.sessions.advance(sid, s.version);
      const next = w.repos.sessions.get(sid)!.current_question_id;
      if (expected.size === 0) expect(next).toBeNull();
      else expect(expected.has(next!)).toBe(true);
    },
  ],
  add: [
    3,
    (w, r) => {
      const deck = pick(r, srsDecks(w));
      if (deck !== undefined) w.repos.questions.add(deck, { type: 'short', prompt: `added-${++w.n}`, answer: 'a' });
    },
  ],
  move: [
    1,
    (w, r) => {
      const qid = pick(r, srsCards(w));
      const deck = pick(r, srsDecks(w));
      if (qid !== undefined && deck !== undefined) w.repos.questions.moveToDeck([qid], deck);
    },
  ],
  deleteCard: [
    1,
    (w, r) => {
      const qid = pick(r, [...srsCards(w), ...triviaCards(w)]);
      if (qid !== undefined) refusable(() => w.repos.questions.delete(qid));
    },
  ],
  decks: [
    1,
    (w, r) => {
      const names = w.db.all<{ name: string }>("SELECT name FROM decks WHERE COALESCE(deck_type, 'srs') = 'srs'").map((d) => d.name);
      if (names.length > 2 && r() < 0.3) refusable(() => w.repos.decks.delete(pick(r, names)!));
      else w.repos.decks.create(`deck-${++w.n}`);
    },
  ],
  writes: [6, (w, r) => write(w, r, w.repos)],
  trivia: [
    4,
    async (w, r) => {
      const deck = pick(r, triviaDecks(w));
      if (deck === undefined) return;
      const roll = r();
      const qid = pick(r, triviaCards(w));
      if (roll < 0.25) w.repos.trivia.appendCard(w.repos.questions.add(deck, { type: 'short', prompt: `t-${++w.n}`, answer: 'a' }), deck);
      else if (roll < 0.5 && qid !== undefined) w.repos.trivia.markAnswered(qid, r() < 0.5);
      else if (roll < 0.65) await w.repos.trivia.replaceActive(deck, { queue: triviaCards(w).slice(0, 2) });
      else if (roll < 0.8) w.repos.decks.recordNotificationFire(deck, iso(w.clock.now().getTime() - Math.floor(r() * 120) * M), Math.floor(r() * 4));
      else if (roll < 0.9) w.repos.decks.muteNotificationsUntil(deck, r() < 0.5 ? null : iso(w.clock.now().getTime() + 2 * H));
      else w.repos.decks.setTriviaSessionSize(deck, 1 + Math.floor(r() * 5));
    },
  ],
  prefs: [
    3,
    (w, r) => {
      const prefs: NotificationPrefs = {
        ...w.repos.prefs.getNotificationPrefs(),
        mode: pick(r, ['off', 'digest', 'when-ready'])!,
        threshold: Math.floor(r() * 6),
        digest_hour: Math.floor(r() * 24),
        quiet_hours_enabled: r() < 0.3,
        last_when_ready_at: r() < 0.5 ? null : iso(w.clock.now().getTime() - Math.floor(r() * 6) * H),
      };
      w.repos.prefs.setNotificationPrefs(prefs);
    },
  ],
  account: [
    3,
    (w, r) => {
      const roll = r();
      if (roll < 0.3) w.repos.prefs.upsert(USER, { email: USER });
      else if (roll < 0.45) w.repos.prefs.touch();
      else if (roll < 0.6) w.repos.pushSubs.upsert(`https://push.example.test/${Math.floor(r() * 3)}`, 'p', 'a');
      else if (roll < 0.7) w.repos.pushSubs.deleteByEndpoint(`https://push.example.test/${Math.floor(r() * 3)}`);
      else if (roll < 0.8) w.repos.byok.store('anthropic-api', 'c', 'sk-');
      else if (roll < 0.9) w.repos.byok.delete('anthropic-api');
      else w.repos.prefs.setActiveByokProvider(r() < 0.5 ? null : 'anthropic-api');
    },
  ],
  jobs: [
    3,
    (w, r) => {
      const roll = r();
      const wid = `job-${Math.floor(r() * 6)}`;
      if (roll < 0.4) {
        w.repos.jobs.register({ workflowId: `job-${++w.n}`, workflowType: r() < 0.6 ? WORKFLOW_TYPE.TriviaGenerate : 'grading', deckId: pick(r, triviaDecks(w)) ?? null, deckName: 'quiz', urlPath: '/x' });
      } else if (roll < 0.8) {
        const job = w.db.first<{ workflow_id: string }>('SELECT workflow_id FROM active_workflows WHERE terminal_at IS NULL ORDER BY workflow_id LIMIT 1');
        if (job) w.repos.jobs.setTerminalAt(job.workflow_id, iso(w.clock.now().getTime() - Math.floor(r() * 48) * H));
      } else w.repos.jobs.pruneTerminalOlderThan();
      void wid;
    },
  ],
  rollback: [
    2,
    (w, r) => {
      expect(() =>
        w.repos.tx.sync(() => {
          write(w, r, w.repos);
          check(w, r() < 0.5);
          throw new Error('rolled back');
        }),
      ).toThrow('rolled back');
    },
  ],
  behind: [2, (w, r) => write(w, r, w.direct)],
  behindThenOwn: [
    2,
    (w, r) => {
      write(w, r, w.direct);
      w.repos.prefs.touch();
    },
  ],
  burst: [
    1,
    (w, r) => {
      const deck = pick(r, [...w.sessions.values()]);
      const qid = deck === undefined ? undefined : pick(r, ids(w, 'SELECT id FROM questions WHERE deck_id = ?', deck));
      if (qid !== undefined) w.repos.cards.restoreCardState(qid, { next_due: iso(w.clock.now().getTime() - Math.floor(r() * 30) * H) });
      for (let i = 0; i < 70; i++) w.repos.prefs.touch();
    },
  ],
  clock: [
    6,
    (w, r) => {
      const moves = [1_000, 59_000, M, 7 * M, H, 3 * H, D, -M, -H];
      w.clock.advance(pick(r, moves)! + Math.floor(r() * 1000));
    },
  ],
  forget: [1, (w) => w.storage.forgetReads()],
  storage: [
    1,
    async (w, r) => {
      if (r() < 0.5) await w.storage.put('k', r());
      else await w.storage.setAlarm(w.clock.now().getTime() + H);
    },
  ],
};

/** `planFirst` alternates which read meets a stale result first. */
function check(w: World, planFirst = true): void {
  const can = (repos: UserRepos) => canGenerate({ repos, freeTierConfigured: true, jobsEnabled: true });
  const plan = () => {
    expect(readWakeInputs(w.repos, w.clock, can(w.repos))).toEqual(readWakeInputs(w.direct, w.clock, can(w.direct)));
    expect(nextWakeAt(w.repos, w.clock, can(w.repos))).toEqual(nextWakeAt(w.direct, w.clock, can(w.direct)));
  };
  if (planFirst) plan();

  const both = <T>(read: (repos: UserRepos) => T) => expect(read(w.repos)).toEqual(read(w.direct));
  both((x) => x.tombstone.get());
  both((x) => x.prefs.get());
  both((x) => [x.prefs.getNotificationPrefs(), x.prefs.getActiveByokProvider(), x.prefs.getDesiredRetention(), x.prefs.getEditorInputMode(), x.prefs.getIdBase()]);
  both((x) => [x.cards.countDue(), x.cards.countDue(1), x.cards.countDue(4), x.cards.nextDueMinutes()]);
  both((x) => [x.byok.listProviders(), x.pushSubs.count(), x.notify.countUnseen(), x.jobs.earliestTerminalAt()]);
  both((x) => x.decks.listNames());
  for (const deck of [...srsDecks(w), ...triviaDecks(w)]) {
    both((x) => [x.decks.findName(deck), x.decks.getType(deck), x.cards.nextDueMinutes(deck), x.trivia.queueCounts(deck, { queued: 1, unanswered: 3 }), x.trivia.getActiveSessionForDeck(deck)]);
  }
  for (const qid of srsCards(w).slice(0, 8)) both((x) => [x.questions.get(qid), x.cards.srsState(qid)]);
  for (const sid of w.sessions.keys()) both((x) => x.sessions.get(sid));

  const floors = new HeldPickFloors(readCacheOf(w.storage)!);
  for (const [sid, deck] of w.sessions) {
    const floor = floors.floor(sid, deck);
    if (floor === null) continue;
    const showable = w.db.all<{ nd: string }>(
      `SELECT cards.next_due AS nd FROM cards JOIN questions q ON q.id = cards.question_id
        WHERE q.deck_id = ? AND COALESCE(q.suspended, 0) = 0
          AND NOT EXISTS (SELECT 1 FROM study_session_answers a WHERE a.session_id = ? AND a.question_id = q.id)`,
      deck,
      sid,
    );
    for (const { nd } of showable) expect(nd >= floor, `${sid}: ${nd} under its floor ${floor}`).toBe(true);
  }
  if (!planFirst) plan();
}

describe('the read cache, against the rows it stands in for', () => {
  const steps = Object.entries(STEPS);
  const total = steps.reduce((n, [, [weight]]) => n + weight, 0);

  for (const seed of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]) {
    it(`agrees after every step of sequence ${seed}`, async () => {
      const w = world(seed);
      const r = lcg(seed * 7919);
      seedWorld(w, r);
      check(w);
      const ran: string[] = [];
      for (let i = 0; i < 250; i++) {
        let roll = r() * total;
        const [name, [, step]] = steps.find(([, [weight]]) => (roll -= weight) < 0) ?? steps[0]!;
        ran.push(name);
        await step(w, r);
        try {
          check(w, i % 2 === 0);
        } catch (e) {
          throw new Error(`after step ${i} (${ran.slice(-6).join(' > ')}): ${e instanceof Error ? e.message : String(e)}`);
        }
      }
    });
  }
});
