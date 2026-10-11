// The reads rewritten to seek rather than scan return what the scanning
// statements returned. Each old statement is kept here verbatim and run
// against the same randomized cell as its replacement.
import { describe, expect, it } from 'vitest';
import { DEFAULT_EDITOR_INPUT_MODE, DEFAULT_NOTIFICATION_PREFS, EDITOR_INPUT_MODES, type ActiveWorkflow } from '../../app/entities.js';
import { WORKFLOW_TYPE } from '../../app/jobs/graph.js';
import { questionsForExport } from '../../app/api/deckIo.js';
import { Db } from '../../runtime/adapters/sql/storage.js';
import { DUE_BUCKET } from '../../runtime/adapters/sql/cardRepo.js';
import { cell, D, H, M, TEST_NOW, type Cell } from './setup.js';

const NOW_ISO = '2026-03-14T15:00:00+00:00';
const iso = (ms: number) => new Date(ms).toISOString().replace('.000Z', '+00:00');

function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

/** Decks of every kind, dues piled into shared hours across decks, suspended
 * and future cards, a review history, trivia in every answer state, and job
 * rows of several types. */
function randomCell(seed: number): Cell & { srs: number[]; trivia: number } {
  const c = cell();
  const r = lcg(seed);
  const pick = <T>(xs: readonly T[]): T => xs[Math.floor(r() * xs.length)]!;
  const now = TEST_NOW.getTime();
  const hours = [-30 * H, -5 * H, -5 * H + 20 * M, -2 * H, -2 * H + 59 * M, -1, 0, 1, 3 * D];
  const srs = ['alpha', 'beta', 'gamma', 'empty'].map((name) => c.repos.decks.create(name, { displayName: r() < 0.5 ? null : name.toUpperCase() }));
  c.repos.decks.setNotificationsEnabled(srs[1]!, false);
  c.repos.pins.pinnedAt(srs[2]!, NOW_ISO);
  const trivia = c.repos.decks.createTrivia('quiz', { topic: 'facts', intervalMinutes: 30 });
  for (const deck of srs.slice(0, 3)) {
    const n = 5 + Math.floor(r() * 30);
    for (let i = 0; i < n; i++) {
      const qid = c.repos.questions.add(deck, { type: 'short', prompt: `${deck}-${i}`, answer: 'a', skeleton: r() < 0.2 ? 'x' : null });
      c.repos.cards.restoreCardState(qid, { next_due: iso(now + pick(hours)) });
      if (r() < 0.15) c.repos.questions.setSuspended(qid, true);
      const reviews = Math.floor(r() * 5);
      for (let k = 0; k < reviews; k++) c.repos.reviews.importReview(qid, iso(now - (k + 1) * D), r() < 0.7 ? 'right' : 'wrong', 'a');
    }
  }
  for (let i = 0; i < 12; i++) {
    const qid = c.repos.questions.add(trivia, { type: 'short', prompt: `t-${i}`, answer: 'a' });
    const state = pick([null, 0, 1]);
    c.repos.trivia.importEntry(qid, i + 1, state === null ? {} : { lastAnsweredAt: iso(now - i * H), lastAnsweredCorrectly: state });
  }
  const types = [WORKFLOW_TYPE.TriviaGenerate, 'grading', 'plan'];
  for (let i = 0; i < 10; i++) {
    c.clock.set(new Date(now - pick([1, 5, 30, 90]) * M - i * M));
    const wid = `w-${i}`;
    const type = pick(types);
    c.repos.jobs.register({ workflowId: wid, workflowType: type, deckId: r() < 0.8 ? pick([trivia, srs[0]!]) : null, deckName: 'quiz', urlPath: `/x/${i}` });
    if (r() < 0.6) c.repos.jobs.setTerminalAt(wid, iso(now - pick([2, 20, 200]) * M));
  }
  c.clock.set(TEST_NOW);
  return { ...c, srs, trivia };
}

const SEEDS = [1, 2, 3, 4, 5, 6];

describe('the reads that stopped scanning', () => {
  it('lists the deck summaries the single join did', () => {
    for (const seed of SEEDS) {
      const c = randomCell(seed);
      const old = new Db(c.storage.sql)
        .all(
          `SELECT d.id, d.name, d.display_name, d.deck_type, d.pinned_at,
                  COUNT(q.id) AS total,
                  SUM(CASE WHEN cards.next_due <= ? AND COALESCE(q.suspended,0)=0
                            AND COALESCE(d.deck_type,'srs') = 'srs'
                           THEN 1 ELSE 0 END) AS due
             FROM decks d
             LEFT JOIN questions q ON q.deck_id = d.id
             LEFT JOIN cards ON cards.question_id = q.id
            GROUP BY d.id
            ORDER BY (d.pinned_at IS NULL), d.pinned_at DESC, COALESCE(d.display_name, d.name)`,
          NOW_ISO,
        )
        .map((r) => ({
          id: Number(r['id']),
          name: String(r['name']),
          display_name: (r['display_name'] as string | null) ?? null,
          total: Number(r['total'] ?? 0),
          due: Number(r['due'] ?? 0),
          deck_type: (r['deck_type'] as string | null) || 'srs',
          pinned: r['pinned_at'] != null,
        }));
      expect(c.repos.decks.listSummaries()).toEqual(old);
      expect(c.repos.decks.listNames()).toEqual(old.map((d) => ({ name: d.name, display_name: d.display_name })));
    }
  });

  it("lists a deck's cards with the attempts and rights the per-card counts gave", () => {
    for (const seed of SEEDS) {
      const c = randomCell(seed);
      for (const deck of [...c.srs, c.trivia]) {
        const old = new Db(c.storage.sql).all(
          `SELECT q.id, cards.next_due,
                  (SELECT COUNT(*) FROM reviews r WHERE r.question_id=q.id) AS attempts,
                  (SELECT COUNT(*) FROM reviews r WHERE r.question_id=q.id AND r.result='right') AS rights
             FROM questions q
             LEFT JOIN cards ON cards.question_id = q.id
            WHERE q.deck_id = ?
            ORDER BY cards.next_due ASC, q.id ASC`,
          deck,
        );
        expect(c.repos.questions.listInDeck(deck).map((x) => ({ id: x.id, next_due: x.next_due, attempts: x.attempts, rights: x.rights }))).toEqual(
          old.map((r) => ({ id: Number(r['id']), next_due: r['next_due'] ?? null, attempts: Number(r['attempts']), rights: Number(r['rights']) })),
        );
        const byId = c.repos.questions
          .listInDeck(deck)
          .map((x) => x.id)
          .sort((a, b) => a - b)
          .map((id) => c.repos.questions.get(id));
        expect(questionsForExport(c.repos, deck)).toEqual(byId);
        expect(c.repos.questions.countInDeck(deck)).toBe(byId.length);
      }
    }
  });

  it('counts due cards the uncapped join did, and stops at a cap', () => {
    for (const seed of SEEDS) {
      const c = randomCell(seed);
      const old = Number(
        new Db(c.storage.sql).first<{ n: number }>(
          `SELECT COUNT(*) AS n
             FROM cards
             JOIN questions ON questions.id = cards.question_id
             JOIN decks ON decks.id = questions.deck_id
            WHERE COALESCE(questions.suspended, 0) = 0
              AND COALESCE(decks.notifications_enabled, 1) = 1
              AND COALESCE(decks.deck_type, 'srs') = 'srs'
              AND cards.next_due <= ?`,
          NOW_ISO,
        )!.n,
      );
      expect(c.repos.cards.countDue()).toBe(old);
      for (const cap of [1, 3, 1000]) expect(c.repos.cards.countDue(cap)).toBe(Math.min(old, cap));
    }
  });

  it('counts trivia queue rows as the deck stats do, up to a cap', () => {
    for (const seed of SEEDS) {
      const c = randomCell(seed);
      const stats = c.repos.trivia.deckStats(c.trivia);
      for (const cap of [1, 3, 50]) {
        expect(c.repos.trivia.queueCounts(c.trivia, { queued: cap, unanswered: cap })).toEqual({ queued: Math.min(stats.total, cap), unanswered: Math.min(stats.unanswered, cap) });
        expect(c.repos.trivia.queueCounts(c.trivia, { queued: 1, unanswered: cap })).toEqual({ queued: Math.min(stats.total, 1), unanswered: Math.min(stats.unanswered, cap) });
      }
      expect(c.repos.trivia.queueCounts(c.srs[0]!, { queued: 1, unanswered: 3 })).toEqual({ queued: 0, unanswered: 0 });
    }
  });

  it("reads each deck's last refill and the oldest terminal row as the full job list did", () => {
    for (const seed of SEEDS) {
      const c = randomCell(seed);
      const jobs: ActiveWorkflow[] = c.repos.jobs.listForUser({ recentTerminalWindowSeconds: 100 * 365 * 24 * 3600 });
      const lastRefill = new Map<number, string>();
      for (const w of jobs) {
        if (w.workflow_type !== WORKFLOW_TYPE.TriviaGenerate || w.deck_id === null) continue;
        const seen = lastRefill.get(w.deck_id);
        if (seen === undefined || w.started_at > seen) lastRefill.set(w.deck_id, w.started_at);
      }
      let oldest: string | null = null;
      for (const w of jobs) if (w.terminal_at !== null && (oldest === null || w.terminal_at < oldest)) oldest = w.terminal_at;
      expect(c.repos.jobs.lastStartedByDeck(WORKFLOW_TYPE.TriviaGenerate)).toEqual(lastRefill);
      expect(c.repos.jobs.earliestTerminalAt()).toBe(oldest);
    }
  });

  it("draws a session's next card from the hour the deck-wide sort would, across every card of it", async () => {
    for (const seed of SEEDS) {
      const c = randomCell(seed);
      for (const deck of c.srs) {
        // What the single sorted statement could return: its sort key's minimum.
        const rows = new Db(c.storage.sql).all<{ id: number; bucket: string }>(
          `SELECT q.id, ${DUE_BUCKET} AS bucket FROM questions q JOIN cards ON cards.question_id = q.id
            WHERE q.deck_id = ? AND COALESCE(q.suspended, 0) = 0 AND cards.next_due <= ?`,
          deck,
          NOW_ISO,
        );
        const first = rows.map((r) => r.bucket).sort()[0];
        const expected = new Set(rows.filter((r) => r.bucket === first).map((r) => Number(r.id)));
        const drawn = new Set<number | null>();
        for (let i = 0; i < 40; i++) {
          const sid = await c.repos.sessions.create(deck, 'x');
          drawn.add(c.repos.sessions.get(sid)!.current_question_id);
        }
        if (expected.size === 0) expect(drawn).toEqual(new Set([null]));
        else if (expected.size <= 3) expect(drawn).toEqual(expected);
        else for (const id of drawn) expect(expected.has(id!)).toBe(true);
      }
    }
  });

  it('skips the cards a session already answered, then completes', async () => {
    const c = cell();
    const deck = c.repos.decks.create('d');
    const other = c.repos.decks.create('o');
    const now = TEST_NOW.getTime();
    let n = 0;
    const add = (d: number, due: number) => {
      const qid = c.repos.questions.add(d, { type: 'short', prompt: `p${++n}`, answer: 'a' });
      c.repos.cards.restoreCardState(qid, { next_due: iso(now + due) });
      return qid;
    };
    // The other deck's cards come first in the due index and share the hour.
    add(other, -3 * H);
    add(other, -2 * H + 5 * M);
    const a = add(deck, -2 * H + 10 * M);
    const b = add(deck, -2 * H + 40 * M);
    const later = add(deck, -1 * H);
    add(deck, 2 * H);
    const sid = await c.repos.sessions.create(deck, 'x');
    const seen: number[] = [];
    let s = c.repos.sessions.get(sid)!;
    while (s.current_question_id !== null) {
      const qid = s.current_question_id;
      seen.push(qid);
      const v = c.repos.sessions.recordAnswerSync(sid, qid, s.version, 'a', { result: 'right' }, {});
      c.repos.sessions.advance(sid, v);
      s = c.repos.sessions.get(sid)!;
    }
    expect(new Set(seen.slice(0, 2))).toEqual(new Set([a, b]));
    expect(seen.slice(2)).toEqual([later]);
    expect(s.status).toBe('completed');
  });
});

describe('the reads that share one statement with a read the request already made', () => {
  it("reads each profile setting the single-column statements read", () => {
    for (const seed of SEEDS) {
      const c = randomCell(seed);
      const r = lcg(seed);
      if (r() < 0.7) c.repos.prefs.setNotificationPrefs({ ...c.repos.prefs.getNotificationPrefs(), mode: 'digest', threshold: Math.floor(r() * 9) });
      if (r() < 0.5) c.repos.prefs.setEditorInputMode(r() < 0.5 ? 'vim' : 'emacs');
      if (r() < 0.5) c.repos.prefs.setActiveByokProvider(r() < 0.5 ? 'openai-api' : '');
      if (r() < 0.5) c.repos.prefs.setDesiredRetention(r() < 0.5 ? 0.85 : null);
      if (r() < 0.5) c.repos.prefs.setIdBase(Math.floor(r() * 9));
      const db = new Db(c.storage.sql);
      const old = (column: string) => db.first(`SELECT ${column} FROM profile LIMIT 1`)?.[column];
      const prefs = old('notification_prefs');
      expect(c.repos.prefs.getNotificationPrefs()).toEqual({ ...DEFAULT_NOTIFICATION_PREFS, ...(prefs ? JSON.parse(String(prefs)) : {}) });
      const mode = old('editor_input_mode');
      expect(c.repos.prefs.getEditorInputMode()).toBe(mode && (EDITOR_INPUT_MODES as readonly unknown[]).includes(mode) ? mode : DEFAULT_EDITOR_INPUT_MODE);
      expect(c.repos.prefs.getActiveByokProvider()).toBe(old('active_byok_provider') || null);
      const retention = old('desired_retention');
      expect(c.repos.prefs.getDesiredRetention()).toBe(retention != null ? Number(retention) : null);
      expect(c.repos.prefs.getIdBase()).toBe(Number(old('id_base') ?? 0));
    }
  });

  it("reads a session's version guard fields as the narrow statement did", async () => {
    for (const seed of SEEDS) {
      const c = randomCell(seed);
      for (const deck of c.srs) {
        const sid = await c.repos.sessions.create(deck, 'x');
        const old = new Db(c.storage.sql).first('SELECT version, deck_id, status, state, current_question_id FROM study_sessions WHERE id = ?', sid)!;
        const s = c.repos.sessions.get(sid)!;
        expect({ version: s.version, deck_id: s.deck_id, status: s.status, state: s.state, current_question_id: s.current_question_id }).toEqual({
          version: Number(old['version']),
          deck_id: Number(old['deck_id']),
          status: old['status'],
          state: old['state'],
          current_question_id: old['current_question_id'],
        });
      }
    }
  });

  it("draws from the hour's eligible cards from the earliest one on, which are all of them", async () => {
    for (const seed of SEEDS) {
      const c = randomCell(seed);
      const db = new Db(c.storage.sql);
      for (const deck of c.srs) {
        const sid = await c.repos.sessions.create(deck, 'x');
        const r = lcg(seed + deck);
        for (const q of c.repos.questions.listInDeck(deck)) {
          if (r() < 0.3) db.run("INSERT OR IGNORE INTO study_session_answers (session_id, question_id, answered_at, result) VALUES (?, ?, 'x', 'right')", sid, q.id);
        }
        const eligible = `q.deck_id = ? AND COALESCE(q.suspended, 0) = 0
          AND NOT EXISTS (SELECT 1 FROM study_session_answers a WHERE a.session_id = ? AND a.question_id = q.id)`;
        const first = db.first<{ bucket: string; nd: string }>(
          `SELECT ${DUE_BUCKET} AS bucket, cards.next_due AS nd FROM cards CROSS JOIN questions q ON q.id = cards.question_id
            WHERE cards.next_due <= ? AND ${eligible} ORDER BY cards.next_due LIMIT 1`,
          NOW_ISO,
          deck,
          sid,
        );
        if (first === null) continue;
        const from = (lower: string) =>
          db
            .all<{ id: number }>(
              `SELECT q.id FROM cards CROSS JOIN questions q ON q.id = cards.question_id
                WHERE cards.next_due >= ? AND cards.next_due < ? || char(1114111) AND cards.next_due <= ?
                  AND ${DUE_BUCKET} = ? AND ${eligible} ORDER BY q.id`,
              lower,
              first.bucket,
              NOW_ISO,
              first.bucket,
              deck,
              sid,
            )
            .map((x) => Number(x.id));
        expect(from(first.nd)).toEqual(from(first.bucket));
      }
    }
  });
});
