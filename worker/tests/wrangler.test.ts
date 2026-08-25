import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
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
]);
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
      }
    }
  });
});
