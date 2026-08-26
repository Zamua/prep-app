import { describe, expect, it } from 'vitest';
import { expectedContext, renderedContext, seeded } from './setup.js';

describe('GET /deck/{name}', () => {
  it.each([
    ['world-capitals', '04-GET-deck-world-capitals'],
    ['world-history', '07-GET-deck-world-history'],
    ['scratch', '08-GET-deck-scratch'],
  ])('renders %s with the recorded context', async (name, file) => {
    const h = await seeded('reader');
    const res = await h.get(`/deck/${name}`);
    expect(res.status).toBe(200);
    expect(h.rendered()?.template).toBe('deck.html');
    expect(renderedContext(h)).toEqual(expectedContext('reader', file));
  });

  it('materializes an unknown deck rather than 404ing, as get-or-create does', async () => {
    const h = await seeded('reader');
    expect((await h.get('/deck/brand-new')).status).toBe(200);
    expect(h.rendered()?.context['deck_name']).toBe('brand-new');
    expect((await h.get('/')).status).toBe(200);
    expect(Object.keys(h.rendered()?.context['deck_display'] as object)).toContain('brand-new');
  });
});

describe('POST /deck/{name}/pin', () => {
  it('redirects back and flips the flag the deck page then shows', async () => {
    const h = await seeded('reader');
    const res = await h.post('/deck/world-capitals/pin', { pinned: 'on' });
    expect(res.status).toBe(303);
    expect(res.headers.get('location')).toBe('/deck/world-capitals');
    await h.get('/deck/world-capitals');
    expect(renderedContext(h)).toEqual(expectedContext('reader', '06-GET-deck-world-capitals@pinned'));
  });

  it('follows a same-origin Referer and ignores a cross-site one', async () => {
    const h = await seeded('reader');
    const same = await h.post('/deck/world-capitals/pin', { pinned: 'on' }, { headers: { referer: 'https://parity.example.test/?tab=decks' } });
    expect(same.headers.get('location')).toBe('/?tab=decks');
    const cross = await h.post('/deck/world-capitals/pin', { pinned: '' }, { headers: { referer: 'https://evil.example/steal' } });
    expect(cross.headers.get('location')).toBe('/deck/world-capitals');
  });

  it('swaps the pin fragment for an htmx caller instead of redirecting', async () => {
    const h = await seeded('reader');
    const res = await h.post('/deck/world-capitals/pin', { pinned: 'on' }, { headers: { 'hx-request': 'true' } });
    expect(res.status).toBe(200);
    expect(h.rendered()?.template).toBe('partials/pin_form.html');
    expect(h.rendered()?.context).toMatchObject({ deck_name: 'world-capitals', pinned: true });
  });
});

describe('the deck mutations', () => {
  it('renames the label and leaves the slug alone', async () => {
    const h = await seeded('reader');
    expect((await h.post('/deck/world-capitals/rename', { new_name: 'Capitals!' })).headers.get('location')).toBe('/deck/world-capitals');
    await h.get('/deck/world-capitals');
    expect((h.rendered()?.context['deck_meta'] as { display_name: string }).display_name).toBe('Capitals!');
  });

  it('refuses a delete whose confirmation matches neither label', async () => {
    const h = await seeded('reader');
    const res = await h.post('/deck/world-capitals/delete', { confirm: 'nope' });
    expect(res.status).toBe(400);
    expect(h.rendered()?.template).toBe('error.html');
    expect(String(h.rendered()?.context['blurb'])).toContain("deck name didn't match");
    expect((await h.get('/deck/world-capitals')).status).toBe(200);
  });

  it('accepts either the display name or the slug and sends the user home', async () => {
    const h = await seeded('reader');
    expect((await h.post('/deck/world-capitals/delete', { confirm: 'World Capitals' })).headers.get('location')).toBe('/');
    const h2 = await seeded('reader');
    expect((await h2.post('/deck/world-capitals/delete', { confirm: 'world-capitals' })).headers.get('location')).toBe('/');
  });

  it('takes a topic only on a trivia deck, and answers 204 to htmx', async () => {
    const h = await seeded('reader');
    expect((await h.post('/deck/world-capitals/topic', { context_prompt: 'x' })).status).toBe(400);
    expect((await h.post('/deck/world-history/topic', { context_prompt: '' })).status).toBe(400);
    const ok = await h.post('/deck/world-history/topic', { context_prompt: 'Ancient history' }, { headers: { 'hx-request': 'true' } });
    expect(ok.status).toBe(204);
    await h.get('/deck/world-history');
    expect((h.rendered()?.context['deck_meta'] as { context_prompt: string }).context_prompt).toBe('Ancient history');
  });

  it('takes retention only on an SRS deck, and only inside the FSRS bounds', async () => {
    const h = await seeded('reader');
    expect((await h.post('/deck/world-history/retention', { retention: '0.9' })).status).toBe(400);
    expect((await h.post('/deck/world-capitals/retention', { retention: '0.5' })).status).toBe(400);
    expect((await h.post('/deck/world-capitals/retention', { retention: 'wat' })).status).toBe(400);
    expect((await h.post('/deck/world-capitals/retention', { retention: '0.95' })).headers.get('location')).toBe('/deck/world-capitals');
    await h.get('/deck/world-capitals');
    expect(h.rendered()?.context['deck_retention']).toBe(0.95);
    await h.post('/deck/world-capitals/retention', { retention: 'default' });
    await h.get('/deck/world-capitals');
    expect(h.rendered()?.context['deck_retention']).toBeNull();
  });

  it('pausing notifications abandons the deck sessions that were running', async () => {
    const h = await seeded('reader');
    const begun = await h.post('/study/world-capitals/begin');
    const sid = begun.headers.get('location')!.replace('/session/', '');
    expect((await h.post('/deck/world-capitals/notifications', { enabled: '' })).status).toBe(303);
    expect((await h.get(`/session/${sid}`)).headers.get('location')).toBe('/deck/world-capitals');
  });

  it('404s a deck that is not there for every route but the view', async () => {
    const h = await seeded('empty');
    for (const path of ['/deck/ghost/split', '/deck/ghost/export', '/deck/ghost/edit-with-ai', '/deck/ghost/question/new']) {
      expect((await h.get(path)).status, path).toBe(404);
    }
  });
});

describe('GET/POST /deck/{name}/split', () => {
  it('moves the selected cards into a new deck', async () => {
    const h = await seeded('reader');
    expect((await h.get('/deck/world-capitals/split')).status).toBe(200);
    expect(h.rendered()?.context['form']).toEqual({ new_name: '', new_topic: '', selected_ids: [] });
    const res = await h.post('/deck/world-capitals/split', { new_name: 'africa', question_ids: ['2', '6'] });
    expect(res.headers.get('location')).toBe('/deck/africa');
    await h.get('/deck/africa');
    expect((h.rendered()?.context['questions'] as { id: number }[]).map((q) => q.id).sort()).toEqual([2, 6]);
  });

  it('re-renders the form with the refusal and keeps the typed input', async () => {
    const h = await seeded('reader');
    const res = await h.post('/deck/world-capitals/split', { new_name: 'africa' });
    expect(res.status).toBe(400);
    expect(h.rendered()?.template).toBe('deck_split.html');
    expect(h.rendered()?.context['error']).toBe('select at least one card to move');
    expect(h.rendered()?.context['form']).toEqual({ new_name: 'africa', new_topic: '', selected_ids: [] });
  });

  it('refuses a name that is taken', async () => {
    const h = await seeded('reader');
    const res = await h.post('/deck/world-capitals/split', { new_name: 'scratch', question_ids: ['2'] });
    expect(res.status).toBe(400);
    expect(h.rendered()?.context['error']).toBe('a deck named "scratch" already exists');
  });
});

describe('the deck side pages', () => {
  it('renders the AI edit page and keeps the legacy path as a redirect', async () => {
    const h = await seeded('reader');
    expect((await h.get('/deck/world-capitals/edit-with-ai')).status).toBe(200);
    expect(h.rendered()?.context).toMatchObject({ deck_name: 'world-capitals', deck_type: 'srs', error: null });
    const legacy = await h.get('/deck/world-capitals/edit-with-claude');
    expect(legacy.status).toBe(303);
    expect(legacy.headers.get('location')).toBe('/deck/world-capitals/edit-with-ai');
  });

  it('renders the export hub', async () => {
    const h = await seeded('reader');
    expect((await h.get('/deck/world-history/export')).status).toBe(200);
    expect(h.rendered()?.template).toBe('deck_export.html');
    expect(h.rendered()?.context).toMatchObject({ deck_name: 'world-history', deck_type: 'trivia' });
  });
});
