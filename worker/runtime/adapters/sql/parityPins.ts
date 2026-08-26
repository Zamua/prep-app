// Column pins the parity seed writes past the repositories, as
// prep/dev/parity_seed.py does: timestamps the profiles fix relative to the
// clock.
import type { ParityPins } from '../../../app/ports.js';
import { Db, type CellStorage } from './storage.js';

export class SqlParityPins implements ParityPins {
  private readonly db: Db;

  constructor(storage: CellStorage) {
    this.db = new Db(storage.sql);
  }

  session(sid: string, lastActive: string, createdAt: string | null = null): void {
    this.db.run('UPDATE study_sessions SET last_active = ?, created_at = ? WHERE id = ?', lastActive, createdAt ?? lastActive, sid);
  }

  answerInSession(sid: string, qid: number, answeredAt: string, result: string): void {
    this.db.run('INSERT INTO study_session_answers (session_id, question_id, answered_at, result) VALUES (?, ?, ?, ?)', sid, qid, answeredAt, result);
  }

  pinnedAt(deckId: number, pinnedAt: string): void {
    this.db.run('UPDATE decks SET pinned_at = ? WHERE id = ?', pinnedAt, deckId);
  }

  notificationSentAt(noteId: number, sentAt: string): void {
    this.db.run('UPDATE notifications_log SET sent_at = ? WHERE id = ?', sentAt, noteId);
  }

  workflowStartedAt(workflowId: string, startedAt: string): void {
    this.db.run('UPDATE active_workflows SET started_at = ? WHERE workflow_id = ?', startedAt, workflowId);
  }

  tokenCreatedAt(tokenId: number, createdAt: string): void {
    this.db.run('UPDATE api_tokens SET created_at = ? WHERE id = ?', createdAt, tokenId);
  }
}
