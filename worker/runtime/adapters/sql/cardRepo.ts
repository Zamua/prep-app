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

export function rowToCard(r: Row): CardRow {
  return {
    question_id: Number(r['question_id']),
    step: Number(r['step'] ?? 0),
    next_due: String(r['next_due']),
    last_review: (r['last_review'] as string | null) ?? null,
    stability: r['stability'] == null ? null : Number(r['stability']),
    difficulty: r['difficulty'] == null ? null : Number(r['difficulty']),
    fsrs_state: Number(r['fsrs_state'] || 1),
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
    const row = this.db.first('SELECT question_id, step, next_due, last_review, stability, difficulty, fsrs_state FROM cards WHERE question_id = ?', qid);
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
      `UPDATE cards SET step = ?, next_due = ?, last_review = ?, stability = ?, difficulty = ?, fsrs_state = ? WHERE question_id = ?`,
      scheduled.stepBucket,
      isoUtc(scheduled.nextDue),
      reviewedAt,
      scheduled.state.stability,
      scheduled.state.difficulty,
      scheduled.state.fsrsState,
      qid,
    );
  }

  listCardStateForDeck(deckId: number): (CardRow & { prompt: string })[] {
    return this.db
      .all(
        `SELECT q.prompt, c.question_id, c.step, c.next_due, c.last_review, c.stability, c.difficulty, c.fsrs_state
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

  countDue(): number {
    const row = this.db.first<{ n: number }>(
      `SELECT COUNT(*) AS n
         FROM cards
         JOIN questions ON questions.id = cards.question_id
         JOIN decks ON decks.id = questions.deck_id
        WHERE COALESCE(questions.suspended, 0) = 0
          AND COALESCE(decks.notifications_enabled, 1) = 1
          AND COALESCE(decks.deck_type, 'srs') = 'srs'
          AND cards.next_due <= ?`,
      isoNow(this.clock),
    );
    return row ? Number(row.n) : 0;
  }

  nextDueMinutes(deckId: number | null = null): number | null {
    const now = this.clock.now();
    let sql =
      `SELECT MIN(cards.next_due) AS nd FROM cards JOIN questions q ON q.id = cards.question_id JOIN decks d ON d.id = q.deck_id
        WHERE COALESCE(q.suspended, 0) = 0 AND COALESCE(d.deck_type, 'srs') = 'srs' AND cards.next_due > ?`;
    const params: unknown[] = [isoUtc(now)];
    if (deckId !== null) {
      sql += ' AND q.deck_id = ?';
      params.push(deckId);
    }
    const row = this.db.first<{ nd: string | null }>(sql, ...params);
    const raw = row?.nd;
    if (!raw) return null;
    let due: Date;
    try {
      due = parseIso(raw);
    } catch {
      return null;
    }
    const seconds = (due.getTime() - now.getTime()) / 1000;
    return Math.max(1, Math.ceil(seconds / 60));
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
