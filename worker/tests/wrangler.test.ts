import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { frontendApiHost } from '../runtime/adapters/clerk.js';
import { ROOT } from './helpers.js';

const ENVS = ['dev', 'staging', 'prod'] as const;
const CELLS = ['UserCell', 'DirectoryCell', 'InstantLimiterCell', 'JobCell'];
const PUBLIC_VARS = new Set([
  'PREP_ENV',
  'PREP_PARITY_MODE',
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
/** The parity pins, committable in `dev` alone. `PREP_INTERNAL_TOKEN` is one
 * of them and is what gates the fake identity provider (decision 7.0), so
 * anywhere else it is a secret and arrives as `CELLD_VAR_`. */
const DEV_ONLY = /^(PREP_PARITY|PREP_FAKE|PREP_INTERNAL|PREP_BUILD_ID)/;

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

  it('carry the parity pins in dev only', () => {
    for (const key of Object.keys(files.staging.vars)) expect(key).not.toMatch(DEV_ONLY);
    for (const key of Object.keys(files.prod.vars)) expect(key).not.toMatch(DEV_ONLY);
    expect(files.dev.vars.PREP_PARITY_MODE).toBe('1');
    expect(files.dev.vars.PREP_BUILD_ID).toBe('ce11d0000000');
    expect(files.dev.vars.PREP_INTERNAL_TOKEN).toBe('parity-internal-token');
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
});
