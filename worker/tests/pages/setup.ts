// One seeded user cell, driven through its own route table, with the
// rendered template and context captured. The corpus under
// tests/fixtures/parity/pages is the oracle for both.
import type { Composition } from '../../runtime/compose.js';
import { composeWith } from '../../runtime/compose.js';
import { UserCell } from '../../runtime/cells/UserCell.js';
import { KIND_HEADER, SUBJECT_HEADER } from '../../runtime/cells/router.js';
import type { Env } from '../../runtime/env.js';
import { fakeCellState } from '../fakes/sqlStorage.js';
import { corpusPage, fakeEnv, req, spyRenderer } from '../helpers.js';

export const USER = 'parity@example.com';

/** The parity server's own free-tier pins, so `agent_available` and the
 * free-tier callout read as the corpus recorded them. */
export const FREE_TIER = {
  PREP_FREE_INFERENCE_BASE_URL: 'http://127.0.0.1:1/v1',
  PREP_FREE_INFERENCE_API_KEY: 'parity-free-tier-key',
  PREP_FREE_INFERENCE_MODEL: 'parity-model',
};

export interface Harness {
  env: Env;
  c: Composition;
  cell: UserCell;
  renderer: ReturnType<typeof spyRenderer>;
  state: ReturnType<typeof fakeCellState>;
  /** The last render, or null when the response carried no page. */
  rendered(): { template: string; context: Record<string, unknown> } | null;
  get(path: string, init?: RequestInit): Promise<Response>;
  post(path: string, body?: Record<string, string | string[]>, init?: RequestInit): Promise<Response>;
}

export function harness(overrides: Partial<Env> = {}): Harness {
  const env = fakeEnv({ ...FREE_TIER, ...overrides });
  const renderer = spyRenderer();
  const c = composeWith(env, { renderer });
  const state = fakeCellState();
  const cell = new UserCell(state, env);
  const identity = { [SUBJECT_HEADER]: USER, 'x-prep-display-name': 'Parity', [KIND_HEADER]: 'fake' };
  const send = (path: string, init: RequestInit) =>
    cell.fetch(req(path, { ...init, headers: { ...identity, ...(init.headers as Record<string, string>) } }));
  return {
    env,
    c,
    cell,
    renderer,
    state,
    rendered: () => renderer.calls[renderer.calls.length - 1] ?? null,
    get: (path, init = {}) => send(path, init),
    post: (path, body = {}, init = {}) => {
      const form = new URLSearchParams();
      for (const [k, v] of Object.entries(body)) {
        if (Array.isArray(v)) for (const one of v) form.append(k, one);
        else form.append(k, v);
      }
      return send(path, { ...init, method: 'POST', body: form.toString(), headers: { 'content-type': 'application/x-www-form-urlencoded', ...(init.headers as Record<string, string>) } });
    },
  };
}

export async function seeded(profile: string, overrides: Partial<Env> = {}): Promise<Harness> {
  const h = harness(overrides);
  await h.cell.seed(profile, USER, null);
  h.renderer.calls.length = 0;
  return h;
}

/** The corpus context, minus the columns a cell does not carry: the user
 * key was dropped from every table when one cell became one user. */
export function expectedContext(profile: string, file: string): Record<string, unknown> {
  return stripUserColumns(corpusPage(profile, file).context ?? {}) as Record<string, unknown>;
}

export function stripUserColumns(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripUserColumns);
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (k === 'user_id') continue;
      out[k] = stripUserColumns(v);
    }
    return out;
  }
  return value;
}

/** The context a page rendered with, comparable against the corpus: the
 * request origin is the router's, not the recording's. */
export function renderedContext(h: Harness): Record<string, unknown> {
  const call = h.rendered();
  if (!call) throw new Error('nothing was rendered');
  const { app_base: _appBase, ...rest } = call.context;
  return JSON.parse(JSON.stringify(rest)) as Record<string, unknown>;
}
