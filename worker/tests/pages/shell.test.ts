import { describe, expect, it } from 'vitest';
import { seeded } from './setup.js';

describe('POST /study/{name}/begin', () => {
  it('resumes an active session and starts a fresh one on demand', async () => {
    const h = await seeded('reader');
    const first = await h.post('/study/world-capitals/begin');
    expect(first.status).toBe(303);
    const sid = first.headers.get('location')!.replace('/session/', '');
    expect((await h.post('/study/world-capitals/begin')).headers.get('location')).toBe(`/session/${sid}`);
    const fresh = await h.post('/study/world-capitals/begin?fresh=1');
    expect(fresh.headers.get('location')).not.toBe(`/session/${sid}`);
    expect(h.state.fake.rows('study_sessions').find((s) => s['id'] === sid)?.['status']).toBe('abandoned');
  });

  it('records the device the session was opened on', async () => {
    const h = await seeded('reader');
    await h.post('/study/world-capitals/begin', {}, { headers: { 'user-agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0)' } });
    expect(h.state.fake.rows('study_sessions').at(-1)?.['device_label']).toBe('iPhone');
  });

  it('refuses a trivia deck, which has no SRS state to study', async () => {
    const h = await seeded('reader');
    const res = await h.post('/study/world-history/begin');
    expect(res.status).toBe(400);
    expect(String(h.rendered()?.context['blurb'])).toContain('trivia decks are notification-driven');
  });
});

describe('GET /session/{sid}', () => {
  it('renders the shell for an active session', async () => {
    const h = await seeded('reader');
    const sid = (await h.post('/study/world-capitals/begin')).headers.get('location')!.replace('/session/', '');
    expect((await h.get(`/session/${sid}`)).status).toBe(200);
    expect(h.rendered()?.template).toBe('study_shell.html');
    expect(h.rendered()?.context).toMatchObject({ deck_name: 'world-capitals', session_id: sid, sign_in_url: '' });
  });

  it('redirects an abandoned session to its deck and 404s an unknown one', async () => {
    const h = await seeded('reader');
    const sid = (await h.post('/study/world-capitals/begin')).headers.get('location')!.replace('/session/', '');
    expect((await h.post(`/session/${sid}/abandon`)).headers.get('location')).toBe('/deck/world-capitals');
    expect((await h.get(`/session/${sid}`)).headers.get('location')).toBe('/deck/world-capitals');
    expect((await h.get('/session/deadbeefdeadbeef')).status).toBe(404);
  });

  it('snoozes a session out of the continue strip and wakes it again', async () => {
    const h = await seeded('reader');
    const sid = (await h.post('/study/world-capitals/begin')).headers.get('location')!.replace('/session/', '');
    expect((await h.post(`/session/${sid}/snooze`, { preset: 'tomorrow' })).headers.get('location')).toBe('/');
    expect(h.state.fake.rows('study_sessions').find((s) => s['id'] === sid)?.['snoozed_until']).toBe('2026-03-15T08:00:00+00:00');
    await h.get('/');
    expect((h.rendered()?.context['snoozed_sessions'] as { id: string }[]).some((s) => s.id === sid)).toBe(true);
    await h.post(`/session/${sid}/snooze`, { preset: 'wake' });
    expect(h.state.fake.rows('study_sessions').find((s) => s['id'] === sid)?.['snoozed_until']).toBeNull();
  });

  it('refuses a duration it cannot parse', async () => {
    const h = await seeded('reader');
    const sid = (await h.post('/study/world-capitals/begin')).headers.get('location')!.replace('/session/', '');
    expect((await h.post(`/session/${sid}/snooze`, { custom: '0', unit: 'hours' })).status).toBe(400);
    expect((await h.post(`/session/${sid}/snooze`, { custom: '2', unit: 'fortnights' })).status).toBe(400);
  });
});

describe('GET /study/{name}', () => {
  it('renders the sessionless shell and materializes the deck', async () => {
    const h = await seeded('empty');
    expect((await h.get('/study/fresh-deck')).status).toBe(200);
    expect(h.rendered()?.context).toMatchObject({ deck_name: 'fresh-deck', session_id: null });
    expect(h.state.fake.rows('decks').map((d) => d['name'])).toEqual(['fresh-deck']);
  });
});

describe('GET /grading/{wid}', () => {
  it('sends the browser to the session when one is named, else to the deck', async () => {
    const h = await seeded('reader');
    const sid = (await h.post('/study/world-capitals/begin')).headers.get('location')!.replace('/session/', '');
    expect((await h.get('/grading/grade-world-capitals-q2-abc123')).headers.get('location')).toBe('/study/world-capitals');
    expect((await h.get(`/grading/grade-session-q2-${sid}v1?sid=${sid}`)).headers.get('location')).toBe(`/session/${sid}`);
  });

  it('refuses a crafted id rather than minting a deck through get-or-create', async () => {
    const h = await seeded('reader');
    expect((await h.get('/grading/nonsense')).status).toBe(400);
    expect((await h.get('/grading/grade-invented-deck-q2-abc123')).status).toBe(404);
    expect((await h.get('/grading/grade-world-capitals-q9999-abc123')).status).toBe(404);
    expect(h.state.fake.rows('decks').map((d) => d['name'])).not.toContain('invented-deck');
  });
});
