import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { frontendApiHost } from '../runtime/adapters/clerk.js';
import { jobLlmTimeoutMs } from '../runtime/compose.js';
import { ROOT } from './helpers.js';

const ENVS = ['dev', 'staging', 'prod'] as const;
const CELLS = ['UserCell', 'DirectoryCell', 'InstantLimiterCell', 'JobCell'];
const PUBLIC_VARS = new Set([
  'PREP_ENV',
  'CELLD_FETCH_TIMEOUT_S',
  'PREP_TEST_MODE',
  'PREP_TEST_NO_PERIODIC',
  'PREP_FAKE_NOW',
  'PREP_BUILD_ID',
  'PREP_PLACEHOLDER_INDEX',
  'PREP_INTERNAL_TOKEN',
  'CLERK_ISSUER',
  'CLERK_JWKS_URL',
  'CLERK_ACCOUNTS_URL',
  'CLERK_AUTHORIZED_PARTIES',
  'CLERK_PUBLISHABLE_KEY',
]);
/** A secret in a deploy file is the mistake this file exists to catch. */
const SECRET_NAME = /SECRET|TOKEN|KEY|PASSWORD/;
/** Named like a credential and public by construction: the publishable key
 * ships in every page's markup. */
const NOT_A_SECRET = new Set(['CLERK_PUBLISHABLE_KEY']);
/** The test pins, committable in `dev` alone. `PREP_INTERNAL_TOKEN` is one
 * of them and is what gates the fake identity provider, so
 * anywhere else it is a secret and arrives as `CELLD_VAR_`. */
const DEV_ONLY = /^(PREP_TEST|PREP_FAKE|PREP_INTERNAL|PREP_BUILD_ID)/;

/** JSON with `//` comments; a `//` inside a string is not a comment. */
function stripComments(src: string): string {
  let out = '';
  let inString = false;
  for (let i = 0; i < src.length; i++) {
    const ch = src[i]!;
    if (inString) {
      out += ch;
      if (ch === '\\') out += src[++i] ?? '';
      else if (ch === '"') inString = false;
    } else if (ch === '"') {
      inString = true;
      out += ch;
    } else if (ch === '/' && src[i + 1] === '/') {
      while (i < src.length && src[i] !== '\n') i++;
      out += '\n';
    } else out += ch;
  }
  return out;
}

interface Wrangler {
  name: string;
  main: string;
  durable_objects: { bindings: { name: string; class_name: string }[] };
  migrations: unknown[];
  assets: unknown;
  vars: Record<string, string>;
}

const files = Object.fromEntries(
  ENVS.map((env) => [env, JSON.parse(stripComments(readFileSync(join(ROOT, `wrangler.${env}.jsonc`), 'utf8'))) as Wrangler]),
) as Record<(typeof ENVS)[number], Wrangler>;

describe('the three wrangler files', () => {
  it('share bindings, migrations and assets', () => {
    for (const env of ENVS) {
      expect(files[env].name).toBe('prep');
      expect(files[env].main).toBe('runtime/worker.ts');
      expect(files[env].durable_objects).toEqual(files.dev.durable_objects);
      expect(files[env].migrations).toEqual(files.dev.migrations);
      expect(files[env].assets).toEqual(files.dev.assets);
    }
  });

  it('declare exactly the four cells', () => {
    expect(files.dev.durable_objects.bindings.map((b) => b.class_name)).toEqual(CELLS);
    expect(files.dev.migrations).toEqual([{ tag: 'v1', new_sqlite_classes: CELLS }]);
    expect(files.dev.assets).toEqual({ directory: 'dist/assets', binding: 'ASSETS', run_worker_first: true });
  });

  it('name their environment', () => {
    for (const env of ENVS) expect(files[env].vars.PREP_ENV).toBe(env);
  });

  it('carry the test pins in dev only', () => {
    for (const key of Object.keys(files.staging.vars)) expect(key).not.toMatch(DEV_ONLY);
    for (const key of Object.keys(files.prod.vars)) expect(key).not.toMatch(DEV_ONLY);
    expect(files.dev.vars.PREP_TEST_MODE).toBe('1');
    expect(files.dev.vars.PREP_BUILD_ID).toBe('ce11d0000000');
    expect(files.dev.vars.PREP_INTERNAL_TOKEN).toBe('test-internal-token');
  });

  it('hold only public var names', () => {
    for (const env of ENVS) {
      for (const key of Object.keys(files[env].vars)) {
        expect(PUBLIC_VARS.has(key), `${env}: ${key} is not an allowed public var`).toBe(true);
        const secretish = SECRET_NAME.test(key) && !NOT_A_SECRET.has(key) && !(env === 'dev' && DEV_ONLY.test(key));
        expect(secretish, `${env}: ${key} names a credential; it belongs in CELLD_VAR_*`).toBe(false);
      }
    }
  });

  it('name Clerk the same way on staging and prod', () => {
    for (const env of ['staging', 'prod'] as const) {
      const vars = files[env].vars;
      expect(vars.CLERK_JWKS_URL, env).toBe(`${vars.CLERK_ISSUER}/.well-known/jwks.json`);
      // `urls()` redirects back to the first party, so the deploy's own
      // origin has to lead the list or staging bounces onto prod.
      expect((vars.CLERK_AUTHORIZED_PARTIES ?? '').split(',')[0], env).toMatch(/^https:\/\//);
      // The key encodes the frontend API host, and `compose` refuses to boot
      // without it: a blank or borrowed one leaves ClerkJS unbootstrapped and
      // the dormant-session recovery with nothing to recover through.
      expect(frontendApiHost(vars.CLERK_PUBLISHABLE_KEY ?? ''), env).toBe(new URL(vars.CLERK_ISSUER!).host);
    }
  });

  it('keeps the seed credential out of staging and prod', () => {
    for (const env of ['staging', 'prod'] as const) expect(files[env].vars.PREP_INTERNAL_TOKEN, env).toBeUndefined();
  });

  // Unset, the worker assumes celld's 120s default and clamps an LLM step to
  // 115s, where the spec and the node both allow 300s.
  it('gives an LLM step its full budget on the two real deploys', () => {
    for (const env of ['staging', 'prod'] as const) {
      expect(files[env].vars.CELLD_FETCH_TIMEOUT_S, env).toBe('330');
      expect(jobLlmTimeoutMs({ CELLD_FETCH_TIMEOUT_S: files[env].vars.CELLD_FETCH_TIMEOUT_S }), env).toBe(300_000);
    }
  });
});

describe('the authorized parties cover every host the ingress serves', () => {
  // A host that reaches the app but is not authorized fails Clerk at
  // sign-in, and only for the users who happen to be on it.
  // dev is absent: it runs without Clerk, so it defines no parties.
  const SERVED: Record<string, string[]> = {
    staging: ['https://staging.prepcards.app', 'https://celld.staging.prepcards.app'],
    prod: ['https://prepcards.app', 'https://www.prepcards.app'],
  };

  for (const env of Object.keys(SERVED)) {
    it(`${env} authorizes every host it answers on`, () => {
      const cfg = JSON.parse(
        readFileSync(join(ROOT, `wrangler.${env}.jsonc`), 'utf8').replace(/^\s*\/\/.*$/gm, ''),
      );
      const parties = String(cfg.vars.CLERK_AUTHORIZED_PARTIES).split(',').map((s) => s.trim());
      for (const host of SERVED[env]!) expect(parties).toContain(host);
    });
  }
});

describe('the accounts URL is the account portal, not the frontend API', () => {
  // The FAPI host serves no /sign-in, so pointing the portal at it turns
  // every sign-in into a 404 while the rest of the app looks healthy.
  for (const env of ['staging', 'prod'] as const) {
    it(`${env} does not point the portal at its own FAPI host`, () => {
      const cfg = JSON.parse(
        readFileSync(join(ROOT, `wrangler.${env}.jsonc`), 'utf8').replace(/^\s*\/\/.*$/gm, ''),
      );
      const accounts = new URL(String(cfg.vars.CLERK_ACCOUNTS_URL)).host;
      expect(accounts).not.toBe(frontendApiHost(String(cfg.vars.CLERK_PUBLISHABLE_KEY)));
      expect(accounts.startsWith('clerk.')).toBe(false);
    });
  }
});
