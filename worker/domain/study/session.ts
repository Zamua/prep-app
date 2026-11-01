// A study session's lifecycle: the states a session can be in, and the one
// function that moves it between them. The repository persists what `next`
// returns and nothing else.

type Json = Record<string, unknown>;

export type Session =
  /** A card on screen, or none when nothing was due at the last pick. */
  | { kind: 'awaiting'; questionId: number | null; draft: string }
  /** The answer is with a grading job; the typed answer is kept for a retry. */
  | { kind: 'grading'; questionId: number; workflowId: string; draft: string }
  /** A verdict on screen until the learner moves on. */
  | { kind: 'result'; questionId: number; verdict: Json; srs: Json }
  | { kind: 'completed' }
  | { kind: 'abandoned' };

export type SessionEvent =
  | { type: 'draft'; draft: string }
  /** A verdict reached without a job: deterministic or self-graded. */
  | { type: 'answer'; questionId: number; verdict: Json; srs: Json }
  | { type: 'grade'; questionId: number; workflowId: string }
  | { type: 'graded'; questionId: number; workflowId: string; verdict: Json; srs: Json }
  /** The job will never land: refused, failed or gone. */
  | { type: 'gradeDropped'; workflowId: string }
  /** `next` is the card the pick drew, null when none is due. */
  | { type: 'advance'; next: { id: number; skeleton: string | null } | null }
  /** The view found the card on screen gone. */
  | { type: 'exhausted' }
  | { type: 'abandon' }
  /** A week with no activity. */
  | { type: 'idle' };

export interface AnswerWrite {
  questionId: number;
  result: string;
  workflowId: string | null;
}

export type Move =
  /** `active` is false for a move the learner did not make, which leaves
   * the session's last activity where it was. */
  | { to: Session; answer: AnswerWrite | null; active: boolean }
  /** Not a move from this state: the caller acted on an outdated view. */
  | { refused: string }
  /** A late or repeated report about a grade this session is no longer waiting on. */
  | { ignored: string };

const to = (s: Session, answer: AnswerWrite | null = null): Move => ({ to: s, answer, active: true });
const refused = (why: string): Move => ({ refused: why });
const ignored = (why: string): Move => ({ ignored: why });

/** Where a session goes from `s` on `e`. Every pair is decided here. */
export function next(s: Session, e: SessionEvent): Move {
  switch (s.kind) {
    case 'awaiting':
      return fromAwaiting(s, e);
    case 'grading':
      return fromGrading(s, e);
    case 'result':
      return fromResult(s, e);
    case 'completed':
    case 'abandoned':
      return fromEnded(s, e);
  }
}

function fromAwaiting(s: Extract<Session, { kind: 'awaiting' }>, e: SessionEvent): Move {
  switch (e.type) {
    case 'draft':
      return to({ ...s, draft: e.draft });
    case 'answer':
      if (e.questionId !== s.questionId) return refused('another card is on screen');
      return to({ kind: 'result', questionId: e.questionId, verdict: e.verdict, srs: e.srs }, { questionId: e.questionId, result: String(e.verdict['result']), workflowId: null });
    case 'grade':
      if (e.questionId !== s.questionId) return refused('another card is on screen');
      return to({ kind: 'grading', questionId: e.questionId, workflowId: e.workflowId, draft: s.draft });
    case 'graded':
    case 'gradeDropped':
      return ignored('no grade in flight');
    case 'advance':
      if (s.questionId !== null) return refused('the card on screen is unanswered');
      return to(picked(e.next));
    case 'exhausted':
      return to({ kind: 'completed' });
    case 'abandon':
      return to({ kind: 'abandoned' });
    case 'idle':
      return { to: { kind: 'abandoned' }, answer: null, active: false };
  }
}

function fromGrading(s: Extract<Session, { kind: 'grading' }>, e: SessionEvent): Move {
  switch (e.type) {
    case 'draft':
    case 'answer':
    case 'grade':
    case 'advance':
      return refused('the answer is being graded');
    case 'graded':
      if (e.questionId !== s.questionId || e.workflowId !== s.workflowId) return ignored('not the grade in flight');
      return to({ kind: 'result', questionId: e.questionId, verdict: e.verdict, srs: e.srs }, { questionId: e.questionId, result: String(e.verdict['result']), workflowId: e.workflowId });
    case 'gradeDropped':
      if (e.workflowId !== s.workflowId) return ignored('not the grade in flight');
      return to({ kind: 'awaiting', questionId: s.questionId, draft: s.draft });
    case 'exhausted':
      return ignored('a graded card is still on screen');
    case 'abandon':
      return to({ kind: 'abandoned' });
    case 'idle':
      return { to: { kind: 'abandoned' }, answer: null, active: false };
  }
}

function fromResult(s: Extract<Session, { kind: 'result' }>, e: SessionEvent): Move {
  switch (e.type) {
    case 'draft':
    case 'answer':
    case 'grade':
      return refused('the card was already answered');
    case 'graded':
    case 'gradeDropped':
      return ignored('the grade already landed');
    case 'advance':
      return to(picked(e.next));
    case 'exhausted':
      return ignored(`a verdict is on screen for card ${s.questionId}`);
    case 'abandon':
      return to({ kind: 'abandoned' });
    case 'idle':
      return { to: { kind: 'abandoned' }, answer: null, active: false };
  }
}

function fromEnded(s: Extract<Session, { kind: 'completed' | 'abandoned' }>, e: SessionEvent): Move {
  switch (e.type) {
    case 'draft':
    case 'answer':
    case 'grade':
    case 'advance':
      return refused(`the session is ${s.kind}`);
    case 'graded':
    case 'gradeDropped':
    case 'exhausted':
    case 'abandon':
    case 'idle':
      return ignored(`the session is ${s.kind}`);
  }
}

function picked(card: { id: number; skeleton: string | null } | null): Session {
  return card === null ? { kind: 'completed' } : { kind: 'awaiting', questionId: card.id, draft: card.skeleton || '' };
}

/** The columns a session row stores its lifecycle in. */
export interface SessionColumns {
  status: string;
  state: string;
  current_question_id: number | null;
  current_draft: string | null;
  current_grading_workflow_id: string | null;
  last_answered_qid: number | null;
  last_answered_verdict: Json | null;
  last_answered_state: Json | null;
}

/** A stored row as a state. A row no current code writes (grading with no
 * job, a verdict with no card) reads as the card awaiting its answer. */
export function sessionOf(row: SessionColumns): Session {
  if (row.status === 'completed') return { kind: 'completed' };
  if (row.status === 'abandoned') return { kind: 'abandoned' };
  const awaiting: Session = { kind: 'awaiting', questionId: row.current_question_id, draft: row.current_draft ?? '' };
  if (row.state === 'grading') {
    if (row.current_question_id === null || !row.current_grading_workflow_id) return awaiting;
    return { kind: 'grading', questionId: row.current_question_id, workflowId: row.current_grading_workflow_id, draft: row.current_draft ?? '' };
  }
  if (row.state === 'showing-result') {
    if (row.last_answered_qid === null) return awaiting;
    return { kind: 'result', questionId: row.last_answered_qid, verdict: row.last_answered_verdict ?? {}, srs: row.last_answered_state ?? {} };
  }
  return awaiting;
}

/** The row a state is stored as; every lifecycle column is written. */
export function columnsOf(s: Session): SessionColumns {
  const blank = { current_draft: null, current_grading_workflow_id: null, last_answered_qid: null, last_answered_verdict: null, last_answered_state: null };
  switch (s.kind) {
    case 'awaiting':
      return { ...blank, status: 'active', state: 'awaiting-answer', current_question_id: s.questionId, current_draft: s.draft };
    case 'grading':
      return { ...blank, status: 'active', state: 'grading', current_question_id: s.questionId, current_draft: s.draft, current_grading_workflow_id: s.workflowId };
    case 'result':
      return { ...blank, status: 'active', state: 'showing-result', current_question_id: s.questionId, last_answered_qid: s.questionId, last_answered_verdict: s.verdict, last_answered_state: s.srs };
    case 'completed':
    case 'abandoned':
      return { ...blank, status: s.kind, state: 'awaiting-answer', current_question_id: null };
  }
}
