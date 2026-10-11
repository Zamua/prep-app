// The SRS state on `cards`, and the due-queue reads.
import type { CardRepo, Clock } from '../../../app/ports.js';
import type { CardRow, Question } from '../../../app/entities.js';
import type { ScheduledReview } from '../../../domain/fsrs/index.js';
import { parseIso } from '../../../domain/time.js';
import { rowToQuestion } from './questionRepo.js';
import { Db, type CellStorage, type Row } from './storage.js';
import { isoNow, isoUtc } from './time.js';

// The hour bucket of an ISO timestamp: cards due within the same hour tie
// and RANDOM() shuffles them.
export const DUE_BUCKET = 'substr(cards.next_due, 1, 13)';

// A negative LIMIT is no limit. The cards loop leads so the due index bounds
// the walk, which a cap then stops early.
export const COUNT_DUE = `SELECT COUNT(*) AS n FROM (
         SELECT 1
           FROM cards
           CROSS JOIN questions ON questions.id = cards.question_id
           JOIN decks ON decks.id = questions.deck_id
          WHERE COALESCE(questions.suspended, 0) = 0
            AND COALESCE(decks.notifications_enabled, 1) = 1
            AND COALESCE(decks.deck_type, 'srs') = 'srs'
            AND cards.next_due <= ?
          LIMIT ?)`;

export const NEXT_DUE = `SELECT cards.next_due AS nd FROM cards CROSS JOIN questions q ON q.id = cards.question_id JOIN decks d ON d.id = q.deck_id
        WHERE COALESCE(q.suspended, 0) = 0 AND COALESCE(d.deck_type, 'srs') = 'srs' AND cards.next_due > ?`;

/** A review's write: `next_due` is the second binding. */
export const WRITE_SCHEDULED = `UPDATE cards SET step = ?, next_due = ?, last_review = ?, stability = ?, difficulty = ?, fsrs_state = ?, learning_steps = ? WHERE question_id = ?`;

/** Whole minutes from `now` to a stored due stamp, at least one; null for none. */
export function minutesUntil(raw: unknown, now: Date): number | null {
  if (!raw) return null;
  let due: Date;
  try {
    due = parseIso(raw as string);
  } catch {
    return null;
  }
  const seconds = (due.getTime() - now.getTime()) / 1000;
  return Math.max(1, Math.ceil(seconds / 60));
}

export function rowToCard(r: Row): CardRow {
  return {
    question_id: Number(r['question_id']),
    step: Number(r['step'] ?? 0),
    next_due: String(r['next_due']),
    last_review: (r['last_review'] as string | null) ?? null,
    stability: r['stability'] == null ? null : Number(r['stability']),
    difficulty: r['difficulty'] == null ? null : Number(r['difficulty']),
    fsrs_state: Number(r['fsrs_state'] || 1),
    learning_steps: Number(r['learning_steps'] ?? 0),
  };
}

export class SqlCardRepo implements CardRepo {
  private readonly db: Db;

  constructor(
    storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  srsState(qid: number): CardRow | null {
    const row = this.db.first('SELECT question_id, step, next_due, last_review, stability, difficulty, fsrs_state, learning_steps FROM cards WHERE question_id = ?', qid);
    return row ? rowToCard(row) : null;
  }

  effectiveRetention(qid: number): number | null {
    const row = this.db.first(
      `SELECT d.desired_retention AS deck_ret, (SELECT desired_retention FROM profile LIMIT 1) AS user_ret
         FROM questions q JOIN decks d ON d.id = q.deck_id
        WHERE q.id = ?`,
      qid,
    );
    if (!row) return null;
    const v = row['deck_ret'] ?? row['user_ret'];
    return v == null ? null : Number(v);
  }

  writeScheduled(qid: number, scheduled: ScheduledReview, reviewedAt: string): void {
    this.db.run(
      WRITE_SCHEDULED,
      scheduled.stepBucket,
      isoUtc(scheduled.nextDue),
      reviewedAt,
      scheduled.state.stability,
      scheduled.state.difficulty,
      scheduled.state.fsrsState,
      scheduled.state.learningSteps,
      qid,
    );
  }

  listCardStateForDeck(deckId: number): (CardRow & { prompt: string })[] {
    return this.db
      .all(
        `SELECT q.prompt, c.question_id, c.step, c.next_due, c.last_review, c.stability, c.difficulty, c.fsrs_state, c.learning_steps
           FROM cards c JOIN questions q ON q.id = c.question_id
          WHERE q.deck_id = ?`,
        deckId,
      )
      .map((r) => ({ ...rowToCard(r), prompt: String(r['prompt']) }));
  }

  restoreCardState(qid: number, fields: Partial<Omit<CardRow, 'question_id'>>): void {
    const entries = Object.entries(fields).filter(([, v]) => v !== null && v !== undefined);
    if (entries.length === 0) return;
    const sets = entries.map(([k]) => `${k} = ?`).join(', ');
    this.db.run(`UPDATE cards SET ${sets} WHERE question_id = ?`, ...entries.map(([, v]) => v), qid);
  }

  countDue(cap?: number): number {
    return this.countDueAt(isoNow(this.clock), cap);
  }

  countDueAt(at: string, cap?: number): number {
    const row = this.db.first<{ n: number }>(COUNT_DUE, at, cap ?? -1);
    return row ? Number(row.n) : 0;
  }

  nextDueMinutes(deckId: number | null = null): number | null {
    const now = this.clock.now();
    return minutesUntil(this.nextDueAfter(isoUtc(now), deckId), now);
  }

  /** The earliest `next_due` after `at` among unsuspended SRS cards, as stored. */
  nextDueAfter(at: string, deckId: number | null): unknown {
    // The earliest in due-index order rather than MIN: the walk stops at the
    // first eligible card whatever the planner makes of the join.
    let sql = NEXT_DUE;
    const params: unknown[] = [at];
    if (deckId !== null) {
      sql += ' AND q.deck_id = ?';
      params.push(deckId);
    }
    return this.db.first(`${sql} ORDER BY cards.next_due LIMIT 1`, ...params)?.['nd'] ?? null;
  }

  dueQuestions(deckId: number, limit = 3): Question[] {
    const ids = this.db.all<{ id: number }>(
      `SELECT q.id FROM questions q JOIN cards ON cards.question_id = q.id
        WHERE q.deck_id = ? AND COALESCE(q.suspended, 0) = 0 AND cards.next_due <= ?
        ORDER BY ${DUE_BUCKET} ASC, RANDOM()
        LIMIT ?`,
      deckId,
      isoNow(this.clock),
      limit,
    );
    const out: Question[] = [];
    for (const { id } of ids) {
      const row = this.db.first('SELECT q.*, cards.step, cards.next_due FROM questions q LEFT JOIN cards ON cards.question_id = q.id WHERE q.id = ?', id);
      if (row) out.push(rowToQuestion(row));
    }
    return out;
  }
}
