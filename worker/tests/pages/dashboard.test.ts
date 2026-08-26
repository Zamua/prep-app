import { describe, expect, it } from 'vitest';
import { expectedContext, renderedContext, seeded } from './setup.js';

describe('GET /', () => {
  it.each(['reader', 'empty'])('renders index.html with the recorded context for %s', async (profile) => {
    const h = await seeded(profile);
    const res = await h.get('/');
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('text/html; charset=utf-8');
    expect(h.rendered()?.template).toBe('index.html');
    expect(renderedContext(h)).toEqual(expectedContext(profile, '01-GET-root'));
  });

  it('is refused for an anonymous identity, whose home page is the landing', async () => {
    const h = await seeded('reader');
    const res = await h.get('/', { headers: { 'x-prep-kind': 'anon' } });
    expect(res.status).toBe(401);
  });
});
