import { describe, expect, it } from 'vitest';
import { expectedContext, harness, renderedContext, seeded } from './setup.js';

const ANON = 'anon:' + 'ab'.repeat(16);

describe('GET /', () => {
  it.each(['reader', 'empty'])('renders index.html with the recorded context for %s', async (profile) => {
    const h = await seeded(profile);
    const res = await h.get('/');
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('text/html; charset=utf-8');
    expect(h.rendered()?.template).toBe('index.html');
    expect(renderedContext(h)).toEqual(expectedContext(profile, '01-GET-root'));
  });

  // The instant-start product turns on this: an anonymous account's home is
  // the dashboard, not the landing page a visitor sees.
  it('renders for an anonymous account', async () => {
    const h = harness();
    await h.cell.createInstantDeck({
      displayName: 'Capitals',
      cards: [{ prompt: 'Capital of France?', answer: 'Paris', answer_regex: null }],
      mint: { id: ANON, displayName: 'Guest', idx: 7 },
      at: '2026-03-14T15:00:00+00:00',
    });
    const res = await h.get('/', { headers: { 'x-prep-subject': ANON, 'x-prep-kind': 'anon' } });
    expect(res.status).toBe(200);
    expect(h.rendered()?.template).toBe('index.html');
  });

  it('refuses an anonymous identity naming a cell that is not anonymous', async () => {
    const h = await seeded('reader');
    const res = await h.get('/', { headers: { 'x-prep-kind': 'anon' } });
    expect(res.status).toBe(401);
  });
});
