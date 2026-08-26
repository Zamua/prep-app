// The composition root: the only place adapters meet ports, memoized per
// isolate. Cross-cutting wrappers live here and are applied by the router,
// never inside a handler. Cells get their repositories through it too.
import type {
  Clock,
  Directory,
  FixturePages,
  Hasher,
  IdentityProvider,
  Limiter,
  Random,
  Renderer,
  SessionIds,
  Sync,
  UserCells,
  UserRepos,
} from '../app/ports.js';
import type { Fuzz } from '../domain/fsrs/index.js';
import { DEFAULT_LIMITS, type Limits } from '../domain/instant/limiter.js';
import { namespaceDirectory, namespaceLimiter, namespaceUserCells } from './adapters/cells.js';
import { clockFromEnv, FixedClock, parseFakeNow } from './adapters/clock.js';
import { FakeIdentityProvider, NoIdentityProvider } from './adapters/fakeIdentity.js';
import { fixturePagesFromBuild } from './adapters/fixturePages.js';
import { WebCryptoHasher } from './adapters/hash.js';
import { createRenderer } from './adapters/nunjucks/index.js';
import { ParitySessionIds, RandomSessionIds, SeededRandom, WebCryptoRandom } from './adapters/random.js';
import {
  DIRECTORY_MIGRATIONS,
  LIMITER_MIGRATIONS,
  migrate,
  resetSequences,
  seedSequences,
  SqlDirectoryRepo,
  SqlLimiterRepo,
  USER_MIGRATIONS,
  userRepos,
} from './adapters/sql/index.js';
import type { CellStorage } from './storage.js';
import { resolveBuildToken } from './buildToken.js';
import type { Env, InstantLimitEnv } from './env.js';

/** The seed of the parity harness (tests/parity/oracles/harness.py). */
export const PARITY_SEED = 20260314;
export const NOW_HEADER = 'x-prep-now';
export const PARITY_NOW_HEADER = 'x-parity-now';
const SESSION_COUNTER_KEY = 'parity_session_counter';

/** Three generators, seeded apart under parity as the Python harness patches them. */
export interface Randoms {
  instant: Random;
  merge: Random;
  tokens: Random;
}

export interface Composition {
  clock: Clock;
  identity: IdentityProvider;
  renderer: Renderer;
  pages: FixturePages;
  buildToken: string;
  parity: boolean;
  internalToken: string;
  authProvider: string;
  randoms: Randoms;
  fuzz: Fuzz;
  hasher: Hasher;
  limits: Limits;
  directory: Directory;
  limiter: Limiter;
  userCells: UserCells;
  userRepos(storage: CellStorage, clock: Clock): UserRepos;
  sessionIds(storage: CellStorage): SessionIds;
  directoryRepo(storage: CellStorage): Sync<Directory>;
  limiterRepo(storage: CellStorage): Sync<Limiter>;
  /** Re-seeds the parity generators; a no-op outside parity. */
  resetRandom(): void;
  /** Each cell class's migrations, idempotent, version written last. */
  migrateUserCell(storage: CellStorage): number;
  migrateDirectory(storage: CellStorage): number;
  migrateLimiter(storage: CellStorage): number;
  /** The cell's autoincrement counters at `idx * 2^32`; block 0 restarts at 1. */
  seedIdBlock(storage: CellStorage, idx: number): void;
  resetIdBlock(storage: CellStorage): void;
}

const compositions = new WeakMap<Env, Composition>();

/** The parity pins: the fake identity provider (a trusted header, an open
 * door anywhere real), the frozen clock and the fixed landing placeholder.
 * Only the two parity hosts may carry any of them; an unknown or missing
 * PREP_ENV is refused rather than trusted. */
const PIN = /^PREP_(PARITY|FAKE|PLACEHOLDER)/;
const PARITY_HOSTS = new Set(['dev', 'staging']);

function refusePinsOutsideParityHosts(env: Env): void {
  if (PARITY_HOSTS.has(env.PREP_ENV)) return;
  const vars = env as unknown as Record<string, unknown>;
  const pins = Object.keys(vars).filter((k) => PIN.test(k) && vars[k] != null);
  if (pins.length) {
    throw new Error(`refusing ${pins.join(', ')} outside dev and staging (PREP_ENV=${JSON.stringify(env.PREP_ENV ?? null)})`);
  }
}

/** Python's `_env_int`: unset or unparsable means the default. */
function envInt(raw: string | undefined, fallback: number): number {
  const s = (raw ?? '').trim();
  if (!s || !/^[+-]?\d+$/.test(s)) return fallback;
  return Number(s);
}

export function limitsFromEnv(env: InstantLimitEnv): Limits {
  const d = DEFAULT_LIMITS;
  return {
    burstLimit: envInt(env.PREP_INSTANT_BURST_LIMIT, d.burstLimit),
    burstWindowS: envInt(env.PREP_INSTANT_BURST_WINDOW_S, d.burstWindowS),
    perIpPerDay: envInt(env.PREP_INSTANT_PER_IP_PER_DAY, d.perIpPerDay),
    perAnonUserPerDay: envInt(env.PREP_INSTANT_PER_ANON_USER_PER_DAY, d.perAnonUserPerDay),
    perUserPerDay: envInt(env.PREP_INSTANT_PER_USER_PER_DAY, d.perUserPerDay),
    globalPerDay: envInt(env.PREP_INSTANT_GLOBAL_PER_DAY, d.globalPerDay),
    globalPerMinute: envInt(env.PREP_INSTANT_GLOBAL_PER_MINUTE, d.globalPerMinute),
  };
}

function seededRandoms(): Randoms {
  return { instant: new SeededRandom(PARITY_SEED), merge: new SeededRandom(PARITY_SEED + 1), tokens: new SeededRandom(PARITY_SEED + 2) };
}

export function compose(env: Env): Composition {
  const memo = compositions.get(env);
  if (memo) return memo;
  refusePinsOutsideParityHosts(env);
  const parity = env.PREP_PARITY_MODE === '1';
  const clock = clockFromEnv(env);
  const webRandom = new WebCryptoRandom();
  const composition: Composition = {
    clock,
    identity: parity ? new FakeIdentityProvider() : new NoIdentityProvider(),
    renderer: createRenderer({ clock, root: '' }),
    pages: fixturePagesFromBuild(),
    buildToken: resolveBuildToken(env.PREP_BUILD_ID),
    parity,
    internalToken: env.PREP_INTERNAL_TOKEN ?? '',
    authProvider: parity ? 'tailscale' : 'clerk',
    randoms: parity ? seededRandoms() : { instant: webRandom, merge: webRandom, tokens: webRandom },
    fuzz: parity ? false : { random: () => webRandom.bytes(4).reduce((acc, b) => acc * 256 + b, 0) / 2 ** 32 },
    hasher: new WebCryptoHasher(),
    limits: limitsFromEnv(env),
    directory: namespaceDirectory(env.DIRECTORY),
    limiter: namespaceLimiter(env.INSTANT_LIMITER),
    userCells: namespaceUserCells(env.USER),
    userRepos: (storage, requestClock) =>
      userRepos(storage, { clock: requestClock, sessionIds: composition.sessionIds(storage), random: composition.randoms.instant, fuzz: composition.fuzz }),
    sessionIds: (storage) =>
      parity
        ? new ParitySessionIds({
            get: async () => (await storage.get<number>(SESSION_COUNTER_KEY)) ?? 0,
            set: (n) => storage.put(SESSION_COUNTER_KEY, n),
          })
        : new RandomSessionIds(webRandom),
    directoryRepo: (storage) => new SqlDirectoryRepo(storage),
    limiterRepo: (storage) => new SqlLimiterRepo(storage, composition.limits),
    resetRandom: () => {
      if (parity) composition.randoms = seededRandoms();
    },
    migrateUserCell: (storage) => migrate(storage.sql, USER_MIGRATIONS),
    migrateDirectory: (storage) => migrate(storage.sql, DIRECTORY_MIGRATIONS),
    migrateLimiter: (storage) => migrate(storage.sql, LIMITER_MIGRATIONS),
    seedIdBlock: (storage, idx) => seedSequences(storage.sql, idx),
    resetIdBlock: (storage) => resetSequences(storage.sql),
  };
  compositions.set(env, composition);
  return composition;
}

/** The clock a request runs on: the parity instant it carries, else the composition's. */
export function clockFor(c: Composition, request: Request): Clock {
  const raw = request.headers.get(NOW_HEADER);
  if (!c.parity || !raw) return c.clock;
  return new FixedClock(parseFakeNow(raw));
}

/** `Cache-Control: no-cache` on every HTML response, as the Python
 * middleware does; other content types pass untouched. */
export function noCacheHtml(res: Response): Response {
  if (!(res.headers.get('content-type') ?? '').startsWith('text/html')) return res;
  const out = new Response(res.body, res);
  out.headers.set('cache-control', 'no-cache');
  return out;
}

/** The response-path hook the anonymous cookie takes in phase 3; the
 * identity function until then. */
export function cookieHooks(_req: Request, res: Response): Response {
  return res;
}

/** The composition with some ports replaced, memoized for `env`: how a test
 * hands a cell or the router a spy through the same root. */
export function composeWith(env: Env, overrides: Partial<Composition>): Composition {
  const composition = { ...compose(env), ...overrides };
  compositions.set(env, composition);
  return composition;
}
