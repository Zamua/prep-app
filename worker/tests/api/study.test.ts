// The study API's branches the corpus does not reach, and the two pure
// pieces it leans on: the chat handoff and the grading workflow id.
import { describe, expect, it } from 'vitest';
import { DurationError, FOREVER_ISO, parseUntil } from '../../app/durations.js';
import * as study from '../../app/study/api.js';
import { buildMessage, DEFAULT_PROVIDER, providerUrls, quoteAll } from '../../app/study/handoff.js';
import { RunnerUnavailable, type WorkflowRunner } from '../../app/ports.js';
import { cell, PARITY_NOW } from '../repos/setup.js';

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
    runner: opts.runner ?? refusing,
  };
}

const body = (json: unknown) => json;

describe('parseGradingWid', () => {
  it('walks from the right, so a hyphenated deck name survives', () => {
    expect(study.parseGradingWid('grade-world-capitals-q42-abcdef')).toEqual(['world-capitals', 42]);
    expect(study.parseGradingWid('grade-x-q1-a')).toEqual(['x', 1]);
  });

  it('refuses anything that is not a grading id', () => {
    for (const wid of ['not-a-grading-id', 'grade-x-1-a', 'grade-x-qz-a', 'grade-a-b', 'transform-x-q1-a']) {
      expect(study.parseGradingWid(wid), wid).toBeNull();
    }
  });
});

describe('GET /api/study/grading/{wid}', () => {
  it('refuses a malformed id before touching any row', () => {
    expect(study.gradingPoll(deps(), 'nope', '')).toEqual({
      json: { error: { code: 'malformed_workflow_id', message: 'workflow id is not a grading id' } },
      status: 400,
    });
  });

  it('answers 404 for a question the caller does not own, so a guessed id leaks nothing', () => {
    expect(study.gradingPoll(deps(), 'grade-x-q999999-abcdef', '')).toMatchObject({ status: 404 });
  });

  it('reports the failure when no job row backs the id, and releases the session', () => {
    const d = deps();
    const deck = d.repos.decks.create('capitals');
    const qid = d.repos.questions.add(deck, { type: 'short', prompt: 'q', answer: 'a' });
    expect(study.gradingPoll(d, `grade-capitals-q${qid}-abcdef`, '')).toEqual({
      json: { failed: { code: 'grading_failed', message: 'the grader returned nothing' } },
      status: 200,
    });
  });

  it('reports the tracked status while the job is still running', () => {
    const d = deps();
    const deck = d.repos.decks.create('capitals');
    const qid = d.repos.questions.add(deck, { type: 'short', prompt: 'q', answer: 'a' });
    const wid = `grade-capitals-q${qid}-abcdef`;
    d.repos.jobs.register({ workflowId: wid, workflowType: 'grading', deckId: null, deckName: 'capitals', urlPath: `/grading/${wid}`, initialStatus: 'grading' });
    expect(study.gradingPoll(d, wid, 'sid-1')).toEqual({
      json: { pending: { poll: `/api/study/grading/${wid}?sid=sid-1`, workflow_id: wid, status: 'grading' } },
      status: 200,
    });
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
    expect(parseUntil({ preset: 'forever', now: PARITY_NOW })).toBe(FOREVER_ISO);
    expect(parseUntil({ preset: '2w', now: PARITY_NOW })).toBe('2026-03-28T15:00:00+00:00');
    expect(() => parseUntil({ preset: 'never', now: PARITY_NOW })).toThrow(DurationError);
    expect(() => parseUntil({ custom: '0', unit: 'hours', now: PARITY_NOW })).toThrow(/out of range/);
    expect(() => parseUntil({ custom: 'many', unit: 'hours', now: PARITY_NOW })).toThrow(/must be an integer/);
    expect(() => parseUntil({ custom: '2', unit: 'fortnights', now: PARITY_NOW })).toThrow(/unknown unit/);
    expect(() => parseUntil({ now: PARITY_NOW })).toThrow(/missing preset/);
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
