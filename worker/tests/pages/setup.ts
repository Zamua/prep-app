// One seeded user cell, driven through its own route table, with the
// rendered template and context captured.
import type { Composition } from '../../runtime/compose.js';
import { composeWith } from '../../runtime/compose.js';
import { UserCell } from '../../runtime/cells/UserCell.js';
import { KIND_HEADER, SUBJECT_HEADER } from '../../runtime/cells/router.js';
import type { Env } from '../../runtime/env.js';
import { fakeCellState } from '../fakes/sqlStorage.js';
import { fakeEnv, req, spyRenderer } from '../helpers.js';

export const USER = 'seed@example.com';

/** A funded free tier, so a seeded account has an agent available. */
export const FREE_TIER = {
  PREP_FREE_INFERENCE_BASE_URL: 'http://127.0.0.1:1/v1',
  PREP_FREE_INFERENCE_API_KEY: 'test-free-tier-key',
  PREP_FREE_INFERENCE_MODEL: 'test-model',
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
  const identity = { [SUBJECT_HEADER]: USER, 'x-prep-display-name': 'Seed', [KIND_HEADER]: 'fake' };
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
