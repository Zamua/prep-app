// `reviews` and the grade path, transcribed from prep/study/repo.py:
// ReviewRepo.record. The scheduler is the domain's; the retention
// resolution (deck, then profile, then the default) is the card repo's.
import type { CardRepo, Clock, ReviewRepo } from '../../../app/ports.js';
import { QuestionNotFound } from '../../../app/ports.js';
import type { CardState, ReviewResult, ReviewRow } from '../../../app/entities.js';
import { scheduleReview, type Fuzz, type FsrsStateValue } from '../../../domain/fsrs/index.js';
import { parseIso } from '../../../domain/time.js';
import { Db, type CellStorage } from './storage.js';
import { isoUtc } from './time.js';

export class SqlReviewRepo implements ReviewRepo {
  private readonly db: Db;

  constructor(
    private readonly storage: CellStorage,
    private readonly clock: Clock,
    private readonly cards: CardRepo,
    private readonly fuzz: Fuzz,
  ) {
    this.db = new Db(storage.sql);
  }

  record(qid: number, result: ReviewResult, userAnswer: string, notes = ''): CardState {
    if (result !== 'right' && result !== 'wrong') throw new RangeError(`unknown result: ${result}`);
    const now = this.clock.now();
    const ts = isoUtc(now);
    return this.storage.transactionSync(() => {
      const owner = this.db.first('SELECT id FROM questions WHERE id = ?', qid);
      if (!owner) throw new QuestionNotFound(`question ${qid} not owned by user`);
      const card = this.cards.srsState(qid);
      if (!card) throw new QuestionNotFound(`no card for question ${qid}`);
      const scheduled = scheduleReview(
        {
          stability: card.stability,
          difficulty: card.difficulty,
          fsrsState: (card.fsrs_state || 1) as FsrsStateValue,
          lastReview: card.last_review ? parseIso(card.last_review) : null,
        },
        result,
        now,
        { desiredRetention: this.cards.effectiveRetention(qid), fuzz: this.fuzz },
      );
      const intervalMinutes = Math.max(1, Math.floor(scheduled.intervalSeconds / 60));
      const nextDue = isoUtc(scheduled.nextDue);
      this.db.run('INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) VALUES (?, ?, ?, ?, ?)', qid, ts, result, userAnswer, notes);
      this.cards.writeScheduled(qid, scheduled, ts);
      return { step: scheduled.stepBucket, next_due: nextDue, interval_minutes: intervalMinutes };
    });
  }

  listReviewsForDeck(deckId: number): ReviewRow[] {
    return this.db
      .all(
        `SELECT q.prompt, r.ts, r.result, r.user_answer, r.grader_notes
           FROM reviews r JOIN questions q ON q.id = r.question_id
          WHERE q.deck_id = ?
          ORDER BY r.ts ASC, r.id ASC`,
        deckId,
      )
      .map((r) => ({
        prompt: String(r['prompt']),
        ts: String(r['ts']),
        result: String(r['result']),
        user_answer: (r['user_answer'] as string | null) ?? null,
        grader_notes: (r['grader_notes'] as string | null) ?? null,
      }));
  }

  importReview(qid: number, ts: string, result: ReviewResult, userAnswer = '', graderNotes: string | null = ''): void {
    if (result !== 'right' && result !== 'wrong') throw new RangeError(`unknown result: ${JSON.stringify(result)}`);
    this.db.run('INSERT INTO reviews (question_id, ts, result, user_answer, grader_notes) VALUES (?, ?, ?, ?, ?)', qid, ts, result, userAnswer, graderNotes);
  }

  getLastUserAnswer(qid: number): string | null {
    const row = this.db.first<{ user_answer: string | null }>('SELECT user_answer FROM reviews WHERE question_id = ? ORDER BY id DESC LIMIT 1', qid);
    return row ? row.user_answer : null;
  }
}
