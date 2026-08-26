import { describe, expect, it } from 'vitest';
import { PKCE_COOKIE } from '../../app/settings/openrouter.js';
import { pageRoutes } from '../../runtime/cells/routes/pages.js';
import { matchRoute } from '../../runtime/cells/router.js';
import { seeded } from './setup.js';

describe('the page route table', () => {
  it('matches every sub-resource before the deck and trivia catch-alls', () => {
    const of = (method: string, path: string) => matchRoute(pageRoutes, method, path)?.route.pattern;
    expect(of('GET', '/deck/world-capitals/export')).toBe('/deck/{name}/export');
    expect(of('GET', '/deck/world-capitals/question/new')).toBe('/deck/{name}/question/new');
    expect(of('GET', '/deck/world-capitals')).toBe('/deck/{name}');
    expect(of('GET', '/trivia/session/world-history')).toBe('/trivia/session/{deck_name}');
    expect(of('GET', '/trivia/10')).toBe('/trivia/{question_id}');
    expect(of('POST', '/trivia/decks/4/mute')).toBe('/trivia/decks/{deck_id}/mute');
    expect(of('GET', '/decks/new/srs')).toBe('/decks/new/srs');
  });

  it('claims no path outside its own surfaces', () => {
    for (const path of ['/api/study/decks/x/next', '/api/v1/decks', '/mcp', '/webhooks/clerk', '/api/offline/snapshot', '/sw.js']) {
      expect(matchRoute(pageRoutes, 'GET', path), path).toBeNull();
      expect(matchRoute(pageRoutes, 'POST', path), path).toBeNull();
    }
  });

  it('gates every page on a signed-in identity', () => {
    expect(pageRoutes.every((r) => r.gate === 'signedIn')).toBe(true);
  });

  it('refuses an unmatched method rather than answering the GET', async () => {
    const h = await seeded('reader');
    expect((await h.post('/deck/world-capitals')).status).toBe(404);
  });

  it('decodes a path parameter before it reaches a use case', async () => {
    const h = await seeded('reader');
    expect((await h.get('/deck/world%2Dcapitals')).status).toBe(200);
    expect(h.rendered()?.context['deck_name']).toBe('world-capitals');
  });
});

describe('the OpenRouter hand-off', () => {
  it('sets the verifier cookie and redirects with an S256 challenge', async () => {
    const h = await seeded('reader');
    const res = await h.get('/settings/agent/openrouter/start');
    expect(res.status).toBe(303);
    const target = new URL(res.headers.get('location')!);
    expect(target.origin + target.pathname).toBe('https://openrouter.ai/auth');
    expect(target.searchParams.get('callback_url')).toBe('https://parity.example.test/settings/agent/openrouter/callback');
    expect(target.searchParams.get('code_challenge_method')).toBe('S256');
    expect(target.searchParams.get('code_challenge')).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const cookie = res.headers.get('set-cookie')!;
    expect(cookie.startsWith(`${PKCE_COOKIE}=`)).toBe(true);
    expect(cookie).toContain('HttpOnly');
    expect(cookie).toContain('Path=/settings/agent/openrouter/callback');
  });

  it('refuses a callback with no code or no verifier, and drops the cookie', async () => {
    const h = await seeded('reader', { PREP_KEY_ENCRYPTION_SECRET: 'a'.repeat(64) });
    const noCode = await h.get('/settings/agent/openrouter/callback', { headers: { cookie: `${PKCE_COOKIE}=v` } });
    expect(noCode.status).toBe(400);
    expect(noCode.headers.get('set-cookie')).toContain('Max-Age=0');
    const noVerifier = await h.get('/settings/agent/openrouter/callback?code=abc');
    expect(noVerifier.status).toBe(400);
    expect(String(h.rendered()?.context['byok_error'])).toContain('expired');
  });

  it('stores the minted key like a pasted one', async () => {
    const h = await seeded('reader', { PREP_KEY_ENCRYPTION_SECRET: 'a'.repeat(64) });
    h.c.openRouter = {
      startChallenge: async () => ({ verifier: 'v', challenge: 'c' }),
      exchange: async () => `sk-or-v1-${'z'.repeat(40)}9999`,
    };
    const res = await h.get('/settings/agent/openrouter/callback?code=abc', { headers: { cookie: `${PKCE_COOKIE}=v` } });
    expect(res.status).toBe(200);
    expect(h.rendered()?.context['byok_flash']).toBe('Your OpenRouter key is saved. AI features now use your account.');
    expect(h.state.fake.rows('byok_credentials')[0]).toMatchObject({ provider: 'openrouter-api', key_prefix: 'sk-or-v1-zzzzz…9999' });
    expect(h.state.fake.rows('profile')[0]?.['active_byok_provider']).toBe('openrouter-api');
  });

  it('surfaces a refused exchange as a 502 on the settings page', async () => {
    const h = await seeded('reader', { PREP_KEY_ENCRYPTION_SECRET: 'a'.repeat(64) });
    h.c.openRouter = {
      startChallenge: async () => ({ verifier: 'v', challenge: 'c' }),
      exchange: async () => {
        throw new Error('HTTP 400');
      },
    };
    const res = await h.get('/settings/agent/openrouter/callback?code=abc', { headers: { cookie: `${PKCE_COOKIE}=v` } });
    expect(res.status).toBe(502);
    expect(String(h.rendered()?.context['byok_error'])).toContain('HTTP 400');
  });
});
