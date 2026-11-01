// `study_sessions` and `study_session_answers`. Version-checked mutations raise
// `StaleVersionError`; the next card is the most overdue, ties within the
// hour broken by RANDOM().
import type { Clock, SessionIds, SessionRepo } from '../../../app/ports.js';
import { SessionNotFound, StaleVersionError } from '../../../app/ports.js';
import type { RecentSession, SessionState, SessionStatus, StudySession } from '../../../app/entities.js';
import { columnsOf, next, sessionOf, type Session, type SessionEvent } from '../../../domain/study/session.js';
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
    const first = this.pickNextQuestion(deckId, sid);
    const c = columnsOf({ kind: 'awaiting', questionId: first ? first.id : null, draft: first ? first.skeleton || '' : '' });
    this.db.run(
      `INSERT INTO study_sessions
         (id, deck_id, created_at, last_active, status, state, current_question_id, current_draft, version, device_label)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)`,
      sid,
      deckId,
      ts,
      ts,
      c.status,
      c.state,
      c.current_question_id,
      c.current_draft,
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

  retireIdle(): number {
    const before = isoUtc(shifted(this.clock.now(), -7 * DAY_MS));
    const idle = this.db.all<{ id: string }>(`SELECT id FROM study_sessions WHERE status = 'active' AND last_active < ?`, before);
    for (const { id } of idle) this.apply(id, null, { type: 'idle' });
    return idle.length;
  }

  listRecent(limit = 5): RecentSession[] {
    return this.db
      .all(
        `${RECENT_SELECT}
          WHERE s.status = 'active' AND (s.snoozed_until IS NULL OR s.snoozed_until <= ?)
          ORDER BY s.last_active DESC
          LIMIT ?`,
        isoNow(this.clock),
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

  /**
   * The one write a session's lifecycle takes: `e` applied to the stored
   * state. A caller holding a version is refused when the row moved past it
   * or the move is not one this state allows; an event the session itself
   * raises (a grade landing, the week-old sweep) is dropped instead. The
   * new version, or null when nothing moved.
   */
  private apply(sid: string, expected: number | null, e: SessionEvent | ((s: Session) => SessionEvent)): number | null {
    return this.storage.transactionSync(() => {
      const row = this.db.first(SESSION_ROW, sid);
      if (!row) {
        if (expected === null) return null;
        throw new SessionNotFound(`session ${sid} not found for user`);
      }
      const version = Number(row['version'] || 1);
      if (expected !== null && version !== expected) throw new StaleVersionError(version);
      const session = sessionOf(rowToSession(row));
      const move = next(session, typeof e === 'function' ? e(session) : e);
      if ('refused' in move) {
        if (expected === null) return null;
        throw new StaleVersionError(version);
      }
      if ('ignored' in move) return null;
      const ts = isoNow(this.clock);
      if (move.answer !== null) {
        const a = move.answer;
        if (a.workflowId === null) this.db.run(RECORD_ANSWER, sid, a.questionId, ts, a.result);
        else this.db.run(RECORD_GRADED_ANSWER, sid, a.questionId, ts, a.result, a.workflowId);
      }
      const c = columnsOf(move.to);
      this.db.run(
        `UPDATE study_sessions SET status = ?, state = ?, current_question_id = ?, current_draft = ?, current_grading_workflow_id = ?,
                last_answered_qid = ?, last_answered_verdict = ?, last_answered_state = ?, version = ?, last_active = ?
          WHERE id = ?`,
        c.status,
        c.state,
        c.current_question_id,
        c.current_draft,
        c.current_grading_workflow_id,
        c.last_answered_qid,
        c.last_answered_verdict === null ? null : JSON.stringify(c.last_answered_verdict),
        c.last_answered_state === null ? null : JSON.stringify(c.last_answered_state),
        version + 1,
        move.active ? ts : String(row['last_active']),
        sid,
      );
      return version + 1;
    });
  }

  /** Refuses as `apply` would, with nothing written. */
  private assertAllows(sid: string, expected: number, e: SessionEvent): void {
    const row = this.db.first(SESSION_ROW, sid);
    if (!row) throw new SessionNotFound(`session ${sid} not found for user`);
    const version = Number(row['version'] || 1);
    if (version !== expected || 'refused' in next(sessionOf(rowToSession(row)), e)) throw new StaleVersionError(version);
  }

  assertAnswerable(sid: string, questionId: number, expectedVersion: number): void {
    this.assertAllows(sid, expectedVersion, { type: 'grade', questionId, workflowId: '' });
  }

  updateDraft(sid: string, draft: string, expectedVersion: number): number {
    return this.apply(sid, expectedVersion, { type: 'draft', draft })!;
  }

  recordAnswerSync(
    sid: string,
    questionId: number,
    expectedVersion: number,
    _userAnswer: string,
    verdict: Record<string, unknown>,
    state: Record<string, unknown>,
  ): number {
    return this.apply(sid, expectedVersion, { type: 'answer', questionId, verdict, srs: state })!;
  }

  setGrading(sid: string, questionId: number, workflowId: string, expectedVersion: number): number {
    return this.apply(sid, expectedVersion, { type: 'grade', questionId, workflowId })!;
  }

  gradingCompleted(sid: string, questionId: number, verdict: Record<string, unknown>, state: Record<string, unknown>, workflowId: string): void {
    this.apply(sid, null, { type: 'graded', questionId, workflowId, verdict, srs: state });
  }

  gradingAbandoned(sid: string, workflowId: string): void {
    this.apply(sid, null, { type: 'gradeDropped', workflowId });
  }

  advance(sid: string, expectedVersion: number): number {
    // The pick reads the due index, so it runs only once the move is allowed.
    this.assertAllows(sid, expectedVersion, { type: 'advance', next: null });
    const deckId = Number(this.db.first<{ deck_id: number }>(SESSION_ROW, sid)!.deck_id);
    return this.apply(sid, expectedVersion, () => ({ type: 'advance', next: this.pickNextQuestion(deckId, sid) }))!;
  }

  markCompleted(sid: string): void {
    this.apply(sid, null, { type: 'exhausted' });
  }

  abandon(sid: string): void {
    this.apply(sid, null, { type: 'abandon' });
  }

  abandonAllForDeck(deckId: number): number {
    const open = this.db.all<{ id: string }>(`SELECT id FROM study_sessions WHERE deck_id = ? AND status = 'active'`, deckId);
    for (const { id } of open) this.apply(id, null, { type: 'abandon' });
    return open.length;
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
