import { describe, expect, it } from 'vitest';
import { compose, composeWith, cookieHooks, noCacheHtml } from '../runtime/compose.js';
import { fakeEnv, IDENTIFIED, req } from './helpers.js';

describe('compose', () => {
  it('refuses parity on prod', () => {
    expect(() => compose(fakeEnv({ PREP_ENV: 'prod', PREP_PARITY_MODE: '1' }))).toThrow(
      'refusing the fake identity provider on prod',
    );
  });

  it('composes the fake provider under parity on staging', async () => {
    const c = compose(fakeEnv({ PREP_ENV: 'staging', PREP_PARITY_MODE: '1' }));
    expect(c.parity).toBe(true);
    expect(await c.identity.identify(req('/', { headers: IDENTIFIED }))).toEqual({
      subject: 'parity@example.com',
      displayName: 'Parity',
    });
    expect(await c.identity.identify(req('/'))).toBeNull();
  });

  it('has no identity provider without parity', async () => {
    const c = compose(fakeEnv({ PREP_ENV: 'prod', PREP_PARITY_MODE: undefined }));
    expect(c.parity).toBe(false);
    expect(await c.identity.identify(req('/', { headers: IDENTIFIED }))).toBeNull();
  });

  it('pins the clock and the build token from the env', () => {
    const c = compose(fakeEnv());
    expect(c.clock.now().toISOString()).toBe('2026-03-14T15:00:00.000Z');
    expect(c.buildToken).toBe('ce11d0000000');
    expect(c.internalToken).toBe('parity-internal-token');
  });

  it('is memoized per env', () => {
    const env = fakeEnv();
    expect(compose(env)).toBe(compose(env));
    expect(compose(env)).not.toBe(compose(fakeEnv()));
  });

  it('composeWith replaces ports through the same root', () => {
    const env = fakeEnv();
    const renderer = { render: () => 'x' };
    expect(composeWith(env, { renderer }).renderer).toBe(renderer);
    expect(compose(env).renderer).toBe(renderer);
  });
});

describe('the cross-cutting wrappers', () => {
  it('no-cache lands on HTML only', () => {
    const html = noCacheHtml(new Response('<p>', { headers: { 'content-type': 'text/html; charset=utf-8' } }));
    expect(html.headers.get('cache-control')).toBe('no-cache');
    const json = noCacheHtml(Response.json({ ok: true }));
    expect(json.headers.get('cache-control')).toBeNull();
  });

  it('cookieHooks is the identity until phase 3', () => {
    const res = new Response('x');
    expect(cookieHooks(req('/'), res)).toBe(res);
  });
});
