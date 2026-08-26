import { describe, expect, it } from 'vitest';
import { expectedContext, renderedContext, seeded } from './setup.js';

const NO_FREE_TIER = { PREP_FREE_INFERENCE_BASE_URL: '', PREP_FREE_INFERENCE_API_KEY: '', PREP_FREE_INFERENCE_MODEL: '' };

describe('the new-deck forms', () => {
  it.each([
    ['/decks/new', 'deck_new_chooser.html', '09-GET-decks-new'],
    ['/decks/new/srs', 'deck_new_srs.html', '10-GET-decks-new-srs'],
    ['/decks/new/trivia', 'deck_new_trivia.html', '11-GET-decks-new-trivia'],
  ])('renders %s as recorded', async (path, template, file) => {
    const h = await seeded('reader');
    expect((await h.get(path)).status).toBe(200);
    expect(h.rendered()?.template).toBe(template);
    expect(renderedContext(h)).toEqual(expectedContext('reader', file));
  });
});

describe('POST /decks/new/srs', () => {
  it('creates an empty deck under an opaque slug and redirects to it', async () => {
    const h = await seeded('empty');
    const res = await h.post('/decks/new/srs', { name: 'My Deck', context_prompt: 'about things', action: 'empty' });
    expect(res.status).toBe(303);
    const slug = res.headers.get('location')!.replace('/deck/', '');
    expect(slug).toMatch(/^[a-z2-9]{8}$/);
    const row = h.state.fake.rows('decks')[0]!;
    expect(row).toMatchObject({ name: slug, display_name: 'My Deck', context_prompt: 'about things' });
  });

  it('re-renders with the refusal and the typed values', async () => {
    const h = await seeded('empty');
    for (const [body, message] of [
      [{ name: '  ' }, 'Deck name is required.'],
      [{ name: 'a\nb' }, "Deck name can't contain newlines."],
      [{ name: 'x'.repeat(61) }, 'Deck name is too long (61 chars; max 60).'],
    ] as const) {
      const res = await h.post('/decks/new/srs', body as Record<string, string>);
      expect(res.status).toBe(400);
      expect(h.rendered()?.template).toBe('deck_new_srs.html');
      expect(h.rendered()?.context['error']).toBe(message);
    }
    expect(h.state.fake.rows('decks')).toEqual([]);
  });

  it('refuses an over-long description before creating anything', async () => {
    const h = await seeded('empty');
    const res = await h.post('/decks/new/srs', { name: 'ok', context_prompt: 'x'.repeat(8001) });
    expect(res.status).toBe(400);
    expect(h.rendered()?.context['error']).toBe('Description is too long (8001 chars; max 8000).');
    expect(h.state.fake.rows('decks')).toEqual([]);
  });

  it('refuses plan-and-generate without an agent, and without a description', async () => {
    const noAgent = await seeded('empty', NO_FREE_TIER);
    const refused = await noAgent.post('/decks/new/srs', { name: 'ok', context_prompt: 'about things', action: 'plan' });
    expect(refused.status).toBe(400);
    expect(String(noAgent.rendered()?.context['error'])).toContain('Plan & generate needs an AI agent');
    const h = await seeded('empty');
    expect((await h.post('/decks/new/srs', { name: 'ok', action: 'plan' })).status).toBe(400);
    expect(h.rendered()?.context['error']).toBe('Plan & generate needs a description for the AI to plan against.');
  });

  it('keeps the deck when the workflow will not start', async () => {
    const h = await seeded('empty');
    const res = await h.post('/decks/new/srs', { name: 'ok', context_prompt: 'about things', action: 'plan' });
    expect(res.status).toBe(500);
    expect(String(h.rendered()?.context['blurb'])).toContain('deck created but failed to start plan workflow');
    expect(h.state.fake.rows('decks')).toHaveLength(1);
  });
});

describe('POST /decks/new/trivia', () => {
  it('lands on the deck page when no tier funds a generation', async () => {
    const h = await seeded('empty', NO_FREE_TIER);
    const res = await h.post('/decks/new/trivia', { name: 'Quiz', topic: 'world history', notification_interval_minutes: '45' });
    expect(res.status).toBe(303);
    const slug = res.headers.get('location')!.replace('/deck/', '');
    expect(h.state.fake.rows('decks')[0]).toMatchObject({
      name: slug,
      display_name: 'Quiz',
      deck_type: 'trivia',
      context_prompt: 'world history',
      notification_interval_minutes: 45,
    });
  });

  it('keeps the deck when the generation workflow will not start', async () => {
    const h = await seeded('empty');
    const res = await h.post('/decks/new/trivia', { name: 'Quiz', topic: 'world history' });
    expect(res.status).toBe(500);
    expect(String(h.rendered()?.context['blurb'])).toContain('deck created but failed to start trivia workflow');
    expect(h.state.fake.rows('decks')).toHaveLength(1);
  });

  it.each([
    [{ name: 'Quiz', topic: '' }, 'Topic is required — it describes what the deck is about, and the AI uses it later if you configure one.'],
    [{ name: 'Quiz', topic: 'x'.repeat(8001) }, 'Topic is too long (8001 chars; max 8000).'],
    [{ name: 'Quiz', topic: 't', notification_interval_minutes: 'soon' }, 'Notification interval must be an integer.'],
    [{ name: 'Quiz', topic: 't', notification_interval_minutes: '0' }, 'Notification interval must be 1–720 minutes.'],
    [{ name: 'Quiz', topic: 't', notification_interval_minutes: '721' }, 'Notification interval must be 1–720 minutes.'],
  ])('refuses %j', async (body, message) => {
    const h = await seeded('empty');
    const res = await h.post('/decks/new/trivia', body);
    expect(res.status).toBe(400);
    expect(h.rendered()?.context['error']).toBe(message);
    expect(h.state.fake.rows('decks')).toEqual([]);
  });

  it('puts the default interval back into the form when the typed one is garbage', async () => {
    const h = await seeded('empty');
    await h.post('/decks/new/trivia', { name: 'Quiz', topic: 't', notification_interval_minutes: 'soon' });
    expect(h.rendered()?.context['interval_value']).toBe(30);
  });
});
