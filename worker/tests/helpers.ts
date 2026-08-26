import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Renderer } from '../app/ports.js';
import { DirectoryCell } from '../runtime/cells/DirectoryCell.js';
import { InstantLimiterCell } from '../runtime/cells/InstantLimiterCell.js';
import type { Env } from '../runtime/env.js';
import { fakeCellState } from './fakes/sqlStorage.js';

export const ROOT = new URL('..', import.meta.url).pathname;
export const CORPUS = join(ROOT, '..', 'tests', 'fixtures', 'parity', 'pages');

export function corpusPage(profile: string, file: string): {
  status: number;
  template?: string;
  context?: Record<string, unknown>;
  headers: Record<string, string>;
} {
  return JSON.parse(readFileSync(join(CORPUS, profile, `${file}.json`), 'utf8'));
}

export interface Rendered {
  template: string;
  context: Record<string, unknown>;
}

/** A renderer that records what it was asked for. */
export function spyRenderer(): Renderer & { calls: Rendered[] } {
  const calls: Rendered[] = [];
  return {
    calls,
    render(template, context) {
      calls.push({ template, context });
      return `<rendered ${template}>`;
    },
  };
}

/** A cell state over fake SQL storage and KV. */
export function fakeState(): DurableObjectState {
  return fakeCellState();
}

/** A namespace whose stubs are made on first `get` per name. */
export function namespaceOf(make: (name: string) => object): DurableObjectNamespace {
  const stubs = new Map<string, object>();
  return {
    idFromName: (name: string) => ({ name, toString: () => name }),
    get: (id: { name: string }) => {
      let stub = stubs.get(id.name);
      if (!stub) {
        stub = make(id.name);
        stubs.set(id.name, stub);
      }
      return stub;
    },
  } as unknown as DurableObjectNamespace;
}

export const unreachable = (): DurableObjectNamespace =>
  namespaceOf(() => ({
    fetch: async () => {
      throw new Error('namespace must not be reached');
    },
  }));

/** A namespace of real cells over fake storage, one per name. */
export function cellNamespace(make: (state: DurableObjectState, env: Env) => object, env: () => Env): DurableObjectNamespace {
  return namespaceOf(() => make(fakeCellState(), env()));
}

export function fakeEnv(overrides: Partial<Env> = {}): Env {
  const env: Env = {
    USER: unreachable(),
    DIRECTORY: cellNamespace((state, e) => new DirectoryCell(state, e), () => env),
    INSTANT_LIMITER: cellNamespace((state, e) => new InstantLimiterCell(state, e), () => env),
    JOB: unreachable(),
    ASSETS: { fetch: async () => new Response('asset') } as unknown as Fetcher,
    PREP_ENV: 'dev',
    PREP_PARITY_MODE: '1',
    PREP_FAKE_NOW: '2026-03-14T15:00:00Z',
    PREP_BUILD_ID: 'ce11d0000000',
    PREP_PLACEHOLDER_INDEX: '0',
    PREP_INTERNAL_TOKEN: 'parity-internal-token',
    ...overrides,
  };
  return env;
}

export const IDENTIFIED = { 'tailscale-user-login': 'parity@example.com', 'tailscale-user-name': 'Parity' };

export function req(path: string, init: RequestInit = {}): Request {
  return new Request(`https://parity.example.test${path}`, init);
}
