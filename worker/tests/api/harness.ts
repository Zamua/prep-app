// A Worker environment backed by real cell classes and fake SQLite storage.
import { DirectoryCell } from '../../runtime/cells/DirectoryCell.js';
import { InstantLimiterCell } from '../../runtime/cells/InstantLimiterCell.js';
import { UserCell } from '../../runtime/cells/UserCell.js';
import type { Env } from '../../runtime/env.js';
import worker from '../../runtime/worker.js';
import { fakeCellState, type FakeCellStorage } from '../fakes/sqlStorage.js';
import { WebCryptoHasher } from '../../runtime/adapters/hash.js';
import { userRepos } from '../../runtime/adapters/sql/index.js';
import { SeededSessionIds, SeededRandom } from '../../runtime/adapters/random.js';
import { assembleToken, maskToken } from '../../domain/pat.js';

export const SEED_USER = 'seed@example.com';
export const INTERNAL_TOKEN = 'test-internal-token';
export const ORIGIN = 'https://prep.example.test';

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

export interface WorkerEnv {
  env: Env;
  userStorage(id: string): FakeCellStorage;
}

/** An entry-worker environment backed by fake cells. */
export function workerEnv(overrides: Partial<Env> = {}): WorkerEnv {
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
    PREP_TEST_MODE: '1',
    PREP_FAKE_NOW: '2026-03-14T15:00:00Z',
    PREP_BUILD_ID: 'ce11d0000000',
    PREP_PLACEHOLDER_INDEX: '0',
    PREP_INTERNAL_TOKEN: INTERNAL_TOKEN,
    PREP_KEY_ENCRYPTION_SECRET: '11'.repeat(32),
    PREP_FREE_INFERENCE_BASE_URL: 'http://127.0.0.1:9/v1',
    PREP_FREE_INFERENCE_API_KEY: 'test-free-tier-key',
    PREP_FREE_INFERENCE_MODEL: 'test-model',
    PREP_CLIENT_IP_HEADER: 'x-real-ip',
    PREP_VAPID_PUBLIC_KEY: 'BCT1EPH4xriWIwlJllh05zjCEDDXMj0G_-IzKI5Zp-42-Kk0tAjtpxKl2cPvIDToNDxQlXOXUXivZmMV2BuMku8',
    ...overrides,
  };
  return { env, userStorage: (id) => users.storage(id) };
}

/** Seed a test user through the entry worker. */
export async function seed(env: Env, profile: string, user = SEED_USER): Promise<Record<string, unknown>> {
  const res = await worker.fetch(
    new Request(`${ORIGIN}/_test/seed`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-internal-token': INTERNAL_TOKEN },
      body: JSON.stringify({ user, profile }),
    }),
    env,
  );
  if (res.status !== 200) throw new Error(`seed failed: ${res.status} ${await res.text()}`);
  return (await res.json()) as Record<string, unknown>;
}

/** Mint a personal access token directly in the subject's cell. */
export async function mintToken(storage: FakeCellStorage, subject: string, label: string): Promise<string> {
  const hasher = new WebCryptoHasher();
  const token = assembleToken(subject, new SeededRandom(20260315).bytes(32));
  let counter = 0;
  const repos = userRepos(storage, {
    clock: { now: () => new Date('2026-03-14T15:00:00Z') },
    random: new SeededRandom(20260316),
    fuzz: false,
    sessionIds: new SeededSessionIds({ get: async () => counter, set: async (n) => void (counter = n) }),
  });
  repos.tokens.insert(await hasher.sha256Hex(token), maskToken(token), label);
  return token;
}
