import { describe, expect, it } from 'vitest';
import { expectedContext, renderedContext, seeded } from './setup.js';

describe('the question forms', () => {
  it('renders the empty new-question form as recorded', async () => {
    const h = await seeded('reader');
    expect((await h.get('/deck/world-capitals/question/new')).status).toBe(200);
    expect(h.rendered()?.template).toBe('question_new.html');
    expect(renderedContext(h)).toEqual(expectedContext('reader', '12-GET-deck-world-capitals-question-new'));
  });

  it('renders the edit form for a code card as recorded', async () => {
    const h = await seeded('reader');
    expect((await h.get('/question/4/edit')).status).toBe(200);
    expect(h.rendered()?.template).toBe('question_edit.html');
    expect(renderedContext(h)).toEqual(expectedContext('reader', '13-GET-question-4-edit'));
  });

  it('unwraps a multi answer into lines and re-joins the choices', async () => {
    const h = await seeded('reader');
    await h.get('/question/3/edit');
    expect(h.rendered()?.context['form']).toMatchObject({
      type: 'multi',
      answer: 'Ottawa\nLima',
      choices: 'Ottawa\nToronto\nLima\nRio de Janeiro',
    });
  });
});

describe('POST /deck/{name}/question/new', () => {
  it('adds the card and returns to the deck', async () => {
    const h = await seeded('reader');
    const res = await h.post('/deck/world-capitals/question/new', { type: 'short', prompt: 'Capital of Chile?', answer: 'Santiago' });
    expect(res.headers.get('location')).toBe('/deck/world-capitals');
    await h.get('/deck/world-capitals');
    expect((h.rendered()?.context['questions'] as { prompt: string }[]).some((q) => q.prompt === 'Capital of Chile?')).toBe(true);
  });

  it('puts a card added to a trivia deck into the rotation, not the SRS schedule', async () => {
    const h = await seeded('reader');
    await h.post('/deck/world-history/question/new', { type: 'short', prompt: 'Who was Hammurabi?', answer: 'A Babylonian king' });
    const before = h.state.fake.rows('trivia_queue').length;
    expect(before).toBeGreaterThan(0);
    expect(h.state.fake.rows('trivia_queue').some((r) => r['question_id'] === h.state.fake.rows('questions').at(-1)?.['id'])).toBe(true);
  });

  it('re-renders with the first refusal and every typed field', async () => {
    const h = await seeded('reader');
    const res = await h.post('/deck/world-capitals/question/new', { type: 'mcq', prompt: 'Pick one', answer: 'a', choices: '  \n ' });
    expect(res.status).toBe(400);
    expect(h.rendered()?.context['error']).toBe('MCQ questions need at least one choice (one per line).');
    expect(h.rendered()?.context['form']).toEqual({
      type: 'mcq',
      prompt: 'Pick one',
      answer: 'a',
      topic: '',
      skeleton: '',
      language: '',
      rubric: '',
      answer_regex: '',
      choices: '',
    });
  });

  it.each([
    [{ type: 'nope', prompt: 'p', answer: 'a' }, 'Type must be one of: code, mcq, multi, short.'],
    [{ type: 'short', prompt: '', answer: 'a' }, 'Prompt is required.'],
    [{ type: 'short', prompt: 'p', answer: '' }, 'Answer is required.'],
    [{ type: 'code', prompt: 'p', answer: 'a' }, 'Code questions need a language.'],
  ])('refuses %j', async (body, message) => {
    const h = await seeded('reader');
    expect((await h.post('/deck/world-capitals/question/new', body)).status).toBe(400);
    expect(h.rendered()?.context['error']).toBe(message);
  });

  it('stores a multi answer as its JSON, from a list or from lines', async () => {
    const h = await seeded('reader');
    await h.post('/deck/world-capitals/question/new', { type: 'multi', prompt: 'Which?', answer: 'Ottawa\nLima', choices: 'Ottawa\nLima' });
    const fromLines = h.state.fake.rows('questions').at(-1)?.['answer'];
    expect(fromLines).toBe('["Ottawa", "Lima"]');
    await h.post('/deck/world-capitals/question/new', { type: 'multi', prompt: 'Which two?', answer: '["Ottawa", "Lima"]', choices: 'Ottawa\nLima' });
    expect(h.state.fake.rows('questions').at(-1)?.['answer']).toBe('["Ottawa", "Lima"]');
  });
});

describe('POST /question/{qid}/edit', () => {
  it('keeps the SRS state across an edit', async () => {
    const h = await seeded('reader');
    const before = h.state.fake.rows('cards').find((c) => c['question_id'] === 2);
    const res = await h.post('/question/2/edit', { type: 'short', prompt: 'Capital of Kenya, again?', answer: 'Nairobi' });
    expect(res.headers.get('location')).toBe('/deck/world-capitals');
    expect(h.state.fake.rows('cards').find((c) => c['question_id'] === 2)).toEqual(before);
  });

  it('re-renders with the stored question when the form is refused', async () => {
    const h = await seeded('reader');
    const res = await h.post('/question/2/edit', { type: 'short', prompt: '', answer: 'x' });
    expect(res.status).toBe(400);
    expect(h.rendered()?.template).toBe('question_edit.html');
    expect((h.rendered()?.context['q'] as { id: number }).id).toBe(2);
    expect(h.rendered()?.context['error']).toBe('Prompt is required.');
  });

  it('404s an unknown question', async () => {
    const h = await seeded('reader');
    expect((await h.get('/question/9999/edit')).status).toBe(404);
  });
});

describe('suspend and unsuspend', () => {
  it('answers 204 to htmx and 303 to the deck otherwise', async () => {
    const h = await seeded('reader');
    expect((await h.post('/question/2/suspend', {}, { headers: { 'hx-request': 'true' } })).status).toBe(204);
    await h.get('/deck/world-capitals');
    expect((h.rendered()?.context['questions'] as { id: number; suspended: boolean }[]).find((q) => q.id === 2)?.suspended).toBe(true);
    const res = await h.post('/question/2/unsuspend');
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe('/deck/world-capitals');
  });
});

describe('POST /question/{qid}/improve', () => {
  it('refuses an empty prompt and an unknown question before the runner', async () => {
    const h = await seeded('reader');
    expect((await h.post('/question/2/improve', { prompt: '  ' })).status).toBe(400);
    expect((await h.post('/question/9999/improve', { prompt: 'tighten it' })).status).toBe(404);
  });

  it('403s when no tier funds the work, 500s when the runner cannot start', async () => {
    const noAgent = await seeded('reader', { PREP_FREE_INFERENCE_BASE_URL: '', PREP_FREE_INFERENCE_API_KEY: '', PREP_FREE_INFERENCE_MODEL: '' });
    expect((await noAgent.post('/question/2/improve', { prompt: 'tighten it' })).status).toBe(403);
    const h = await seeded('reader');
    const res = await h.post('/question/2/improve', { prompt: 'tighten it' });
    expect(res.status).toBe(500);
    expect(String(h.rendered()?.context['blurb'])).toContain('failed to start transform');
  });
});
