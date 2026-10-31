import { describe, expect, it } from 'vitest';
import { compose, noCacheHtml } from '../runtime/compose.js';
import { fakeEnv, IDENTIFIED, req } from './helpers.js';

/** A prod-shaped env: no pin of any kind. */
const PROD = { PREP_ENV: 'prod', PREP_TEST_MODE: undefined, PREP_FAKE_NOW: undefined, PREP_PLACEHOLDER_INDEX: undefined };

describe('compose', () => {
  it.each([
    ['test mode on prod', { ...PROD, PREP_TEST_MODE: '1' }, 'PREP_TEST_MODE'],
    ['test mode with no PREP_ENV at all', { ...PROD, PREP_ENV: undefined as unknown as string, PREP_TEST_MODE: '1' }, 'PREP_TEST_MODE'],
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

  describe('the account deleter', () => {
    const CLERK = {
      CLERK_ISSUER: 'https://clerk.example.test',
      CLERK_JWKS_URL: 'https://clerk.example.test/.well-known/jwks.json',
      CLERK_AUTHORIZED_PARTIES: 'https://app.example.test',
      CLERK_ACCOUNTS_URL: 'https://accounts.example.test',
      CLERK_PUBLISHABLE_KEY: 'pk_test_' + Buffer.from('clerk.example.test$').toString('base64'),
      CLERK_SECRET_KEY: 'sk_test_secret',
      CLERK_WEBHOOK_SECRET: 'whsec_' + Buffer.from('signing').toString('base64'),
    };

    it('exists where real Clerk sign-in can both delete and hear the webhook', () => {
      expect(compose(fakeEnv({ ...PROD, ...CLERK })).accountDeleter).not.toBeNull();
    });

    it.each([
      ['no Clerk at all', PROD, null],
      ['test mode', { PREP_ENV: 'staging', PREP_TEST_MODE: '1', ...CLERK }, null],
      ['no secret key', { ...PROD, ...CLERK, CLERK_SECRET_KEY: undefined }, 'CLERK_SECRET_KEY'],
      ['no webhook secret, so the cell would never be wiped', { ...PROD, ...CLERK, CLERK_WEBHOOK_SECRET: undefined }, 'CLERK_WEBHOOK_SECRET'],
    ])('is off with %s, and says which secret is missing', (_name, env, missing) => {
      const said: string[] = [];
      expect(compose(fakeEnv(env), (m) => said.push(m)).accountDeleter).toBeNull();
      const warning = said.find((m) => m.startsWith('account delete is off'));
      if (missing) expect(warning).toContain(missing);
      else expect(warning).toBeUndefined();
    });
  });

  it('composes prod without pins, on the system clock', () => {
    const c = compose(fakeEnv(PROD));
    expect(c.testMode).toBe(false);
    expect(Math.abs(c.clock.now().getTime() - Date.now())).toBeLessThan(5_000);
  });

  it('composes the fake provider in test mode on staging', async () => {
    const c = compose(fakeEnv({ PREP_ENV: 'staging', PREP_TEST_MODE: '1' }));
    expect(c.testMode).toBe(true);
    expect(await c.identity.identify(req('/', { headers: IDENTIFIED }))).toEqual({
      subject: 'seed@example.com',
      kind: 'fake',
      displayName: 'Seed',
      email: 'seed@example.com',
      profilePicUrl: null,
    });
    expect(await c.identity.identify(req('/'))).toBeNull();
    // The tailscale headers alone prove nothing.
    const { 'x-internal-token': _token, ...unsigned } = IDENTIFIED;
    expect(await c.identity.identify(req('/', { headers: unsigned }))).toBeNull();
  });

  it('has no identity provider outside test mode', async () => {
    const c = compose(fakeEnv(PROD));
    expect(c.testMode).toBe(false);
    expect(await c.identity.identify(req('/', { headers: IDENTIFIED }))).toBeNull();
  });

  it('pins the clock and the build token from the env', () => {
    const c = compose(fakeEnv());
    expect(c.clock.now().toISOString()).toBe('2026-03-14T15:00:00.000Z');
    expect(c.buildToken).toBe('ce11d0000000');
    expect(c.internalToken).toBe('test-internal-token');
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
