import { describe, expect, it } from 'vitest';
import type { PageRequest, PageResult } from '../../app/pageResult.js';
import type { AgentPort, WorkflowRunner } from '../../app/ports.js';
import { triviaAnswer, triviaCardRegrade, triviaSessionAnswer, triviaSessionRegrade, type TriviaDeps } from '../../app/trivia/pages.js';
import { cell, TEST_NOW } from '../repos/setup.js';
import { seeded, type Harness } from './setup.js';

const DECK = 'world-history';

/** The queue the server picked, from the redirect it answered with. */
async function openSession(h: Harness): Promise<string[]> {
  const res = await h.get(`/trivia/session/${DECK}`);
  expect(res.status).toBe(303);
  const location = res.headers.get('location')!;
  expect(location.startsWith(`/trivia/session/${DECK}?cards=`)).toBe(true);
  return new URL(location, 'https://prep.example.test').searchParams.get('cards')!.split(',');
}

describe('GET /trivia/session/{deck}', () => {
  it('picks a queue and redirects into it, then resumes the same queue', async () => {
    const h = await seeded('reader');
    const picked = await openSession(h);
    expect(picked.length).toBeGreaterThan(0);
    const again = await h.get(`/trivia/session/${DECK}`);
    expect(new URL(again.headers.get('location')!, 'https://x.test').searchParams.get('cards')).toBe(picked.join(','));
  });

  it('renders the head card with its position in the run', async () => {
    const h = await seeded('reader');
    const picked = await openSession(h);
    const res = await h.get(`/trivia/session/${DECK}?cards=${picked.join(',')}`);
    expect(res.status).toBe(200);
    expect(h.rendered()?.template).toBe('trivia/card.html');
    expect(h.rendered()?.context).toMatchObject({
      deck_name: DECK,
      result: null,
      session_position: 1,
      session_total: picked.length,
      session_remaining: picked.join(','),
      session_done: '',
    });
    expect((h.rendered()?.context['q'] as { id: number }).id).toBe(Number(picked[0]));
  });

  it('pops a card that is not in this deck instead of showing it', async () => {
    const h = await seeded('reader');
    const res = await h.get(`/trivia/session/${DECK}?cards=1,10&done=9r`);
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe(`/trivia/session/${DECK}?cards=10&done=9r`);
  });

  it('renders the summary on an empty queue and completes the session', async () => {
    const h = await seeded('reader');
    await openSession(h);
    const res = await h.get(`/trivia/session/${DECK}?cards=&done=10r,11w`);
    expect(res.status).toBe(200);
    expect(h.rendered()?.template).toBe('trivia/session_done.html');
    expect(h.rendered()?.context).toMatchObject({ deck_name: DECK, right_count: 1, total: 2 });
    expect((h.rendered()?.context['results'] as { id: number; verdict: string }[]).map((r) => r.verdict)).toEqual(['r', 'w']);
    expect(h.state.fake.rows('trivia_sessions').every((s) => s['status'] !== 'active')).toBe(true);
  });

  it('404s a deck that is not there', async () => {
    const h = await seeded('reader');
    expect((await h.get('/trivia/session/ghost')).status).toBe(404);
  });
});

describe('POST /trivia/session/{deck}/answer', () => {
  it('grades the head card, appends the verdict and shrinks the queue', async () => {
    const h = await seeded('reader');
    const picked = await openSession(h);
    const res = await h.post(`/trivia/session/${DECK}/answer`, { cards: picked.join(','), done: '', answer: 'The Roman Empire' });
    expect(res.status).toBe(200);
    const ctx = h.rendered()!.context;
    expect(ctx['session_remaining']).toBe(picked.slice(1).join(','));
    expect(String(ctx['session_done'])).toMatch(new RegExp(`^${picked[0]}[rw]$`));
    expect(ctx['result']).toMatchObject({ given: 'The Roman Empire', idk: false });
    expect(ctx['handoff_default_provider']).toBe('claude');
    expect(String(ctx['google_search_url']).startsWith('https://www.google.com/search?q=')).toBe(true);
  });

  it('records "I don\'t know" as wrong without grading anything', async () => {
    const h = await seeded('reader');
    const picked = await openSession(h);
    await h.post(`/trivia/session/${DECK}/answer`, { cards: picked.join(','), done: '', answer: '', idk: '1' });
    expect(h.rendered()?.context['result']).toMatchObject({ correct: false, given: '', idk: true, feedback: null });
    expect(h.rendered()?.context['session_done']).toBe(`${picked[0]}w`);
  });

  it('falls back to string similarity when no model can grade', async () => {
    const h = await seeded('reader');
    const picked = await openSession(h);
    // Magna Carta's answer is "1215": a numeric expected answer grades
    // deterministically, so no model is consulted at all.
    await h.post(`/trivia/session/${DECK}/answer`, { cards: '12', done: '', answer: '1215' });
    expect(h.rendered()?.context['result']).toMatchObject({ correct: true, feedback: null });
    expect(picked.length).toBeGreaterThan(0);
  });

  it('sends an empty queue back to the session start', async () => {
    const h = await seeded('reader');
    const res = await h.post(`/trivia/session/${DECK}/answer`, { cards: '', done: '10r' });
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe(`/trivia/session/${DECK}?cards=&done=10r`);
  });
});

describe('the session verdict disputes', () => {
  it('flips the recorded verdict and the done chain on an override', async () => {
    const h = await seeded('reader');
    await openSession(h);
    const res = await h.post(`/trivia/session/${DECK}/override`, { question_id: '11', cards: '12', done: '10r,11w', answer: 'Gutenberg' });
    expect(res.status).toBe(200);
    expect(h.rendered()?.context['session_done']).toBe('10r,11r');
    expect(h.rendered()?.context['result']).toMatchObject({ correct: true, overridden: true });
  });

  it('404s a question from another deck', async () => {
    const h = await seeded('reader');
    expect((await h.post(`/trivia/session/${DECK}/override`, { question_id: '1', cards: '', done: '' })).status).toBe(404);
  });

  it('regrades through the model and marks the answer as regraded', async () => {
    const h = await seeded('reader');
    const res = await h.post(`/trivia/session/${DECK}/regrade`, { question_id: '10', cards: '11', done: '10w', answer: 'Rome' });
    expect(res.status).toBe(200);
    expect(h.rendered()?.context['result']).toMatchObject({ regraded: true });
    // No model is reachable in this phase, so the fallback line is the verdict.
    expect(String((h.rendered()?.context['result'] as { feedback: string }).feedback)).toContain('graded by string similarity');
  });
});

describe('the standalone card', () => {
  it('renders with no result until an answer is posted', async () => {
    const h = await seeded('reader');
    expect((await h.get('/trivia/10')).status).toBe(200);
    expect(h.rendered()?.context).toMatchObject({ deck_name: DECK, result: null });
    expect((await h.get('/trivia/9999')).status).toBe(404);
  });

  it('answers, rotates the card and offers the explore links', async () => {
    const h = await seeded('reader');
    const res = await h.post('/trivia/10/answer', { answer: 'The Roman Empire' });
    expect(res.status).toBe(200);
    expect(h.rendered()?.context['result']).toMatchObject({ correct: true, given: 'The Roman Empire' });
    expect(h.rendered()?.context['handoff_urls']).toHaveProperty('chatgpt');
    expect(h.state.fake.rows('trivia_queue').find((r) => r['question_id'] === 10)?.['last_answered_correctly']).toBe(1);
  });

  it('overrides a verdict without touching the queue position', async () => {
    const h = await seeded('reader');
    const before = h.state.fake.rows('trivia_queue').find((r) => r['question_id'] === 11)?.['queue_position'];
    await h.post('/trivia/11/override', { answer: 'Gutenberg' });
    expect(h.rendered()?.context['result']).toMatchObject({ correct: true, overridden: true });
    const after = h.state.fake.rows('trivia_queue').find((r) => r['question_id'] === 11);
    expect(after?.['queue_position']).toBe(before);
    expect(after?.['last_answered_correctly']).toBe(1);
  });
});

describe('the per-deck trivia settings', () => {
  it('swaps the notif-edit fragment for htmx and redirects back otherwise', async () => {
    const h = await seeded('reader');
    const swap = await h.post('/trivia/decks/4/interval', { minutes: '60' }, { headers: { 'hx-request': 'true' } });
    expect(swap.status).toBe(200);
    expect(h.rendered()?.template).toBe('partials/notif_edit.html');
    expect(h.rendered()?.context['deck_meta']).toMatchObject({ deck_id: 4, interval_minutes: 60 });
    const plain = await h.post('/trivia/decks/4/session_size', { size: '5' });
    expect(plain.status).toBe(303);
    expect(plain.headers.get('location')).toBe(`/deck/${DECK}`);
  });

  it.each([
    ['/trivia/decks/4/interval', { minutes: 'soon' }],
    ['/trivia/decks/4/interval', { minutes: '0' }],
    ['/trivia/decks/4/session_size', { size: 'many' }],
    ['/trivia/decks/4/session_size', { size: '21' }],
  ])('refuses %s with %j', async (path, body) => {
    const h = await seeded('reader');
    expect((await h.post(path, body)).status).toBe(400);
  });

  it('mutes for a preset duration and unmutes again', async () => {
    const h = await seeded('reader');
    expect((await h.post('/trivia/decks/4/mute', { preset: '2h' })).headers.get('location')).toBe('/');
    expect(h.state.fake.rows('decks').find((d) => d['id'] === 4)?.['notifications_muted_until']).toBe('2026-03-14T17:00:00+00:00');
    expect((await h.post('/trivia/decks/4/unmute')).status).toBe(303);
    expect(h.state.fake.rows('decks').find((d) => d['id'] === 4)?.['notifications_muted_until']).toBeNull();
  });

  it('refuses an unknown preset and 404s an unknown deck', async () => {
    const h = await seeded('reader');
    expect((await h.post('/trivia/decks/4/mute', { preset: 'someday' })).status).toBe(400);
    expect((await h.post('/trivia/decks/999/unmute')).status).toBe(404);
  });

  it('abandons and snoozes the deck session from the continue strip', async () => {
    const h = await seeded('reader');
    await openSession(h);
    expect((await h.post(`/trivia/session/${DECK}/snooze`, { preset: '1h' })).headers.get('location')).toBe('/');
    expect(h.state.fake.rows('trivia_sessions').some((s) => s['snoozed_until'] === '2026-03-14T16:00:00+00:00')).toBe(true);
    await h.post(`/trivia/session/${DECK}/snooze`, { preset: 'wake' });
    expect(h.state.fake.rows('trivia_sessions').every((s) => s['snoozed_until'] === null)).toBe(true);
    expect((await h.post(`/trivia/session/${DECK}/abandon`)).headers.get('location')).toBe('/');
    expect(h.state.fake.rows('trivia_sessions').every((s) => s['status'] !== 'active')).toBe(true);
  });
});

/** Holds every grade until the test replies, so two grades of one card overlap. */
class HeldAgent implements AgentPort {
  readonly held: ((reply: string) => void)[] = [];
  complete(): Promise<string> {
    return new Promise((resolve) => this.held.push(resolve));
  }
}

const NO_RUNNER: WorkflowRunner = {
  start: async () => ({ workflowId: '' }),
  signal: async () => null,
  status: async () => null,
  terminate: async () => {},
};

const learned = (regex: string): string => JSON.stringify({ verdict: 'right', feedback: 'ok', regex_update: regex });

const pageReq = (params: Record<string, string>, form: Record<string, string>): PageRequest => ({
  params,
  query: new URLSearchParams(),
  form: new URLSearchParams(form),
  htmx: false,
  hxHeader: null,
  userAgent: null,
  cookies: {},
  now: TEST_NOW.toISOString(),
});

type Grade = (deps: TriviaDeps, qid: number, answer: string) => Promise<PageResult>;

const GRADES: [string, Grade][] = [
  ['a session answer', (d, qid, answer) => triviaSessionAnswer(pageReq({ deck_name: 'history' }, { cards: String(qid), done: '', answer }), d)],
  ['a session regrade', (d, qid, answer) => triviaSessionRegrade(pageReq({ deck_name: 'history' }, { question_id: String(qid), cards: '', done: `${qid}w`, answer }), d)],
  ['a card answer', (d, qid, answer) => triviaAnswer(pageReq({ question_id: String(qid) }, { answer }), d)],
  ['a card regrade', (d, qid, answer) => triviaCardRegrade(pageReq({ question_id: String(qid) }, { answer }), d)],
];

describe('the learned answer regex', () => {
  it.each(GRADES)('%s keeps an alternative another grade of the card learned while it waited on the model', async (_name, grade) => {
    const c = cell();
    const deck = c.repos.decks.createTrivia('history', { topic: 'History', intervalMinutes: 60, displayName: 'History' });
    const qid = c.repos.questions.add(deck, { type: 'short', prompt: 'Where did William win in 1066?', answer: 'Battle of Hastings in England' });
    c.repos.trivia.appendCard(qid, deck);
    const agent = new HeldAgent();
    const deps: TriviaDeps = { repos: c.repos, agent, runner: NO_RUNNER, freeTierConfigured: false };

    const first = grade(deps, qid, 'Hastings');
    const second = grade(deps, qid, 'Hastings fight');
    expect(agent.held).toHaveLength(2);
    agent.held[0]!(learned('battle of hastings in england|hastings'));
    const firstResult = await first;
    agent.held[1]!(learned('battle of hastings in england|hastings fight'));
    const secondResult = await second;

    expect(c.repos.questions.get(qid)!.answer_regex).toBe('battle of hastings in england|hastings');
    expect(firstResult).toMatchObject({ context: { result: { regex_updated: true } } });
    expect(secondResult).toMatchObject({ context: { result: { regex_updated: false } } });
  });
});
