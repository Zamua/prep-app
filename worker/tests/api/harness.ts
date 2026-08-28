// Replays a recorded corpus against the TypeScript app: the entry worker
// over real cells whose storage is the SqlStorage fake. One env, one
// isolate, so a pair sees what the pairs before it wrote, exactly as the
// recording did.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { DirectoryCell } from '../../runtime/cells/DirectoryCell.js';
import { InstantLimiterCell } from '../../runtime/cells/InstantLimiterCell.js';
import { UserCell } from '../../runtime/cells/UserCell.js';
import type { Env } from '../../runtime/env.js';
import worker from '../../runtime/worker.js';
import { fakeCellState, type FakeCellStorage } from '../fakes/sqlStorage.js';
import { WebCryptoHasher } from '../../runtime/adapters/hash.js';
import { userRepos } from '../../runtime/adapters/sql/index.js';
import { ParitySessionIds, SeededRandom } from '../../runtime/adapters/random.js';
import { assembleToken, maskToken } from '../../domain/pat.js';

export const CORPUS_ROOT = join(new URL('../..', import.meta.url).pathname, '..', 'tests', 'fixtures', 'parity');
export const PARITY_USER = 'parity@example.com';
export const INTERNAL_TOKEN = 'parity-internal-token';
export const ORIGIN = 'https://parity.example.test';

export interface Pair {
  name: string;
  note: string | null;
  request: {
    method: string;
    path: string;
    headers: Record<string, string>;
    json: unknown;
    form: Record<string, string> | null;
    text: string | null;
  };
  response: {
    status: number;
    content_type: string | null;
    json: unknown;
    text: string | null;
    location: string | null;
    set_cookie: string[];
  };
}

export interface Corpus {
  header: { volatile?: { pairs: string; pointer: string; regex: string }[]; ids: Record<string, unknown> };
  pairs: Pair[];
}

export function loadCorpus(name: string): Corpus {
  return JSON.parse(readFileSync(join(CORPUS_ROOT, name, 'pairs.json'), 'utf8')) as Corpus;
}

/** A namespace whose stubs are the real cell class over fake storage. */
function cells<T>(make: (state: DurableObjectState) => T): DurableObjectNamespace & { storage(name: string): FakeCellStorage } {
  const stubs = new Map<string, { cell: T; fake: FakeCellStorage }>();
  const entry = (name: string) => {
    let e = stubs.get(name);
    if (!e) {
      const state = fakeCellState();
      e = { cell: make(state), fake: state.fake };
      stubs.set(name, e);
    }
    return e;
  };
  return {
    idFromName: (name: string) => ({ name, toString: () => name }),
    get: (id: { name: string }) => entry(id.name).cell,
    storage: (name: string) => entry(name).fake,
  } as unknown as DurableObjectNamespace & { storage(name: string): FakeCellStorage };
}

export interface ReplayEnv {
  env: Env;
  userStorage(id: string): FakeCellStorage;
}

/** The parity environment a recorded node runs with, minus the paths. */
export function replayEnv(overrides: Partial<Env> = {}): ReplayEnv {
  const users = cells((state) => new UserCell(state, env));
  const directory = cells((state) => new DirectoryCell(state, env));
  const limiter = cells((state) => new InstantLimiterCell(state, env));
  const env: Env = {
    USER: users,
    DIRECTORY: directory,
    INSTANT_LIMITER: limiter,
    JOB: cells(() => ({})) as unknown as DurableObjectNamespace,
    ASSETS: { fetch: async () => new Response(null, { status: 404 }) } as unknown as Fetcher,
    PREP_ENV: 'dev',
    PREP_PARITY_MODE: '1',
    PREP_FAKE_NOW: '2026-03-14T15:00:00Z',
    PREP_BUILD_ID: 'ce11d0000000',
    PREP_PLACEHOLDER_INDEX: '0',
    PREP_INTERNAL_TOKEN: INTERNAL_TOKEN,
    PREP_KEY_ENCRYPTION_SECRET: '11'.repeat(32),
    PREP_FREE_INFERENCE_BASE_URL: 'http://127.0.0.1:9/v1',
    PREP_FREE_INFERENCE_API_KEY: 'parity-free-tier-key',
    PREP_FREE_INFERENCE_MODEL: 'parity-model',
    PREP_CLIENT_IP_HEADER: 'x-real-ip',
    PREP_VAPID_PUBLIC_KEY: 'BCT1EPH4xriWIwlJllh05zjCEDDXMj0G_-IzKI5Zp-42-Kk0tAjtpxKl2cPvIDToNDxQlXOXUXivZmMV2BuMku8',
    ...overrides,
  };
  return { env, userStorage: (id) => users.storage(id) };
}

export interface Recorded {
  status: number;
  contentType: string | null;
  json: unknown;
  text: string | null;
  location: string | null;
  setCookie: string[];
}

function requestOf(pair: Pair, extraHeaders: Record<string, string> = {}, jsonOverride?: unknown): Request {
  const headers = new Headers();
  for (const [k, v] of Object.entries(pair.request.headers)) headers.set(k, v);
  for (const [k, v] of Object.entries(extraHeaders)) headers.set(k, v);
  // The fake identity provider only trusts the tailscale headers when the
  // internal token rides along, as the recording sends them.
  if (headers.has('tailscale-user-login')) headers.set('x-internal-token', INTERNAL_TOKEN);
  let body: BodyInit | undefined;
  const json = jsonOverride === undefined ? pair.request.json : jsonOverride;
  if (json !== null && json !== undefined) {
    body = JSON.stringify(json);
    if (!headers.has('content-type')) headers.set('content-type', 'application/json');
  } else if (pair.request.text !== null) {
    body = pair.request.text;
  } else if (pair.request.form !== null) {
    body = new URLSearchParams(pair.request.form).toString();
    headers.set('content-type', 'application/x-www-form-urlencoded');
  }
  if (body !== undefined) headers.set('content-length', String(new TextEncoder().encode(String(body)).length));
  return new Request(`${ORIGIN}${pair.request.path}`, { method: pair.request.method, headers, body });
}

export async function record(res: Response): Promise<Recorded> {
  const contentType = res.headers.get('content-type');
  const raw = await res.text();
  const isJson = (contentType ?? '').startsWith('application/json');
  let json: unknown = null;
  if (isJson && raw) {
    try {
      json = JSON.parse(raw);
    } catch {
      json = null;
    }
  }
  return {
    status: res.status,
    contentType,
    json,
    // The recording holds `response.text` for any non-JSON body, so an empty one
    // is '' and never null; collapsing it loses a 303's empty body.
    text: isJson ? null : raw,
    location: res.headers.get('location'),
    // `get('set-cookie')` comma-joins a sequence, so two cookies would record
    // as one malformed value; `getSetCookie` keeps them apart.
    setCookie: res.headers.getSetCookie(),
  };
}

export async function replay(env: Env, pair: Pair, extraHeaders: Record<string, string> = {}, jsonOverride?: unknown): Promise<Recorded> {
  return record(await worker.fetch(requestOf(pair, extraHeaders, jsonOverride), env));
}

/** `POST /_parity/seed` through the entry worker, as the recording does. */
export async function seed(env: Env, profile: string, user = PARITY_USER): Promise<Record<string, unknown>> {
  const res = await worker.fetch(
    new Request(`${ORIGIN}/_parity/seed`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-internal-token': INTERNAL_TOKEN },
      body: JSON.stringify({ user, profile }),
    }),
    env,
  );
  if (res.status !== 200) throw new Error(`seed failed: ${res.status} ${await res.text()}`);
  return (await res.json()) as Record<string, unknown>;
}

/** A personal access token for `subject`, written straight into its cell:
 * the settings page that mints one is another lane's, and the recorded
 * bearer is volatile anyway. */
export async function mintToken(storage: FakeCellStorage, subject: string, label: string): Promise<string> {
  const hasher = new WebCryptoHasher();
  const token = assembleToken(subject, new SeededRandom(20260315).bytes(32));
  let counter = 0;
  const repos = userRepos(storage, {
    clock: { now: () => new Date('2026-03-14T15:00:00Z') },
    random: new SeededRandom(20260316),
    fuzz: false,
    sessionIds: new ParitySessionIds({ get: async () => counter, set: async (n) => void (counter = n) }),
  });
  repos.tokens.insert(await hasher.sha256Hex(token), maskToken(token), label);
  return token;
}
