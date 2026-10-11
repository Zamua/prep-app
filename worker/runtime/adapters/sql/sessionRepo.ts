// `study_sessions` and `study_session_answers`. Version-checked mutations raise
// `StaleVersionError`; the next card is the most overdue, ties within the
// hour broken by RANDOM().
import type { Clock, SessionIds, SessionRepo } from '../../../app/ports.js';
import { SessionNotFound, StaleVersionError } from '../../../app/ports.js';
import type { RecentSession, SessionState, SessionStatus, StudySession } from '../../../app/entities.js';
import { DUE_BUCKET } from './cardRepo.js';
import { Db, type CellStorage, type Row, type SqlValue } from './storage.js';
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

const ELIGIBLE = `q.deck_id = ? AND COALESCE(q.suspended, 0) = 0
          AND NOT EXISTS (SELECT 1 FROM study_session_answers a WHERE a.session_id = ? AND a.question_id = q.id)`;

const firstDue = (from: boolean): string =>
  `SELECT ${DUE_BUCKET} AS bucket, cards.next_due AS nd FROM cards CROSS JOIN questions q ON q.id = cards.question_id
        WHERE ${from ? 'cards.next_due >= ? AND ' : ''}cards.next_due <= ? AND ${ELIGIBLE}
        ORDER BY cards.next_due
        LIMIT 1`;
const FIRST_DUE = firstDue(false);
const FIRST_DUE_FROM = firstDue(true);

const DRAW = `SELECT q.id, q.skeleton FROM cards CROSS JOIN questions q ON q.id = cards.question_id
        WHERE cards.next_due >= ? AND cards.next_due < ? || char(1114111) AND cards.next_due <= ?
          AND ${DUE_BUCKET} = ? AND ${ELIGIBLE}
        ORDER BY RANDOM()
        LIMIT 1`;

/** What the pick reads, for a floor to know which writes concern it. */
export const PICK_STATEMENTS: readonly string[] = [FIRST_DUE, DRAW];

export const RECORD_ANSWER = `INSERT OR REPLACE INTO study_session_answers (session_id, question_id, answered_at, result, workflow_id) VALUES (?, ?, ?, ?, NULL)`;
export const RECORD_GRADED_ANSWER = `INSERT OR REPLACE INTO study_session_answers (session_id, question_id, answered_at, result, workflow_id) VALUES (?, ?, ?, ?, ?)`;

/** Where a session's next pick may start its walk of the due index. */
export interface PickFloors {
  /** A due stamp no card the session could still be shown precedes, when one is known. */
  floor(sid: string, deckId: number): string | null;
  /** The pick's earliest eligible card, due at `nextDue`. */
  found(sid: string, deckId: number, nextDue: SqlValue): void;
}

// One statement for every read of a session row, so a request that reads it
// for the view and again for the version guard reads it once.
const SESSION_ROW = 'SELECT * FROM study_sessions WHERE id = ?';

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
    private readonly floors?: PickFloors,
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
    const row = this.db.first(SESSION_ROW, sid);
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
    const row = this.db.first<{ version: number; deck_id: number }>(SESSION_ROW, sid);
    if (!row) throw new SessionNotFound(`session ${sid} not found for user`);
    if (Number(row.version) !== expected) throw new StaleVersionError(Number(row.version));
    return { deck_id: Number(row.deck_id) };
  }

  private checkedAnswerable(sid: string, questionId: number, expectedVersion: number): { deck_id: number } {
    const row = this.db.first<{
      version: number;
      deck_id: number;
      status: string;
      state: string;
      current_question_id: number | null;
    }>(SESSION_ROW, sid);
    if (!row) throw new SessionNotFound(`session ${sid} not found for user`);
    const currentVersion = Number(row.version);
    if (
      currentVersion !== expectedVersion ||
      row.status !== 'active' ||
      row.state !== 'awaiting-answer' ||
      Number(row.current_question_id) !== questionId
    ) {
      throw new StaleVersionError(currentVersion);
    }
    return { deck_id: Number(row.deck_id) };
  }

  assertAnswerable(sid: string, questionId: number, expectedVersion: number): void {
    this.checkedAnswerable(sid, questionId, expectedVersion);
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
      this.checkedAnswerable(sid, questionId, expectedVersion);
      const v = expectedVersion + 1;
      this.db.run(
        RECORD_ANSWER,
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

  setGrading(sid: string, questionId: number, workflowId: string, expectedVersion: number): number {
    const ts = isoNow(this.clock);
    return this.storage.transactionSync(() => {
      this.checkedAnswerable(sid, questionId, expectedVersion);
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
      const row = this.db.first<{
        state: string;
        current_question_id: number | null;
        current_grading_workflow_id: string | null;
      }>(
        'SELECT state, current_question_id, current_grading_workflow_id FROM study_sessions WHERE id = ?',
        sid,
      );
      if (
        !row ||
        row.state !== 'grading' ||
        Number(row.current_question_id) !== questionId ||
        row.current_grading_workflow_id !== workflowId
      ) return;
      this.db.run(
        RECORD_GRADED_ANSWER,
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
    // Both reads walk the due index, never the whole deck: the oldest
    // eligible card names the hour, then the draw covers that hour from that
    // card on, since no eligible card is due before it. The range only
    // narrows the walk; the bucket compare decides membership.
    const now = isoNow(this.clock);
    const floor = this.floors?.floor(sid, deckId) ?? null;
    const first =
      floor === null
        ? this.db.first<{ bucket: string; nd: SqlValue }>(FIRST_DUE, now, deckId, sid)
        : this.db.first<{ bucket: string; nd: SqlValue }>(FIRST_DUE_FROM, floor, now, deckId, sid);
    if (first === null) return null;
    this.floors?.found(sid, deckId, first.nd);
    const row = this.db.first<{ id: number; skeleton: string | null }>(DRAW, first.nd, first.bucket, now, first.bucket, deckId, sid);
    return row ? { id: Number(row.id), skeleton: row.skeleton } : null;
  }
}
