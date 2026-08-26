import { describe, expect, it } from 'vitest';
import { compose, composeWith, noCacheHtml } from '../runtime/compose.js';
import { fakeEnv, IDENTIFIED, req } from './helpers.js';

/** A prod-shaped env: no pin of any kind. */
const PROD = { PREP_ENV: 'prod', PREP_PARITY_MODE: undefined, PREP_FAKE_NOW: undefined, PREP_PLACEHOLDER_INDEX: undefined };

describe('compose', () => {
  it.each([
    ['parity on prod', { ...PROD, PREP_PARITY_MODE: '1' }, 'PREP_PARITY_MODE'],
    ['parity with no PREP_ENV at all', { ...PROD, PREP_ENV: undefined as unknown as string, PREP_PARITY_MODE: '1' }, 'PREP_PARITY_MODE'],
    ['a frozen clock on prod', { ...PROD, PREP_FAKE_NOW: '2026-03-14T15:00:00Z' }, 'PREP_FAKE_NOW'],
    ['a pinned placeholder under a misspelt env', { ...PROD, PREP_ENV: 'production', PREP_PLACEHOLDER_INDEX: '0' }, 'PREP_PLACEHOLDER_INDEX'],
  ])('refuses %s', (_name, overrides, pin) => {
    expect(() => compose(fakeEnv(overrides))).toThrow(new RegExp(`^refusing .*${pin}.* outside dev and staging`));
  });

  // The subscribe handshake reads the public key from an endpoint that
  // answers an empty string when it is unset, so nothing else would say so.
  it('says so when only half the VAPID pair reached the deploy', () => {
    const said: string[] = [];
    compose(fakeEnv({ ...PROD, PREP_VAPID_PRIVATE_KEY: 'k', PREP_VAPID_PUBLIC_KEY: undefined }), (m) => said.push(m));
    expect(said.join(' ')).toContain('PREP_VAPID_PUBLIC_KEY');
    const both: string[] = [];
    compose(fakeEnv({ ...PROD, PREP_VAPID_PRIVATE_KEY: undefined, PREP_VAPID_PUBLIC_KEY: undefined }), (m) => both.push(m));
    expect(both.join(' ')).not.toContain('PREP_VAPID');
  });

  it('composes prod without pins, on the system clock', () => {
    const c = compose(fakeEnv(PROD));
    expect(c.parity).toBe(false);
    expect(Math.abs(c.clock.now().getTime() - Date.now())).toBeLessThan(5_000);
  });

  it('composes the fake provider under parity on staging', async () => {
    const c = compose(fakeEnv({ PREP_ENV: 'staging', PREP_PARITY_MODE: '1' }));
    expect(c.parity).toBe(true);
    expect(await c.identity.identify(req('/', { headers: IDENTIFIED }))).toEqual({
      subject: 'parity@example.com',
      kind: 'fake',
      displayName: 'Parity',
      email: 'parity@example.com',
      profilePicUrl: null,
    });
    expect(await c.identity.identify(req('/'))).toBeNull();
    // The tailscale headers alone prove nothing (decision 7.0, option c).
    const { 'x-internal-token': _token, ...unsigned } = IDENTIFIED;
    expect(await c.identity.identify(req('/', { headers: unsigned }))).toBeNull();
  });

  it('has no identity provider without parity', async () => {
    const c = compose(fakeEnv(PROD));
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
});
