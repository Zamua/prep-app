import { describe, expect, it } from 'vitest';
import { RETIRED_PROVIDER } from '../../app/settings/providers.js';
import { expectedContext, renderedContext, seeded } from './setup.js';

describe('the recorded settings pages', () => {
  it.each([
    ['/settings/srs', 'settings_srs.html', '17-GET-settings-srs'],
    ['/settings/editor', 'settings_editor.html', '18-GET-settings-editor'],
    ['/settings/api', 'settings_api.html', '19-GET-settings-api'],
    ['/notify/log', 'notify/log.html', '21-GET-notify-log'],
  ])('renders %s as recorded', async (path, template, file) => {
    const h = await seeded('reader');
    expect((await h.get(path)).status).toBe(200);
    expect(h.rendered()?.template).toBe(template);
    expect(renderedContext(h)).toEqual(expectedContext('reader', file));
  });

  it('renders /notify as recorded once the VAPID key is filled in', async () => {
    const h = await seeded('reader', { PREP_VAPID_PUBLIC_KEY: 'BSeedPublicKey' });
    expect((await h.get('/notify')).status).toBe(200);
    expect(renderedContext(h)).toEqual({ ...expectedContext('reader', '20-GET-notify'), vapid_key: 'BSeedPublicKey' });
  });

  it('marks the log seen, so the badge on that very page reads zero', async () => {
    const h = await seeded('reader');
    await h.get('/');
    expect(h.rendered()?.context['notif_unseen_count']).toBe(2);
    await h.get('/notify/log');
    expect(h.rendered()?.context['notif_unseen_count']).toBe(0);
    await h.get('/');
    expect(h.rendered()?.context['notif_unseen_count']).toBe(0);
  });
});

describe('/settings/srs', () => {
  it('saves a preset and reports it as no longer the default', async () => {
    const h = await seeded('reader');
    expect((await h.post('/settings/srs', { retention: '0.95' })).status).toBe(200);
    expect(h.rendered()?.context).toMatchObject({ current: 0.95, is_default: false, saved: true });
    await h.get('/settings/srs');
    expect(h.rendered()?.context).toMatchObject({ current: 0.95, is_default: false, saved: false });
  });

  it.each([
    ['', 'retention must be a number'],
    ['none', 'retention must be a number'],
    ['0.5', 'retention must be between 70% and 97%'],
    ['1.0', 'retention must be between 70% and 97%'],
  ])('refuses %j', async (value, message) => {
    const h = await seeded('reader');
    const res = await h.post('/settings/srs', { retention: value });
    expect(res.status).toBe(400);
    expect(String(h.rendered()?.context['blurb'])).toContain(message);
  });
});

describe('/settings/editor', () => {
  it('saves a mode and shows it on the same render', async () => {
    const h = await seeded('reader');
    expect((await h.post('/settings/editor', { mode: 'vim' })).status).toBe(200);
    expect(h.rendered()?.context).toMatchObject({ current_mode: 'vim', saved: true });
    expect((h.rendered()?.context['user'] as { editor_input_mode: string }).editor_input_mode).toBe('vim');
  });

  it('refuses an unknown mode', async () => {
    const h = await seeded('reader');
    expect((await h.post('/settings/editor', { mode: 'acme' })).status).toBe(400);
    expect(String(h.rendered()?.context['blurb'])).toContain('Unknown input mode "acme".');
  });
});

describe('/settings/api', () => {
  it('mints a token, shows the plaintext once, and never again', async () => {
    const h = await seeded('reader');
    expect((await h.post('/settings/api/tokens', { label: 'laptop' })).status).toBe(200);
    const plaintext = String(h.rendered()?.context['created_plaintext']);
    expect(plaintext).toMatch(/^prep_pat_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
    const listed = h.rendered()?.context['tokens'] as { label: string; key_prefix: string }[];
    expect(listed.some((t) => t.label === 'laptop')).toBe(true);
    expect(listed.every((t) => !JSON.stringify(t).includes(plaintext.split('.')[1]!))).toBe(true);
    await h.get('/settings/api');
    expect(h.rendered()?.context['created_plaintext']).toBeNull();
  });

  it('revokes with an empty 200 for htmx and a flash otherwise', async () => {
    const h = await seeded('reader');
    const swap = await h.post('/settings/api/tokens/1/delete', {}, { headers: { 'hx-request': 'true' } });
    expect(swap.status).toBe(200);
    expect(await swap.text()).toBe('');
    expect(swap.headers.get('content-type')).toBe('text/html; charset=utf-8');
    const h2 = await seeded('reader');
    expect((await h2.post('/settings/api/tokens/1/delete')).status).toBe(200);
    expect(h2.rendered()?.context['flash']).toBe('Token revoked.');
    expect(h2.rendered()?.context['tokens']).toEqual([]);
  });
});

describe('/settings/agent', () => {
  it('offers the three API-key providers and reports the free tier', async () => {
    const h = await seeded('reader');
    expect((await h.get('/settings/agent')).status).toBe(200);
    const ctx = h.rendered()!.context;
    expect(ctx['free_tier_configured']).toBe(true);
    expect((ctx['byok_sections'] as { provider: string }[]).map((s) => s.provider)).toEqual(['anthropic-api', 'openai-api', 'openrouter-api']);
    expect(ctx['byok_sections']).toEqual((ctx['byok_sections'] as { metadata: null }[]).map((s) => ({ ...s, metadata: null })));
  });

  it('reports the free tier off when the deploy has none', async () => {
    const h = await seeded('reader', { PREP_FREE_INFERENCE_BASE_URL: '', PREP_FREE_INFERENCE_API_KEY: '', PREP_FREE_INFERENCE_MODEL: '' });
    await h.get('/settings/agent');
    expect(h.rendered()?.context['free_tier_configured']).toBe(false);
    expect(h.rendered()?.context['agent_available']).toBe(false);
  });

  it('stores a key, marks it active and masks it', async () => {
    const h = await seeded('reader', { PREP_KEY_ENCRYPTION_SECRET: 'a'.repeat(64) });
    const res = await h.post('/settings/agent/byok/anthropic-api/connect', { api_key: `sk-ant-api03-${'p'.repeat(40)}tyAA` });
    expect(res.status).toBe(200);
    expect(h.rendered()?.context['byok_flash']).toBe('Your Anthropic key is saved. AI features now use your account.');
    const section = (h.rendered()?.context['byok_sections'] as { provider: string; is_active: boolean; metadata: { key_prefix: string } | null }[])[0]!;
    expect(section).toMatchObject({ provider: 'anthropic-api', is_active: true });
    expect(section.metadata?.key_prefix).toBe('sk-ant-api03-p…tyAA');
    expect(h.state.fake.rows('byok_credentials')[0]?.['ciphertext']).not.toContain('sk-ant');
  });

  it('refuses an empty key, a wrong prefix and a deploy with no master key', async () => {
    const h = await seeded('reader', { PREP_KEY_ENCRYPTION_SECRET: 'a'.repeat(64) });
    expect((await h.post('/settings/agent/byok/anthropic-api/connect', { api_key: '' })).status).toBe(400);
    expect(h.rendered()?.context['byok_error']).toBe('API key is required.');
    expect((await h.post('/settings/agent/byok/anthropic-api/connect', { api_key: 'sk-nope' })).status).toBe(400);
    expect(String(h.rendered()?.context['byok_error'])).toContain("expected one starting with 'sk-ant-api03-'");
    const noKey = await seeded('reader', { PREP_KEY_ENCRYPTION_SECRET: '' });
    expect((await noKey.post('/settings/agent/byok/anthropic-api/connect', { api_key: `sk-ant-api03-${'p'.repeat(20)}` })).status).toBe(503);
  });

  it('404s an unknown provider slug rather than saying what exists', async () => {
    const h = await seeded('reader');
    expect((await h.post('/settings/agent/byok/acme/connect', { api_key: 'x' })).status).toBe(404);
    expect((await h.post(`/settings/agent/byok/${RETIRED_PROVIDER}/connect`, { api_key: 'x' })).status).toBe(404);
  });

  it('clears a stale active choice on the next render', async () => {
    const h = await seeded('reader', { PREP_KEY_ENCRYPTION_SECRET: 'a'.repeat(64) });
    await h.post('/settings/agent/byok/anthropic-api/connect', { api_key: `sk-ant-api03-${'p'.repeat(20)}` });
    await h.post('/settings/agent/byok/anthropic-api/use');
    expect(h.rendered()?.context['byok_flash']).toBe('Anthropic is now your active provider.');
    await h.post('/settings/agent/byok/anthropic-api/disconnect');
    expect(h.rendered()?.context['byok_flash']).toBe('API key removed.');
    expect(h.state.fake.rows('profile')[0]?.['active_byok_provider']).toBeNull();
  });

  it('refuses to make a provider active before it has a key', async () => {
    const h = await seeded('reader');
    expect((await h.post('/settings/agent/byok/openai-api/use')).status).toBe(400);
    expect(h.rendered()?.context['byok_error']).toBe('Add a OpenAI key before making it active.');
  });

  it('offers a migrated subscription row for deletion and nothing else', async () => {
    const h = await seeded('reader');
    h.state.fake.sql.exec(
      "INSERT INTO byok_credentials (provider, ciphertext, key_prefix, created_at) VALUES (?, 'x', 'sk-ant-oat01-a…bbbb', '2026-03-01T00:00:00+00:00')",
      RETIRED_PROVIDER,
    );
    await h.get('/settings/agent');
    const sections = h.rendered()?.context['byok_sections'] as { provider: string; retired?: true; is_active: boolean }[];
    expect(sections.map((s) => s.provider)).toEqual(['anthropic-api', 'openai-api', 'openrouter-api', RETIRED_PROVIDER]);
    expect(sections.at(-1)).toMatchObject({ retired: true, is_active: false });
    expect((await h.post(`/settings/agent/byok/${RETIRED_PROVIDER}/disconnect`)).status).toBe(200);
    expect(h.state.fake.rows('byok_credentials')).toEqual([]);
  });
});

describe('/settings/account', () => {
  it('404s on a deploy whose identity has no upstream account', async () => {
    const h = await seeded('reader');
    expect((await h.get('/settings/account')).status).toBe(404);
    expect((await h.post('/settings/account/delete', { confirm: 'seed@example.com' })).status).toBe(404);
  });
});
