// `study_sessions` and `study_session_answers`. Version-checked mutations raise
// `StaleVersionError`; the next card is the most overdue, ties within the
// hour broken by RANDOM().
import type { Clock, SessionIds, SessionRepo } from '../../../app/ports.js';
import { SessionNotFound, StaleVersionError } from '../../../app/ports.js';
import type { RecentSession, SessionState, SessionStatus, StudySession } from '../../../app/entities.js';
import { DUE_BUCKET } from './cardRepo.js';
import { Db, type CellStorage, type Row } from './storage.js';
import { DAY_MS, isoNow, isoUtc, shifted } from './time.js';

function decodeJson(v: unknown): Record<string, unknown> | null {
  if (typeof v !== 'string') return (v as Record<string, unknown> | null) ?? null;
  try {
    return JSON.parse(v) as Record<string, unknown>;
  } catch {
    return null;
  }
}

const asString = (v: unknown): string | null => (v == null ? null : String(v));
const num = (v: unknown): number | null => (v == null ? null : Number(v));

function rowToSession(r: Row): StudySession {
  return {
    id: String(r['id']),
    deck_id: Number(r['deck_id']),
    created_at: String(r['created_at']),
    last_active: String(r['last_active']),
    status: ((r['status'] as string | null) || 'active') as SessionStatus,
    state: ((r['state'] as string | null) || 'awaiting-answer') as SessionState,
    current_question_id: num(r['current_question_id']),
    current_draft: asString(r['current_draft']),
    current_grading_workflow_id: asString(r['current_grading_workflow_id']),
    last_answered_qid: num(r['last_answered_qid']),
    last_answered_verdict: decodeJson(r['last_answered_verdict']),
    last_answered_state: decodeJson(r['last_answered_state']),
    version: Number(r['version'] || 1),
    device_label: asString(r['device_label']),
  };
}

function rowToRecent(r: Row): RecentSession {
  return {
    id: String(r['id']),
    deck_id: Number(r['deck_id']),
    deck_name: String(r['deck_name']),
    deck_display_name: asString(r['deck_display_name']),
    last_active: String(r['last_active']),
    status: ((r['status'] as string | null) || 'active') as SessionStatus,
    state: ((r['state'] as string | null) || 'awaiting-answer') as SessionState,
    device_label: asString(r['device_label']),
    current_question_id: num(r['current_question_id']),
    current_prompt: asString(r['current_prompt']),
    current_type: asString(r['current_type']),
    snoozed_until: asString(r['snoozed_until']),
  };
}

const RECENT_SELECT = `SELECT s.*, d.name AS deck_name, d.display_name AS deck_display_name,
       q.prompt AS current_prompt, q.type AS current_type
  FROM study_sessions s
  JOIN decks d ON d.id = s.deck_id
  LEFT JOIN questions q ON q.id = s.current_question_id`;

export class SqlSessionRepo implements SessionRepo {
  private readonly db: Db;

  constructor(
    private readonly storage: CellStorage,
    private readonly clock: Clock,
    private readonly ids: SessionIds,
  ) {
    this.db = new Db(storage.sql);
  }

  async create(deckId: number, deviceLabel: string): Promise<string> {
    const ts = isoNow(this.clock);
    const sid = await this.ids.next();
    const next = this.pickNextQuestion(deckId, sid);
    this.db.run(
      `INSERT INTO study_sessions
         (id, deck_id, created_at, last_active, status, state, current_question_id, current_draft, version, device_label)
       VALUES (?, ?, ?, ?, 'active', 'awaiting-answer', ?, ?, 1, ?)`,
      sid,
      deckId,
      ts,
      ts,
      next ? next.id : null,
      next ? next.skeleton || '' : '',
      deviceLabel,
    );
    return sid;
  }

  get(sid: string): StudySession | null {
    const row = this.db.first('SELECT * FROM study_sessions WHERE id = ?', sid);
    return row ? rowToSession(row) : null;
  }

  findActiveForDeck(deckId: number): StudySession | null {
    const row = this.db.first(`SELECT * FROM study_sessions WHERE deck_id = ? AND status = 'active' ORDER BY last_active DESC LIMIT 1`, deckId);
    return row ? rowToSession(row) : null;
  }

  listRecent(limit = 5): RecentSession[] {
    const now = this.clock.now();
    const abandonBefore = isoUtc(shifted(now, -7 * DAY_MS));
    this.db.run(`UPDATE study_sessions SET status = 'abandoned' WHERE status = 'active' AND last_active < ?`, abandonBefore);
    return this.db
      .all(
        `${RECENT_SELECT}
          WHERE s.status = 'active' AND (s.snoozed_until IS NULL OR s.snoozed_until <= ?)
          ORDER BY s.last_active DESC
          LIMIT ?`,
        isoUtc(now),
        limit,
      )
      .map(rowToRecent);
  }

  snooze(sid: string, untilIso: string | null): void {
    this.db.run('UPDATE study_sessions SET snoozed_until = ? WHERE id = ?', untilIso, sid);
  }

  listSnoozed(): RecentSession[] {
    return this.db
      .all(
        `${RECENT_SELECT}
          WHERE s.status = 'active' AND s.snoozed_until IS NOT NULL AND s.snoozed_until > ?
          ORDER BY s.snoozed_until ASC`,
        isoNow(this.clock),
      )
      .map(rowToRecent);
  }

  private checkedVersion(sid: string, expected: number): { deck_id: number } {
    const row = this.db.first<{ version: number; deck_id: number }>('SELECT version, deck_id FROM study_sessions WHERE id = ?', sid);
    if (!row) throw new SessionNotFound(`session ${sid} not found for user`);
    if (Number(row.version) !== expected) throw new StaleVersionError(Number(row.version));
    return { deck_id: Number(row.deck_id) };
  }

  updateDraft(sid: string, draft: string, expectedVersion: number): number {
    const ts = isoNow(this.clock);
    return this.storage.transactionSync(() => {
      this.checkedVersion(sid, expectedVersion);
      const v = expectedVersion + 1;
      this.db.run('UPDATE study_sessions SET current_draft = ?, last_active = ?, version = ? WHERE id = ?', draft, ts, v, sid);
      return v;
    });
  }

  recordAnswerSync(
    sid: string,
    questionId: number,
    expectedVersion: number,
    _userAnswer: string,
    verdict: Record<string, unknown>,
    state: Record<string, unknown>,
  ): number {
    const ts = isoNow(this.clock);
    return this.storage.transactionSync(() => {
      this.checkedVersion(sid, expectedVersion);
      const v = expectedVersion + 1;
      this.db.run(
        `INSERT OR REPLACE INTO study_session_answers (session_id, question_id, answered_at, result, workflow_id) VALUES (?, ?, ?, ?, NULL)`,
        sid,
        questionId,
        ts,
        String(verdict['result']),
      );
      this.db.run(
        `UPDATE study_sessions SET state = 'showing-result', current_draft = NULL, last_answered_qid = ?,
                last_answered_verdict = ?, last_answered_state = ?, last_active = ?, version = ?
          WHERE id = ?`,
        questionId,
        JSON.stringify(verdict),
        JSON.stringify(state),
        ts,
        v,
        sid,
      );
      return v;
    });
  }

  setGrading(sid: string, _questionId: number, workflowId: string, expectedVersion: number): number {
    const ts = isoNow(this.clock);
    return this.storage.transactionSync(() => {
      this.checkedVersion(sid, expectedVersion);
      const v = expectedVersion + 1;
      this.db.run(
        `UPDATE study_sessions SET state = 'grading', current_grading_workflow_id = ?, last_active = ?, version = ? WHERE id = ?`,
        workflowId,
        ts,
        v,
        sid,
      );
      return v;
    });
  }

  gradingCompleted(sid: string, questionId: number, verdict: Record<string, unknown>, state: Record<string, unknown>, workflowId: string): void {
    const ts = isoNow(this.clock);
    this.storage.transactionSync(() => {
      const row = this.db.first<{ state: string }>('SELECT state, version FROM study_sessions WHERE id = ?', sid);
      if (!row || row.state !== 'grading') return;
      this.db.run(
        `INSERT OR REPLACE INTO study_session_answers (session_id, question_id, answered_at, result, workflow_id) VALUES (?, ?, ?, ?, ?)`,
        sid,
        questionId,
        ts,
        String(verdict['result']),
        workflowId,
      );
      this.db.run(
        `UPDATE study_sessions SET state = 'showing-result', current_grading_workflow_id = NULL, current_draft = NULL,
                last_answered_qid = ?, last_answered_verdict = ?, last_answered_state = ?, last_active = ?, version = version + 1
          WHERE id = ?`,
        questionId,
        JSON.stringify(verdict),
        JSON.stringify(state),
        ts,
        sid,
      );
    });
  }

  gradingAbandoned(sid: string, workflowId: string): void {
    this.db.run(
      `UPDATE study_sessions SET state = 'awaiting-answer', current_grading_workflow_id = NULL, last_active = ?, version = version + 1
        WHERE id = ? AND state = 'grading' AND current_grading_workflow_id = ?`,
      isoNow(this.clock),
      sid,
      workflowId,
    );
  }

  advance(sid: string, expectedVersion: number): number {
    const ts = isoNow(this.clock);
    return this.storage.transactionSync(() => {
      const { deck_id } = this.checkedVersion(sid, expectedVersion);
      const next = this.pickNextQuestion(deck_id, sid);
      const v = expectedVersion + 1;
      if (next === null) {
        this.db.run(
          `UPDATE study_sessions SET status = 'completed', state = 'awaiting-answer', current_question_id = NULL, current_draft = NULL,
                  last_answered_qid = NULL, last_answered_verdict = NULL, last_answered_state = NULL, last_active = ?, version = ?
            WHERE id = ?`,
          ts,
          v,
          sid,
        );
      } else {
        this.db.run(
          `UPDATE study_sessions SET state = 'awaiting-answer', current_question_id = ?, current_draft = ?,
                  last_answered_qid = NULL, last_answered_verdict = NULL, last_answered_state = NULL, last_active = ?, version = ?
            WHERE id = ?`,
          next.id,
          next.skeleton || '',
          ts,
          v,
          sid,
        );
      }
      return v;
    });
  }

  markCompleted(sid: string): void {
    this.db.run(`UPDATE study_sessions SET status='completed', version = version + 1, last_active = ? WHERE id = ?`, isoNow(this.clock), sid);
  }

  abandon(sid: string): void {
    this.db.run(`UPDATE study_sessions SET status = 'abandoned', last_active = ?, version = version + 1 WHERE id = ?`, isoNow(this.clock), sid);
  }

  abandonAllForDeck(deckId: number): number {
    return this.db.run(
      `UPDATE study_sessions SET status = 'abandoned', last_active = ?, version = version + 1 WHERE deck_id = ? AND status = 'active'`,
      isoNow(this.clock),
      deckId,
    );
  }

  /** A due card the session has not answered: most overdue first, ties within the hour at random. */
  private pickNextQuestion(deckId: number, sid: string): { id: number; skeleton: string | null } | null {
    const row = this.db.first<{ id: number; skeleton: string | null }>(
      `SELECT q.id, q.skeleton FROM questions q JOIN cards ON cards.question_id = q.id
        WHERE q.deck_id = ? AND COALESCE(q.suspended, 0) = 0 AND cards.next_due <= ?
          AND q.id NOT IN (SELECT question_id FROM study_session_answers WHERE session_id = ?)
        ORDER BY ${DUE_BUCKET} ASC, RANDOM()
        LIMIT 1`,
      deckId,
      isoNow(this.clock),
      sid,
    );
    return row ? { id: Number(row.id), skeleton: row.skeleton } : null;
  }
}
