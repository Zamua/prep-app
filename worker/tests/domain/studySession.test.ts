// The study session lifecycle, driven by event sequences.
import { describe, expect, it } from 'vitest';
import { columnsOf, next, sessionOf, type Move, type Session, type SessionEvent } from '../../domain/study/session.js';

const RIGHT = { result: 'right', feedback: '' };
const SRS = { step: 1 };

/** Applies `events` in order, failing on any that does not move the session. */
function run(from: Session, ...events: SessionEvent[]): Session {
  let s = from;
  for (const e of events) {
    const m = next(s, e);
    if (!('to' in m)) throw new Error(`${e.type} from ${s.kind}: ${JSON.stringify(m)}`);
    s = m.to;
  }
  return s;
}

const awaiting = (questionId: number | null, draft = ''): Session => ({ kind: 'awaiting', questionId, draft });
const grading: Session = { kind: 'grading', questionId: 1, workflowId: 'w1', draft: 'typed' };
const result: Session = { kind: 'result', questionId: 1, verdict: RIGHT, srs: SRS };

const STATES: Session[] = [awaiting(1), awaiting(null), grading, result, { kind: 'completed' }, { kind: 'abandoned' }];
const EVENTS: SessionEvent[] = [
  { type: 'draft', draft: 'd' },
  { type: 'answer', questionId: 1, verdict: RIGHT, srs: SRS },
  { type: 'grade', questionId: 1, workflowId: 'w1' },
  { type: 'graded', questionId: 1, workflowId: 'w1', verdict: RIGHT, srs: SRS },
  { type: 'gradeDropped', workflowId: 'w1' },
  { type: 'advance', next: { id: 2, skeleton: null } },
  { type: 'exhausted' },
  { type: 'abandon' },
  { type: 'idle' },
];

const outcome = (m: Move): string => ('to' in m ? `to ${m.to.kind}` : 'refused' in m ? 'refused' : 'ignored');

describe('a study session', () => {
  it('shows a verdict reached at once, records it, then moves to the next card', () => {
    const answered = next(awaiting(1), { type: 'answer', questionId: 1, verdict: RIGHT, srs: SRS });
    expect(answered).toEqual({ to: result, answer: { questionId: 1, result: 'right', workflowId: null }, active: true });
    expect(run(result, { type: 'advance', next: { id: 2, skeleton: 'def f():' } })).toEqual(awaiting(2, 'def f():'));
  });

  it('lands a grade only from the job in flight, once', () => {
    const inFlight = run(awaiting(1, 'typed'), { type: 'grade', questionId: 1, workflowId: 'w1' });
    expect(inFlight).toEqual(grading);
    expect(next(inFlight, { type: 'graded', questionId: 1, workflowId: 'w0', verdict: RIGHT, srs: SRS })).toHaveProperty('ignored');
    const landed = next(inFlight, { type: 'graded', questionId: 1, workflowId: 'w1', verdict: RIGHT, srs: SRS });
    expect(landed).toMatchObject({ to: result, answer: { questionId: 1, workflowId: 'w1' } });
    expect(next(result, { type: 'graded', questionId: 1, workflowId: 'w1', verdict: RIGHT, srs: SRS })).toHaveProperty('ignored');
  });

  it('gives the card back with the typed answer when the grade is dropped', () => {
    expect(next(grading, { type: 'gradeDropped', workflowId: 'w0' })).toHaveProperty('ignored');
    expect(run(grading, { type: 'gradeDropped', workflowId: 'w1' })).toEqual(awaiting(1, 'typed'));
  });

  it('completes when the pick finds nothing, or the card on screen is gone', () => {
    expect(run(result, { type: 'advance', next: null })).toEqual({ kind: 'completed' });
    expect(run(awaiting(null), { type: 'advance', next: null })).toEqual({ kind: 'completed' });
    expect(run(awaiting(1), { type: 'exhausted' })).toEqual({ kind: 'completed' });
  });

  it('refuses every move an outdated view could ask for', () => {
    const refusals: [Session, SessionEvent][] = [
      [awaiting(1), { type: 'advance', next: { id: 2, skeleton: null } }],
      [awaiting(1), { type: 'answer', questionId: 2, verdict: RIGHT, srs: SRS }],
      [awaiting(1), { type: 'grade', questionId: 2, workflowId: 'w1' }],
      ...(['draft', 'answer', 'grade', 'advance'] as const).flatMap((type) => {
        const e = EVENTS.find((x) => x.type === type)!;
        return [grading, ...(type === 'advance' ? [] : [result]), { kind: 'completed' }, { kind: 'abandoned' }].map((s) => [s, e] as [Session, SessionEvent]);
      }),
    ];
    for (const [s, e] of refusals) expect(next(s, e), `${e.type} from ${s.kind}`).toHaveProperty('refused');
  });

  it('abandons from any active state, and the week-old sweep is not the learner acting', () => {
    for (const s of [awaiting(1), grading, result]) {
      expect(next(s, { type: 'abandon' })).toMatchObject({ to: { kind: 'abandoned' }, active: true });
      expect(next(s, { type: 'idle' })).toMatchObject({ to: { kind: 'abandoned' }, active: false });
    }
  });

  it('stays ended whatever arrives', () => {
    for (const s of [{ kind: 'completed' }, { kind: 'abandoned' }] as Session[]) {
      for (const e of EVENTS) expect(outcome(next(s, e)), `${e.type} from ${s.kind}`).not.toMatch(/^to /);
    }
  });

  it('decides every state and event', () => {
    for (const s of STATES) for (const e of EVENTS) expect(['to', 'refused', 'ignored'].filter((k) => k in next(s, e))).toHaveLength(1);
  });
});

describe('a stored session row', () => {
  it('reads back as the state it was written from', () => {
    for (const s of [awaiting(1, 'x'), awaiting(null), grading, result, { kind: 'completed' }, { kind: 'abandoned' }] as Session[]) {
      expect(sessionOf(columnsOf(s))).toEqual(s);
    }
  });

  it('reads a grading row with no job, or a verdict with no card, as the card awaiting its answer', () => {
    const base = columnsOf(awaiting(3, 'x'));
    expect(sessionOf({ ...base, state: 'grading' })).toEqual(awaiting(3, 'x'));
    expect(sessionOf({ ...base, state: 'showing-result' })).toEqual(awaiting(3, 'x'));
  });
});
