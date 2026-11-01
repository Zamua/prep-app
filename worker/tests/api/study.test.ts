import { describe, expect, it } from 'vitest';
import { DurationError, FOREVER_ISO, parseUntil } from '../../app/durations.js';
import * as study from '../../app/study/api.js';
import { buildMessage, DEFAULT_PROVIDER, providerUrls, quoteAll } from '../../app/study/handoff.js';
import { RunnerUnavailable, StaleVersionError, type GradeAnswerInput, type WorkflowRunner } from '../../app/ports.js';
import { RequestValidationError } from '../../app/validation.js';
import { keyedGradeId } from '../../domain/jobs/ids.js';
import { cell, MutableClock, TEST_NOW } from '../repos/setup.js';

const IDLE = { signal: async () => null, status: async () => null, terminate: async () => {} };

const refusing: WorkflowRunner = {
  start: async () => {
    throw new RunnerUnavailable('jobs are off on this deploy');
  },
  ...IDLE,
};

function deps(opts: { agentAvailable?: boolean; runner?: WorkflowRunner } = {}): study.StudyDeps {
  const c = cell();
  return {
    repos: c.repos,
    clock: c.clock,
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
    agentAvailable: opts.agentAvailable ?? false,
    // The two answer the same question; the start's precondition reads this one.
    freeTierConfigured: opts.agentAvailable ?? false,
    runner: opts.runner ?? refusing,
  };
}

const body = (json: unknown) => json;

describe('a submission body', () => {
  const refusal = async (run: () => unknown): Promise<unknown> => {
    try {
      await run();
    } catch (e) {
      if (e instanceof RequestValidationError) return e.errors.map((x) => [x.type, x.loc]);
      throw e;
    }
    return null;
  };

  it('takes ids and versions as integers, a numeric string included', async () => {
    const d = deps();
    const deck = d.repos.decks.create('capitals');
    const qid = d.repos.questions.add(deck, { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima' });
    const sid = await d.repos.sessions.create(deck, 'test');
    const version = d.repos.sessions.get(sid)!.version;
    expect(await refusal(() => study.deckSubmit(d, 'capitals', { question_id: 'abc', answer: 'Lima' }))).toEqual([['int_type', ['body', 'question_id']]]);
    expect(await refusal(() => study.deckSubmit(d, 'capitals', { question_id: 1.5, answer: 'Lima' }))).toEqual([['int_type', ['body', 'question_id']]]);
    expect(await refusal(() => study.deckSubmit(d, 'capitals', { answer: 'Lima' }))).toEqual([['missing', ['body', 'question_id']]]);
    expect(await refusal(() => study.sessionSubmit(d, sid, { question_id: qid, version: 'x', answer: 'Lima' }))).toEqual([['int_type', ['body', 'version']]]);
    expect(await refusal(() => study.sessionDraft(d, sid, { version: 'x', draft: 'L' }))).toEqual([['int_type', ['body', 'version']]]);
    expect(await refusal(() => study.sessionAdvance(d, sid, { version: [] }))).toEqual([['int_type', ['body', 'version']]]);
    expect(study.sessionDraft(d, sid, { version: String(version), draft: 'L' })).toMatchObject({ status: 200 });
  });

  it('refuses a self-grade that is neither right nor wrong before any lookup', async () => {
    const d = deps();
    const result = await study.sessionSubmit(d, 'no-such-session', { question_id: 1, version: 0, verdict: 'maybe' });
    expect(result).toMatchObject({ status: 422, json: { error: { code: 'invalid_verdict' } } });
  });

  it('asks a session submission for its version after the session and card are found', async () => {
    const d = deps();
    const deck = d.repos.decks.create('capitals');
    const qid = d.repos.questions.add(deck, { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima' });
    const sid = await d.repos.sessions.create(deck, 'test');
    expect(await study.sessionSubmit(d, sid, { question_id: qid, answer: 'Lima' })).toMatchObject({ status: 422, json: { error: { code: 'version_required' } } });
    expect(await study.sessionSubmit(d, 'no-such-session', { question_id: qid, answer: 'Lima' })).toMatchObject({ status: 404 });
  });
});

describe('a free-text submission', () => {
  it('reveals for self-grading when no tier funds a judge', async () => {
    const d = deps({ agentAvailable: false });
    const deck = d.repos.decks.create('capitals');
    const qid = d.repos.questions.add(deck, { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima' });
    const result = (await study.deckSubmit(d, 'capitals', body({ question_id: qid, answer: 'Lima' }))) as { json: Record<string, unknown> };
    expect(result.json['selfGrade']).toBe(true);
    expect(result.json['answer']).toBe('Lima');
    expect((result.json['card'] as Record<string, unknown>)['answer']).toBe('Lima');
  });

  it('rejects a stale session before the no-agent self-grade fallback', async () => {
    const d = deps({ agentAvailable: false });
    const deck = d.repos.decks.create('capitals');
    const qid = d.repos.questions.add(deck, { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima' });
    const sid = await d.repos.sessions.create(deck, 'test');
    const session = d.repos.sessions.get(sid)!;
    d.repos.sessions.updateDraft(sid, 'newer answer', session.version);

    const result = await study.sessionSubmit(d, sid, {
      question_id: qid,
      version: session.version,
      answer: 'Lima',
      idk: false,
    });

    expect(result).toMatchObject({
      status: 409,
      json: { error: { code: 'stale_version', current_version: session.version + 1 } },
    });
  });

  it('falls back to the same reveal when the runner refuses', async () => {
    const d = deps({ agentAvailable: true, runner: refusing });
    const deck = d.repos.decks.create('capitals');
    const qid = d.repos.questions.add(deck, { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima' });
    const result = (await study.deckSubmit(d, 'capitals', body({ question_id: qid, answer: 'Lima' }))) as { json: Record<string, unknown> };
    expect(result.json['selfGrade']).toBe(true);
  });

  it('registers the job and answers a poll url when a runner is there', async () => {
    const started: string[] = [];
    const runner: WorkflowRunner = {
      start: async (type) => {
        started.push(type);
        return { workflowId: 'grade-capitals-q1-abcdef' };
      },
      ...IDLE,
    };
    const d = deps({ agentAvailable: true, runner });
    const deck = d.repos.decks.create('capitals');
    const qid = d.repos.questions.add(deck, { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima' });
    const result = (await study.deckSubmit(d, 'capitals', body({ question_id: qid, answer: 'Lima' }))) as { json: Record<string, unknown> };
    expect(started).toEqual(['GradeAnswer']);
    expect(result.json['pending']).toEqual({ poll: '/api/study/grading/grade-capitals-q1-abcdef', workflow_id: 'grade-capitals-q1-abcdef' });
    expect(d.repos.jobs.get('grade-capitals-q1-abcdef')).toMatchObject({ workflow_type: 'grading', status: 'grading' });
  });
});

describe('a session grade start', () => {
  /** A deck, a free-text card and a session parked on it, graded by `runner`. */
  async function parked(runner: (d: study.StudyDeps, sid: string) => WorkflowRunner) {
    let sid = '';
    const d = deps({ agentAvailable: true, runner: { ...IDLE, start: (...args) => runner(d, sid).start(...args) } });
    const deck = d.repos.decks.create('capitals');
    const qid = d.repos.questions.add(deck, { type: 'short', prompt: 'Capital of Peru?', answer: 'Lima' });
    sid = await d.repos.sessions.create(deck, 'test');
    const version = d.repos.sessions.get(sid)!.version;
    const submit = () => study.sessionSubmit(d, sid, { question_id: qid, version, answer: 'Lima', idk: false });
    return { d, qid, sid, version, submit };
  }

  it('claims the session before the start yields, so a write interleaved with the start cannot orphan the job', async () => {
    let interleaved: unknown = null;
    const started: string[] = [];
    const { d, sid, version, submit } = await parked((d, sid) => ({
      ...IDLE,
      start: async (_kind, input, opts) => {
        try {
          d.repos.sessions.updateDraft(sid, 'still typing', version);
        } catch (e) {
          interleaved = e;
        }
        const id = keyedGradeId((input as GradeAnswerInput).questionId, opts!.idempotencyKey!);
        started.push(id);
        return { workflowId: id };
      },
    }));

    const result = await submit();

    expect(result).toMatchObject({ json: { pending: { workflow_id: started[0] } } });
    expect(d.repos.sessions.get(sid)).toMatchObject({ state: 'grading', current_grading_workflow_id: started[0] });
    expect(interleaved).toBeInstanceOf(StaleVersionError);
  });

  it('releases a refused claim and answers the self-grade at the released version', async () => {
    const { d, qid, sid, submit } = await parked(() => refusing);

    const result = (await submit()) as { json: { selfGrade?: boolean; session: { version: number } } };

    expect(result.json.selfGrade).toBe(true);
    expect(d.repos.sessions.get(sid)).toMatchObject({ state: 'awaiting-answer', current_grading_workflow_id: null, version: result.json.session.version });
    const recorded = await study.sessionSubmit(d, sid, { question_id: qid, version: result.json.session.version, answer: 'Lima', verdict: 'right' });
    expect(recorded).toMatchObject({ json: { verdict: 'right' } });
  });

  it('keeps the claim when a start fails without a refusal, so a retry starts no second job', async () => {
    const keys: string[] = [];
    const { d, qid, sid, version, submit } = await parked(() => ({
      ...IDLE,
      start: async (_kind, _input, opts) => {
        keys.push(opts!.idempotencyKey!);
        throw new Error('rpc dropped');
      },
    }));

    expect(await submit()).toMatchObject({ status: 502, json: { error: { code: 'grading_start_failed' } } });
    expect(d.repos.sessions.get(sid)).toMatchObject({ state: 'grading', current_grading_workflow_id: keyedGradeId(qid, `${sid}v${version}`) });
    expect(await submit()).toMatchObject({ status: 409 });
    expect(keys).toHaveLength(1);
  });
});

describe('the moves a session refuses', () => {
  const DAY_MS = 24 * 3_600_000;

  /** A deck of `cards` short cards and a session parked on the first. */
  async function studying(cards = 2, runner: WorkflowRunner = refusing) {
    const d = deps({ runner, agentAvailable: runner !== refusing });
    const deck = d.repos.decks.create('capitals');
    for (let i = 0; i < cards; i++) d.repos.questions.add(deck, { type: 'short', prompt: `Capital ${i}?`, answer: `City ${i}` });
    const sid = await d.repos.sessions.create(deck, 'test');
    return { d, sid, session: () => d.repos.sessions.get(sid)! };
  }

  /** Answers the current card Right, leaving the session on its result. */
  async function answered(d: study.StudyDeps, sid: string): Promise<number> {
    const s = d.repos.sessions.get(sid)!;
    const q = d.repos.questions.get(s.current_question_id!)!;
    const result = (await study.sessionSubmit(d, sid, { question_id: q.id, version: s.version, answer: q.answer, verdict: 'right' })) as { json: { session: { version: number } } };
    expect(d.repos.sessions.get(sid)!.state).toBe('showing-result');
    return result.json.session.version;
  }

  it('will not advance past a card nobody answered', async () => {
    const { d, sid, session } = await studying();
    const before = session();
    expect(study.sessionAdvance(d, sid, { version: before.version })).toMatchObject({ status: 409 });
    expect(session()).toMatchObject({ state: 'awaiting-answer', current_question_id: before.current_question_id, version: before.version });
  });

  it('will not advance while the answer is grading, so the grade still lands on it', async () => {
    const runner: WorkflowRunner = { ...IDLE, start: async (_kind, input, opts) => ({ workflowId: keyedGradeId((input as GradeAnswerInput).questionId, opts!.idempotencyKey!) }) };
    const { d, sid, session } = await studying(2, runner);
    const s = session();
    await study.sessionSubmit(d, sid, { question_id: s.current_question_id!, version: s.version, answer: 'the zeroth city', idk: false });
    const grading = session();
    const wid = grading.current_grading_workflow_id;
    expect(grading.state).toBe('grading');
    expect(study.sessionAdvance(d, sid, { version: grading.version })).toMatchObject({ status: 409 });
    expect(session()).toMatchObject({ state: 'grading', current_question_id: s.current_question_id, current_grading_workflow_id: wid });
  });

  it('a session the dashboard retires after a week is stale to the tab that still holds it', async () => {
    const { d, sid, session } = await studying();
    const version = await answered(d, sid);
    (d.clock as MutableClock).advance(8 * DAY_MS);
    d.repos.sessions.retireIdle();
    expect(session().status).toBe('abandoned');
    expect(study.sessionAdvance(d, sid, { version })).toMatchObject({ status: 409 });
    expect(study.sessionDraft(d, sid, { version, draft: 'late' })).toMatchObject({ status: 409 });
    expect(session().status).toBe('abandoned');
  });

  it('a draft lands only on a card awaiting its answer', async () => {
    const { d, sid } = await studying();
    const version = await answered(d, sid);
    expect(study.sessionDraft(d, sid, { version, draft: 'too late' })).toMatchObject({ status: 409 });
  });

  it('abandoning a finished session leaves it finished', async () => {
    const { d, sid, session } = await studying(1);
    const version = await answered(d, sid);
    expect(study.sessionAdvance(d, sid, { version })).toMatchObject({ status: 200 });
    expect(session().status).toBe('completed');
    study.sessionAbandon(d, sid);
    expect(session().status).toBe('completed');
  });
});

describe('authoring a card', () => {
  it('files a deck-less card into the inbox, due immediately', () => {
    const d = deps();
    const result = study.authorCard(d, body({ prompt: '  Capital of Norway?  ', answer: ' Oslo ' })) as { json: Record<string, unknown>; status: number };
    expect(result.status).toBe(201);
    const card = result.json['card'] as Record<string, unknown>;
    expect(card).toMatchObject({ type: 'short', prompt: 'Capital of Norway?', answer: 'Oslo' });
    expect(d.repos.decks.findId('inbox')).not.toBeNull();
    expect(d.repos.cards.srsState(card['question_id'] as number)).toMatchObject({ step: 0 });
  });

  it('refuses a blank side, an unknown deck and a trivia deck', () => {
    const d = deps();
    expect(study.authorCard(d, body({ prompt: '  ', answer: 'x' }))).toMatchObject({ status: 422 });
    expect(study.authorCard(d, body({ prompt: 'x', answer: 'y', deck_id: 999999 }))).toMatchObject({ status: 404 });
    const trivia = d.repos.decks.createTrivia('quiz', { topic: 't', intervalMinutes: 30 });
    expect(study.authorCard(d, body({ prompt: 'x', answer: 'y', deck_id: trivia }))).toMatchObject({ status: 400 });
  });
});

describe('the snooze', () => {
  it('resolves a preset, a custom span and the wake', async () => {
    const d = deps();
    const deck = d.repos.decks.create('capitals');
    const sid = await d.repos.sessions.create(deck, 'iPhone');
    const until = (b: unknown) => (study.sessionSnooze(d, sid, b) as { json: Record<string, unknown> }).json['snoozed_until'];
    expect(until({ preset: '1d' })).toBe('2026-03-15T15:00:00+00:00');
    expect(until({ custom: '3', unit: 'hours' })).toBe('2026-03-14T18:00:00+00:00');
    expect(until({ preset: 'wake' })).toBeNull();
    expect(study.sessionSnooze(d, sid, { preset: 'someday' })).toEqual({
      json: { error: { code: 'invalid_duration', message: "unknown preset 'someday'" } },
      status: 400,
    });
  });
});

describe('parseUntil', () => {
  it('maps every preset and refuses the rest', () => {
    expect(parseUntil({ preset: 'forever', now: TEST_NOW })).toBe(FOREVER_ISO);
    expect(parseUntil({ preset: '2w', now: TEST_NOW })).toBe('2026-03-28T15:00:00+00:00');
    expect(() => parseUntil({ preset: 'never', now: TEST_NOW })).toThrow(DurationError);
    expect(() => parseUntil({ custom: '0', unit: 'hours', now: TEST_NOW })).toThrow(/out of range/);
    expect(() => parseUntil({ custom: 'many', unit: 'hours', now: TEST_NOW })).toThrow(/must be an integer/);
    expect(() => parseUntil({ custom: '2', unit: 'fortnights', now: TEST_NOW })).toThrow(/unknown unit/);
    expect(() => parseUntil({ now: TEST_NOW })).toThrow(/missing preset/);
  });
});

describe('the chat handoff', () => {
  it('marks the picked and correct choices and closes with the ask', () => {
    const message = buildMessage({
      deckName: 'world-capitals',
      q: { type: 'mcq', prompt: 'Capital of Australia?', answer: 'Canberra', choices_list: ['Sydney', 'Canberra'] },
      userAnswer: 'Sydney',
      verdict: { result: 'wrong', feedback: 'Wrong choice.' },
      pickedSet: ['Sydney'],
      correctSet: ['Canberra'],
    });
    expect(message).toContain('- Sydney \u2190 my pick');
    expect(message).toContain('- Canberra \u2713 correct');
    expect(message).toContain('**Verdict:** wrong');
    expect(message.endsWith('\nPlease explain.')).toBe(true);
  });

  it('fences a code answer and says so when the card was skipped', () => {
    const code = buildMessage({ deckName: 'py', q: { type: 'code', prompt: 'p', answer: 'return x' }, userAnswer: 'pass' });
    expect(code).toContain('```\npass\n```');
    const idk = buildMessage({ deckName: 'py', q: { type: 'short', prompt: 'p', answer: 'a' }, idk: true });
    expect(idk).toContain("_(I don't know \u2014 skipped)_");
  });

  it('percent-encodes the whole message into every provider url', () => {
    const urls = providerUrls("a b/c?d=e&f'g");
    expect(quoteAll("a b/c?d=e&f'g")).toBe('a%20b%2Fc%3Fd%3De%26f%27g');
    expect(Object.keys(urls)).toEqual(['claude', 'chatgpt', 'perplexity']);
    expect(urls[DEFAULT_PROVIDER]).toBe('https://claude.ai/new?q=a%20b%2Fc%3Fd%3De%26f%27g');
  });
});
