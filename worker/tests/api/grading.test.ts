// The AI grading poll. The verdict is written before the transition that
// makes the job terminal, so a terminal status with no result is a job that
// produced none - there is nothing left to wait for, and the session has to
// be released or the client polls a verdict that never lands.
import { describe, expect, it } from 'vitest';
import { gradingPoll, parseGradingWid } from '../../app/study/grading.js';
import type { StudyDeps } from '../../app/study/api.js';
import type { JobStatus, WorkflowRunner } from '../../app/ports.js';
import { cell } from '../repos/setup.js';

/** The runner as this route sees it: `status` and nothing else. */
function reading(rows: Record<string, JobStatus> = {}): WorkflowRunner {
  return {
    start: async () => ({ workflowId: '' }),
    signal: async () => null,
    status: async (id) => rows[id] ?? null,
    terminate: async () => {},
  };
}

function deps(rows: Record<string, JobStatus> = {}): StudyDeps {
  const c = cell();
  return {
    repos: c.repos,
    clock: c.clock,
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
    agentAvailable: true,
    freeTierConfigured: true,
    runner: reading(rows),
  };
}

/** A deck, a free-text card and an open session parked on it. */
async function graded(rows: (qid: number) => Record<string, JobStatus>): Promise<{ d: StudyDeps; qid: number; wid: string; sid: string }> {
  const c = cell();
  const deck = c.repos.decks.create('capitals');
  const qid = c.repos.questions.add(deck, { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima' });
  const sid = await c.repos.sessions.create(deck, 'iPhone');
  const wid = `grade-capitals-q${qid}-abcdef`;
  const session = c.repos.sessions.get(sid)!;
  c.repos.sessions.setGrading(sid, qid, wid, session.version);
  const d: StudyDeps = {
    repos: c.repos,
    clock: c.clock,
    userAgent: null,
    agentAvailable: true,
    freeTierConfigured: true,
    runner: reading(rows(qid)),
  };
  return { d, qid, wid, sid };
}

const RESULT = {
  verdict: { result: 'right', feedback: 'Lima is right.' },
  state: { step: 2, next_due: '2026-03-16T15:00:00+00:00', interval_minutes: 1440, extra: 'dropped' },
  user_answer: 'Lima',
  idk: false,
};

describe('parseGradingWid', () => {
  it('walks from the right, so a hyphenated deck name survives', () => {
    expect(parseGradingWid('grade-world-capitals-q42-abcdef')).toEqual(['world-capitals', 42]);
    expect(parseGradingWid('grade-x-q1-a')).toEqual(['x', 1]);
  });

  it('refuses anything that is not a grading id', () => {
    for (const wid of ['not-a-grading-id', 'grade-x-1-a', 'grade-x-qz-a', 'grade-a-b', 'transform-x-q1-a']) {
      expect(parseGradingWid(wid), wid).toBeNull();
    }
  });
});

describe('GET /api/study/grading/{wid}', () => {
  it('refuses a malformed id before touching any row', async () => {
    expect(await gradingPoll(deps(), 'nope', '')).toEqual({
      json: { error: { code: 'malformed_workflow_id', message: 'workflow id is not a grading id' } },
      status: 400,
    });
  });

  it('answers 404 for a question the caller does not own, so a guessed id leaks nothing', async () => {
    expect(await gradingPoll(deps(), 'grade-x-q999999-abcdef', '')).toMatchObject({ status: 404 });
  });

  it('reports the running status, and carries the note the step wrote while running', async () => {
    const { d, wid } = await graded((qid) => ({
      [`grade-capitals-q${qid}-abcdef`]: { status: 'grading', progress: { error: 'Free AI is busy right now' } },
    }));
    expect(await gradingPoll(d, wid, 'sid-1')).toEqual({
      json: { pending: { poll: `/api/study/grading/${wid}?sid=sid-1`, workflow_id: wid, status: 'grading', error: 'Free AI is busy right now' } },
      status: 200,
    });
  });

  it('omits the note when the step has not written one', async () => {
    const { d, wid } = await graded((qid) => ({ [`grade-capitals-q${qid}-abcdef`]: { status: 'recording', progress: {} } }));
    expect(await gradingPoll(d, wid, '')).toEqual({
      json: { pending: { poll: `/api/study/grading/${wid}`, workflow_id: wid, status: 'recording' } },
      status: 200,
    });
  });

  it('reports the failure and releases the session when no row backs the id', async () => {
    const { d, wid, sid } = await graded(() => ({}));
    expect(await gradingPoll(d, wid, sid)).toEqual({
      json: { failed: { code: 'grading_failed', message: 'the grader returned nothing' } },
      status: 200,
    });
    expect(d.repos.sessions.get(sid)!.state).not.toBe('grading');
  });

  it('reports a terminal job that produced no result, keeping the step error as the message', async () => {
    const { d, wid, sid } = await graded((qid) => ({
      [`grade-capitals-q${qid}-abcdef`]: { status: 'failed', progress: { error: 'the model refused' } },
    }));
    expect(await gradingPoll(d, wid, sid)).toEqual({
      json: { failed: { code: 'grading_failed', message: 'the model refused' } },
      status: 200,
    });
    expect(d.repos.sessions.get(sid)!.state).not.toBe('grading');
  });

  it('lands the verdict on the session and answers the revealed outcome', async () => {
    const { d, qid, wid, sid } = await graded((id) => ({ [`grade-capitals-q${id}-abcdef`]: { status: 'done', progress: { result: RESULT } } }));
    const res = (await gradingPoll(d, wid, sid)) as { json: Record<string, unknown>; status: number };
    expect(res.status).toBe(200);
    expect(res.json['verdict']).toBe('right');
    expect(res.json['feedback']).toBe('Lima is right.');
    expect(res.json['nextDueMinutes']).toBe(1440);
    expect(res.json['answer']).toBe('Lima');
    expect(res.json['idk']).toBe(false);
    expect((res.json['card'] as Record<string, unknown>)['answer']).toBe('Lima');
    expect(res.json['session']).toMatchObject({ id: sid, state: 'showing-result', deck_name: 'capitals' });
    const s = d.repos.sessions.get(sid)!;
    expect(s.last_answered_qid).toBe(qid);
    // Only the three FSRS columns the client renders travel; the rest of the
    // step's state stays in the ledger.
    expect(s.last_answered_state).toEqual({ step: 2, next_due: '2026-03-16T15:00:00+00:00', interval_minutes: 1440 });
  });

  it('is idempotent: a repeat poll answers the same outcome', async () => {
    const { d, wid, sid } = await graded((id) => ({ [`grade-capitals-q${id}-abcdef`]: { status: 'done', progress: { result: RESULT } } }));
    const first = await gradingPoll(d, wid, sid);
    expect(await gradingPoll(d, wid, sid)).toEqual(first);
  });

  it('answers without a session when the poll carries no sid', async () => {
    const { d, wid } = await graded((id) => ({ [`grade-capitals-q${id}-abcdef`]: { status: 'done', progress: { result: RESULT } } }));
    const res = (await gradingPoll(d, wid, '')) as { json: Record<string, unknown> };
    expect(res.json['session']).toBeNull();
    expect(res.json['verdict']).toBe('right');
  });
});
