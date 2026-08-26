// The three ledgers: grading (per workflow run), offline sync (per client
// id) and question inserts (per `<job>-insert-N` key).
import type { Clock, IdempotencyRepo } from '../../../app/ports.js';
import type { CardState, SyncOutcome } from '../../../app/entities.js';
import { Db, type CellStorage } from './storage.js';
import { isoNow } from './time.js';

export class SqlIdempotencyRepo implements IdempotencyRepo {
  private readonly db: Db;

  constructor(
    storage: CellStorage,
    private readonly clock: Clock,
  ) {
    this.db = new Db(storage.sql);
  }

  findGrading(key: string): CardState | null {
    const row = this.db.first('SELECT step, next_due, interval_minutes FROM grading_idempotency WHERE idempotency_key = ?', key);
    if (!row) return null;
    return { step: Number(row['step']), next_due: String(row['next_due']), interval_minutes: Number(row['interval_minutes']) };
  }

  recordGrading(key: string, questionId: number, state: CardState): void {
    this.db.run(
      'INSERT INTO grading_idempotency (idempotency_key, question_id, step, next_due, interval_minutes, created_at) VALUES (?, ?, ?, ?, ?, ?)',
      key,
      questionId,
      state.step,
      state.next_due,
      state.interval_minutes,
      isoNow(this.clock),
    );
  }

  findSync(clientId: string): SyncOutcome | null {
    const row = this.db.first('SELECT kind, status, question_id FROM offline_sync_idempotency WHERE client_id = ?', clientId);
    if (!row) return null;
    return { kind: String(row['kind']), status: String(row['status']), question_id: row['question_id'] == null ? null : Number(row['question_id']) };
  }

  recordSync(clientId: string, kind: 'card' | 'review', status: string, questionId: number | null): void {
    this.db.run(
      'INSERT INTO offline_sync_idempotency (client_id, kind, status, question_id, created_at) VALUES (?, ?, ?, ?, ?)',
      clientId,
      kind,
      status,
      questionId,
      isoNow(this.clock),
    );
  }

  findQuestion(key: string): number | null {
    const row = this.db.first<{ question_id: number }>('SELECT question_id FROM questions_idempotency WHERE idempotency_key = ?', key);
    return row ? Number(row.question_id) : null;
  }

  recordQuestion(key: string, questionId: number): void {
    this.db.run('INSERT INTO questions_idempotency (idempotency_key, question_id, created_at) VALUES (?, ?, ?)', key, questionId, isoNow(this.clock));
  }

  findStepResult(key: string): unknown | null {
    const row = this.db.first<{ result: string }>('SELECT result FROM steps_idempotency WHERE idempotency_key = ?', key);
    return row ? JSON.parse(String(row.result)) : null;
  }

  recordStepResult(key: string, result: unknown): void {
    this.db.run('INSERT OR IGNORE INTO steps_idempotency (idempotency_key, result, created_at) VALUES (?, ?, ?)', key, JSON.stringify(result), isoNow(this.clock));
  }
}
