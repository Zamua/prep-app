// `trivia_queue` and `trivia_sessions`, transcribed from prep/trivia/repo.py.
// Selection ranks by answer state; the shown order is shuffled once and the
// session persists it.
import type { Clock, Random, SessionIds, TriviaRepo } from '../../../app/ports.js';
import type { ActiveTriviaSession, DoneItem, NextCard, TriviaDeckStats, TriviaQueueEntry, TriviaSession } from '../../../app/entities.js';
import { formatDone, parseCardIds, parseDone } from '../../../domain/trivia.js';
import { Db, type CellStorage, type Row } from './storage.js';
import { DAY_MS, isoSeconds, shifted } from './time.js';

const ACTIVE_TIMEOUT_MS = 7 * DAY_MS;

function rowToNext(r: Row): NextCard {
  return { question_id: Number(r['question_id']), deck_id: Number(r['deck_id']), prompt: String(r['prompt']), is_fresh: Boolean(r['is_fresh']) };
}

function rowToTriviaSession(r: Row): TriviaSession {
  return {
    id: String(r['id']),
    deck_id: Number(r['deck_id']),
    started_at: String(r['started_at']),
    last_active: String(r['last_active']),
    status: String(r['status']),
    queue: parseCardIds(r['queue'] as string),
    done: parseDone(r['done'] as string) as DoneItem[],
  };
}

function rowToActive(r: Row): ActiveTriviaSession {
  return {
    deck_name: String(r['deck_name']),
    deck_display_name: (r['deck_display_name'] as string | null) ?? null,
    deck_id: Number(r['deck_id']),
    last_active: String(r['last_active']),
    queue: parseCardIds(r['queue'] as string),
    done: parseDone(r['done'] as string) as DoneItem[],
    snoozed_until: (r['snoozed_until'] as string | null) ?? null,
  };
}

/** `random.shuffle`: Fisher-Yates drawing `choice` over the index range. */
export function shuffle<T>(items: T[], random: Random): T[] {
  const out = [...items];
  for (let i = out.length - 1; i > 0; i--) {
    const j = random.choice(Array.from({ length: i + 1 }, (_, k) => k));
    [out[i], out[j]] = [out[j]!, out[i]!];
  }
  return out;
}

export class SqlTriviaRepo implements TriviaRepo {
  private readonly db: Db;

  constructor(
    private readonly storage: CellStorage,
    private readonly clock: Clock,
    private readonly ids: SessionIds,
    private readonly random: Random,
  ) {
    this.db = new Db(storage.sql);
  }

  private nowIso(): string {
    return isoSeconds(this.clock.now());
  }

  // ---- queue -------------------------------------------------------------

  appendCard(questionId: number, deckId: number): TriviaQueueEntry {
    return this.storage.transactionSync(() => {
      const row = this.db.first<{ m: number }>(
        'SELECT COALESCE(MAX(tq.queue_position), 0) AS m FROM trivia_queue tq JOIN questions q ON q.id = tq.question_id WHERE q.deck_id = ?',
        deckId,
      );
      const next = Number(row?.m ?? 0) + 1;
      this.db.run('INSERT INTO trivia_queue (question_id, queue_position) VALUES (?, ?)', questionId, next);
      return { question_id: questionId, queue_position: next, last_answered_at: null, last_answered_correctly: null };
    });
  }

  pickNextForDeck(deckId: number): NextCard | null {
    const row = this.db.first(
      `SELECT q.id AS question_id, q.deck_id, q.prompt, (tq.last_answered_at IS NULL) AS is_fresh
         FROM questions q JOIN trivia_queue tq ON tq.question_id = q.id
        WHERE q.deck_id = ?
        ORDER BY CASE WHEN tq.last_answered_correctly = 0 THEN 0 WHEN tq.last_answered_at IS NULL THEN 1 ELSE 2 END ASC,
                 substr(COALESCE(tq.last_answered_at, ''), 1, 13) ASC, RANDOM()
        LIMIT 1`,
      deckId,
    );
    return row ? rowToNext(row) : null;
  }

  listQueueForDeck(deckId: number): (TriviaQueueEntry & { prompt: string })[] {
    return this.db
      .all(
        `SELECT q.prompt, tq.question_id, tq.queue_position, tq.last_answered_at, tq.last_answered_correctly
           FROM trivia_queue tq JOIN questions q ON q.id = tq.question_id
          WHERE q.deck_id = ? ORDER BY tq.queue_position ASC`,
        deckId,
      )
      .map((r) => ({
        prompt: String(r['prompt']),
        question_id: Number(r['question_id']),
        queue_position: Number(r['queue_position']),
        last_answered_at: (r['last_answered_at'] as string | null) ?? null,
        last_answered_correctly: r['last_answered_correctly'] == null ? null : Boolean(r['last_answered_correctly']),
      }));
  }

  importEntry(questionId: number, queuePosition: number, opts: { lastAnsweredAt?: string | null; lastAnsweredCorrectly?: number | null } = {}): void {
    this.db.run(
      'INSERT INTO trivia_queue (question_id, queue_position, last_answered_at, last_answered_correctly) VALUES (?, ?, ?, ?)',
      questionId,
      queuePosition,
      opts.lastAnsweredAt ?? null,
      opts.lastAnsweredCorrectly ?? null,
    );
  }

  private deckIdFor(questionId: number): number | null {
    const row = this.db.first<{ deck_id: number }>('SELECT deck_id FROM questions WHERE id = ?', questionId);
    return row ? Number(row.deck_id) : null;
  }

  markAnswered(questionId: number, correct: boolean): void {
    this.storage.transactionSync(() => {
      const deckId = this.deckIdFor(questionId);
      if (deckId === null) return;
      const np = this.db.first<{ np: number }>(
        'SELECT COALESCE(MAX(tq.queue_position), 0) + 1 AS np FROM trivia_queue tq JOIN questions q ON q.id = tq.question_id WHERE q.deck_id = ?',
        deckId,
      );
      this.db.run(
        'UPDATE trivia_queue SET last_answered_at = ?, last_answered_correctly = ?, queue_position = ? WHERE question_id = ?',
        this.nowIso(),
        correct ? 1 : 0,
        Number(np?.np ?? 1),
        questionId,
      );
      this.db.run('UPDATE decks SET notification_ignored_streak = 0 WHERE id = ?', deckId);
    });
  }

  setLastCorrectness(questionId: number, correct: boolean): void {
    this.storage.transactionSync(() => {
      const deckId = this.deckIdFor(questionId);
      if (deckId === null) return;
      this.db.run('UPDATE trivia_queue SET last_answered_correctly = ? WHERE question_id = ?', correct ? 1 : 0, questionId);
      this.db.run('UPDATE decks SET notification_ignored_streak = 0 WHERE id = ?', deckId);
    });
  }

  countUnanswered(deckId: number): number {
    const row = this.db.first<{ n: number }>(
      'SELECT COUNT(*) AS n FROM trivia_queue tq JOIN questions q ON q.id = tq.question_id WHERE q.deck_id = ? AND tq.last_answered_at IS NULL',
      deckId,
    );
    return Number(row?.n ?? 0);
  }

  deckStats(deckId: number): TriviaDeckStats {
    const row = this.db.first(
      `SELECT COUNT(*) AS total,
              SUM(CASE WHEN tq.last_answered_at IS NULL THEN 1 ELSE 0 END) AS unanswered,
              SUM(CASE WHEN tq.last_answered_correctly = 0 THEN 1 ELSE 0 END) AS wrong,
              SUM(CASE WHEN tq.last_answered_correctly = 1 THEN 1 ELSE 0 END) AS mastered
         FROM trivia_queue tq JOIN questions q ON q.id = tq.question_id
        WHERE q.deck_id = ?`,
      deckId,
    );
    return {
      total: Number(row?.['total'] ?? 0),
      unanswered: Number(row?.['unanswered'] ?? 0),
      wrong: Number(row?.['wrong'] ?? 0),
      mastered: Number(row?.['mastered'] ?? 0),
    };
  }

  hasAnswerSince(deckId: number, ts: string | null): boolean {
    if (ts === null) return false;
    const row = this.db.first(
      `SELECT 1 AS one FROM trivia_queue tq JOIN questions q ON q.id = tq.question_id
        WHERE q.deck_id = ? AND tq.last_answered_at IS NOT NULL AND tq.last_answered_at > ? LIMIT 1`,
      deckId,
      ts,
    );
    return row !== null;
  }

  countPendingReview(deckId: number): number {
    const row = this.db.first<{ n: number }>(
      `SELECT COUNT(*) AS n FROM trivia_queue tq JOIN questions q ON q.id = tq.question_id
        WHERE q.deck_id = ? AND (tq.last_answered_at IS NULL OR tq.last_answered_correctly = 0)`,
      deckId,
    );
    return Number(row?.n ?? 0);
  }

  pickSessionForDeck(deckId: number, opts: { targetSize?: number; freshTarget?: number } = {}): NextCard[] {
    const targetSize = opts.targetSize ?? 3;
    const freshTarget = opts.freshTarget ?? 1;
    const reviewSlots = targetSize - freshTarget;
    const review =
      reviewSlots > 0
        ? this.db.all(
            `SELECT q.id AS question_id, q.deck_id, q.prompt, 0 AS is_fresh
               FROM questions q JOIN trivia_queue tq ON tq.question_id = q.id
              WHERE q.deck_id = ? AND tq.last_answered_at IS NOT NULL
              ORDER BY CASE WHEN tq.last_answered_correctly = 0 THEN 0 ELSE 1 END, substr(tq.last_answered_at, 1, 13) ASC, RANDOM()
              LIMIT ?`,
            deckId,
            reviewSlots,
          )
        : [];
    const fresh =
      freshTarget > 0
        ? this.db.all(
            `SELECT q.id AS question_id, q.deck_id, q.prompt, 1 AS is_fresh
               FROM questions q JOIN trivia_queue tq ON tq.question_id = q.id
              WHERE q.deck_id = ? AND tq.last_answered_at IS NULL
              ORDER BY RANDOM() LIMIT ?`,
            deckId,
            freshTarget,
          )
        : [];
    const picked = new Set([...review, ...fresh].map((r) => Number(r['question_id'])));
    const short = targetSize - picked.size;
    let backfill: Row[] = [];
    if (short > 0) {
      const ids = [...picked];
      const marks = ids.length ? ids.map(() => '?').join(',') : 'NULL';
      backfill = this.db.all(
        `SELECT q.id AS question_id, q.deck_id, q.prompt, (tq.last_answered_at IS NULL) AS is_fresh
           FROM questions q JOIN trivia_queue tq ON tq.question_id = q.id
          WHERE q.deck_id = ? AND q.id NOT IN (${marks})
          ORDER BY CASE WHEN tq.last_answered_correctly = 0 THEN 0 WHEN tq.last_answered_at IS NULL THEN 1 ELSE 2 END,
                   substr(COALESCE(tq.last_answered_at, ''), 1, 13) ASC, RANDOM()
          LIMIT ?`,
        deckId,
        ...ids,
        short,
      );
    }
    const selected = [...review, ...fresh, ...backfill].slice(0, targetSize);
    return shuffle(selected, this.random).map(rowToNext);
  }

  promptForQuestion(questionId: number): string | null {
    const row = this.db.first<{ prompt: string }>('SELECT prompt FROM questions WHERE id = ?', questionId);
    return row ? row.prompt : null;
  }

  existingPrompts(deckId: number): string[] {
    return this.db.all<{ prompt: string }>('SELECT prompt FROM questions WHERE deck_id = ? ORDER BY id', deckId).map((r) => r.prompt);
  }

  // ---- sessions ------------------------------------------------------------

  getActiveSessionForDeck(deckId: number): TriviaSession | null {
    const row = this.db.first(`SELECT * FROM trivia_sessions WHERE deck_id = ? AND status = 'active' ORDER BY last_active DESC LIMIT 1`, deckId);
    return row ? rowToTriviaSession(row) : null;
  }

  listActiveSessions(): ActiveTriviaSession[] {
    const now = this.clock.now();
    const threshold = isoSeconds(shifted(now, -ACTIVE_TIMEOUT_MS));
    this.db.run(`UPDATE trivia_sessions SET status = 'abandoned' WHERE status = 'active' AND last_active < ?`, threshold);
    return this.db
      .all(
        `SELECT s.deck_id, s.last_active, s.queue, s.done, NULL AS snoozed_until, d.name AS deck_name, d.display_name AS deck_display_name
           FROM trivia_sessions s JOIN decks d ON d.id = s.deck_id
          WHERE s.status = 'active' AND (s.snoozed_until IS NULL OR s.snoozed_until <= ?)
          ORDER BY s.last_active DESC`,
        isoSeconds(now),
      )
      .map(rowToActive);
  }

  snoozeActiveForDeck(deckId: number, untilIso: string | null): number {
    return this.db.run(`UPDATE trivia_sessions SET snoozed_until = ? WHERE deck_id = ? AND status = 'active'`, untilIso, deckId);
  }

  listSnoozedSessions(): ActiveTriviaSession[] {
    return this.db
      .all(
        `SELECT s.deck_id, s.last_active, s.queue, s.done, s.snoozed_until, d.name AS deck_name, d.display_name AS deck_display_name
           FROM trivia_sessions s JOIN decks d ON d.id = s.deck_id
          WHERE s.status = 'active' AND s.snoozed_until IS NOT NULL AND s.snoozed_until > ?
          ORDER BY s.snoozed_until ASC`,
        this.nowIso(),
      )
      .map(rowToActive);
  }

  async startOrResume(deckId: number, state: { queue: readonly number[]; done: readonly DoneItem[] }): Promise<TriviaSession> {
    const existing = this.getActiveSessionForDeck(deckId);
    const now = this.nowIso();
    if (existing) {
      this.db.run('UPDATE trivia_sessions SET last_active = ? WHERE id = ?', now, existing.id);
      return { ...existing, last_active: now };
    }
    const sid = await this.ids.next();
    this.db.run(
      `INSERT INTO trivia_sessions (id, deck_id, started_at, last_active, status, queue, done) VALUES (?, ?, ?, ?, 'active', ?, ?)`,
      sid,
      deckId,
      now,
      now,
      state.queue.join(','),
      formatDone(state.done as readonly [number, 'r' | 'w'][]),
    );
    return { id: sid, deck_id: deckId, started_at: now, last_active: now, status: 'active', queue: [...state.queue], done: [...state.done] };
  }

  async replaceActive(deckId: number, state: { queue: readonly number[] }): Promise<TriviaSession> {
    this.db.run(`UPDATE trivia_sessions SET status = 'abandoned' WHERE deck_id = ? AND status = 'active'`, deckId);
    return this.startOrResume(deckId, { queue: state.queue, done: [] });
  }

  persistState(deckId: number, state: { queue: readonly number[]; done: readonly DoneItem[] }): void {
    this.db.run(
      `UPDATE trivia_sessions SET queue = ?, done = ?, last_active = ? WHERE deck_id = ? AND status = 'active'`,
      state.queue.join(','),
      formatDone(state.done as readonly [number, 'r' | 'w'][]),
      this.nowIso(),
      deckId,
    );
  }

  completeSession(deckId: number): void {
    this.db.run(`UPDATE trivia_sessions SET status = 'completed', last_active = ? WHERE deck_id = ? AND status = 'active'`, this.nowIso(), deckId);
  }

  abandonAllSessionsForDeck(deckId: number): number {
    return this.db.run(`UPDATE trivia_sessions SET status = 'abandoned', last_active = ? WHERE deck_id = ? AND status = 'active'`, this.nowIso(), deckId);
  }
}
