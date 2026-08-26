// The composition root: the only place adapters meet ports, memoized per
// isolate. Cross-cutting wrappers live here and are applied by the router,
// never inside a handler. Cells get their repositories through it too.
import type {
  AgentConfig,
  AgentPort,
  Cipher,
  Clock,
  Directory,
  FixturePages,
  Hasher,
  IdentityProvider,
  JobCells,
  JobLedger,
  LedgerReset,
  Limiter,
  Random,
  Renderer,
  SessionIds,
  Signer,
  Sync,
  UserCells,
  UserRepos,
  WebhookVerifier,
  WebPush,
  WorkflowRunner,
} from '../app/ports.js';
import { JOB_GRAPHS } from '../app/jobs/graph.js';
import { registerWorkflowSteps } from '../app/jobs/index.js';
import { StepRegistry } from '../app/jobs/registry.js';
import type { StepGraph } from '../domain/jobs/graph.js';
import type { AuthUrls } from '../app/pageContext.js';
import type { OpenRouterAuth } from '../app/settings/openrouter.js';
import type { Fuzz } from '../domain/fsrs/index.js';
import { DEFAULT_LIMITS, type Limits } from '../domain/instant/limiter.js';
import type { CookieVerdict } from '../app/auth/resolve.js';
import { deleteCookieHeader, HmacSigner, mintCookie, resolveCookieSecret, setCookieHeader } from './adapters/anonCookie.js';
import { AesGcmCipher, loadMasterKey, MasterKeyError } from './adapters/byokCrypto.js';
import { AlarmLedgerRunner } from './adapters/alarmLedgerRunner.js';
import { NO_FUNDING } from '../app/agent/funding.js';
import { StubWorkflowRunner } from './adapters/runnerStub.js';
import { namespaceDirectory, namespaceJobs, namespaceLimiter, namespaceUserCells, NO_RETRY } from './adapters/cells.js';
import { PROBE_GRAPHS, registerProbe } from './adapters/jobProbe.js';
import { ClerkConfigError, ClerkProvider, ClerkVerifier, clerkConfig, frontendApiHost, type ClerkConfig } from './adapters/clerk.js';
import { clockFromEnv, FixedClock, parseFakeNow } from './adapters/clock.js';
import { FakeIdentityProvider, NoIdentityProvider } from './adapters/fakeIdentity.js';
import { FreeTierAgent, freeTierConfig, INSTANT_MAX_OUTPUT_TOKENS } from './adapters/agents/freeTier.js';
import { RefusingAgent, SelectedAgent, type SelectDeps } from './adapters/agents/select.js';
import { NoWebPush, WebCryptoWebPush } from './adapters/webpush.js';
import { OpenRouterOAuth } from './adapters/openrouter.js';
import { SvixVerifier } from './adapters/svix.js';
import { PatIssuer } from './adapters/pat.js';
import { fixturePagesFromBuild } from './adapters/fixturePages.js';
import { WebCryptoHasher } from './adapters/hash.js';
import { createRenderer } from './adapters/nunjucks/index.js';
import { ParitySessionIds, RandomSessionIds, SeededRandom, WebCryptoRandom } from './adapters/random.js';
import {
  DIRECTORY_MIGRATIONS,
  JOB_MIGRATIONS,
  LIMITER_MIGRATIONS,
  migrate,
  resetSequences,
  seedSequences,
  SqlDirectoryRepo,
  SqlJobLedger,
  SqlLimiterRepo,
  USER_MIGRATIONS,
  userRepos,
} from './adapters/sql/index.js';
import type { CellStorage } from './storage.js';
import { resolveBuildToken } from './buildToken.js';
import type { Env, InstantLimitEnv, PublicServiceVars } from './env.js';

/** celld's default outbound fetch timeout, in seconds. */
const DEFAULT_FETCH_TIMEOUT_S = 120;
const DEFAULT_JOB_LLM_TIMEOUT_S = 300;
/** Room for the adapter to turn a refused fetch into a step failure. */
const FETCH_HEADROOM_S = 5;

/** The seed of the parity harness (tests/parity/oracles/harness.py). */
export const PARITY_SEED = 20260314;
/** The IANA `sub` claim a push service contacts about operational issues. */
export const DEFAULT_VAPID_SUB = 'mailto:noreply@example.com';
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
  /** Null when no signing secret resolves: anonymous accounts are off. */
  signer(): Promise<Signer | null>;
  /** Null without a master key: the BYOK surfaces answer 503. */
  cipher: Cipher | null;
  clerk: ClerkProvider | null;
  clerkConfig: ClerkConfig | null;
  /** Null without CLERK_WEBHOOK_SECRET: the receiver answers 503. */
  webhooks: WebhookVerifier | null;
  pat: PatIssuer;
  openRouter: OpenRouterAuth;
  /** The instant endpoint's agent: the shared tier at its own output cap,
   * else the refusing stub. Every other caller selects per owner. */
  agent: AgentPort;
  /** The port a cell holds for one owner: the credential is resolved per
   * call, so a revoked key stops the call after it. */
  agentFor(load: () => AgentConfig | Promise<AgentConfig>, opts?: { timeoutMs?: number }): AgentPort;
  /** One runner per calling cell: `status` reads that cell's `job_progress`,
   * and a status write lands through that cell's repositories and push. */
  runner(ctx: { owner: string; repos: UserRepos }): WorkflowRunner;
  jobCells: JobCells;
  /** The job kinds this deploy can run, by kind name. */
  jobGraphs: Readonly<Record<string, StepGraph>>;
  /** Where a step name resolves to its handler; lane B registers into it. */
  stepRegistry: StepRegistry;
  /** Ceiling on one LLM step, under the deploy's outbound fetch timeout. */
  jobLlmTimeoutMs: number;
  jobLedger(storage: CellStorage): JobLedger;
  webPush: WebPush;
  /** Whether the shared tier would actually serve, not whether vars exist. */
  freeTierConfigured: boolean;
  vapidPublicKey: string;
  /** What a page embeds for the sign-in chrome and the ClerkJS bootstrap. */
  authUrls: AuthUrls;
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
  /** The same cells with no inline retry, for a caller whose retries are rows:
   * the JobCell backs off through its ledger and its alarm instead. */
  userCellsDirect: UserCells;
  userRepos(storage: CellStorage, clock: Clock): UserRepos;
  sessionIds(storage: CellStorage): SessionIds;
  directoryRepo(storage: CellStorage): Sync<Directory>;
  limiterRepo(storage: CellStorage): Sync<Limiter> & LedgerReset;
  /** Re-seeds the parity generators; a no-op outside parity. */
  resetRandom(): void;
  /** Each cell class's migrations, idempotent, version written last. */
  migrateUserCell(storage: CellStorage): number;
  migrateDirectory(storage: CellStorage): number;
  migrateLimiter(storage: CellStorage): number;
  migrateJobCell(storage: CellStorage): number;
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

/** Empty strings, not nulls: the templates test truthiness on these. */
function authUrlsOf(clerk: ClerkProvider | null): AuthUrls {
  if (!clerk) return { signIn: '', signUp: '', signOut: '', clerkPublishableKey: null, clerkFrontendApiHost: null };
  const urls = clerk.urls();
  return {
    signIn: urls.sign_in ?? '',
    signUp: urls.sign_up ?? '',
    signOut: urls.sign_out ?? '',
    clerkPublishableKey: clerk.publishableKey || null,
    clerkFrontendApiHost: frontendApiHost(clerk.publishableKey),
  };
}

/** Clerk only outside parity, and only when its five vars are all set: a
 * half-configured provider must not silently become "nobody is signed in". */
function clerkOrNull(env: Env, clock: Clock): { provider: ClerkProvider; config: ClerkConfig } | null {
  if (env.PREP_PARITY_MODE === '1') return null;
  if (!(env.CLERK_ISSUER ?? '').trim()) return null;
  let config: ClerkConfig;
  try {
    config = clerkConfig(env);
  } catch (e) {
    if (e instanceof ClerkConfigError) throw e;
    throw e;
  }
  return { provider: new ClerkProvider(config, new ClerkVerifier(config, clock)), config };
}

/** The master key, or null with the reason logged: a deploy without one
 * keeps working, minus BYOK and minus anonymous accounts. */
function cipherOrNull(env: Env, random: Random, warn: (msg: string) => void): Cipher | null {
  if (!(env.PREP_KEY_ENCRYPTION_SECRET ?? '').trim()) return null;
  try {
    return new AesGcmCipher(loadMasterKey(env), random);
  } catch (e) {
    if (e instanceof MasterKeyError) {
      warn(`${e.message} BYOK is disabled.`);
      return null;
    }
    throw e;
  }
}

/** The real sender once both VAPID halves are set; a deploy without them
 * keeps working, minus delivery. */
function webPushOf(env: Env, clock: Clock, warn: (msg: string) => void): WebPush {
  const publicKey = (env.PREP_VAPID_PUBLIC_KEY ?? '').trim();
  const privateKey = (env.PREP_VAPID_PRIVATE_KEY ?? '').trim();
  // Half a key pair is the shape a deploy lands in when only one of the two
  // reaches it, and the browser's subscribe handshake reads the public one
  // from an endpoint that answers an empty string either way.
  if (Boolean(publicKey) !== Boolean(privateKey)) {
    warn(`web push is disabled: PREP_VAPID_${publicKey ? 'PRIVATE' : 'PUBLIC'}_KEY is not set.`);
  }
  if (!publicKey || !privateKey) return new NoWebPush();
  const subject = (env.PREP_VAPID_SUB ?? '').trim() || DEFAULT_VAPID_SUB;
  return new WebCryptoWebPush({ publicKey, privateKey, subject }, () => clock.now());
}

/**
 * The handlers, registered once per isolate: module-level state is shared by
 * every cell of a node, which is right for code and wrong for anything
 * per-cell. The probe graphs only exist under parity mode.
 */
function stepRegistryFor(parity: boolean): StepRegistry {
  const registry = new StepRegistry();
  registerWorkflowSteps(registry);
  if (parity) registerProbe(registry);
  return registry;
}

/** The Go worker allowed 30m per activity; a celld fetch is bounded by
 * `CELLD_FETCH_TIMEOUT_S`, so a step gets the smaller of the two minus the
 * adapter's headroom. A timeout is a step failure, not an extension. */
export function jobLlmTimeoutMs(env: PublicServiceVars & { PREP_JOB_LLM_TIMEOUT_S?: string }): number {
  const fetchCeiling = envInt(env.CELLD_FETCH_TIMEOUT_S, DEFAULT_FETCH_TIMEOUT_S);
  const wanted = envInt(env.PREP_JOB_LLM_TIMEOUT_S, DEFAULT_JOB_LLM_TIMEOUT_S);
  return Math.max(1, Math.min(wanted, fetchCeiling - FETCH_HEADROOM_S)) * 1000;
}

export function compose(env: Env, warn: (msg: string) => void = console.warn): Composition {
  const memo = compositions.get(env);
  if (memo) return memo;
  refusePinsOutsideParityHosts(env);
  const parity = env.PREP_PARITY_MODE === '1';
  const clock = clockFromEnv(env);
  const webRandom = new WebCryptoRandom();
  const clerk = clerkOrNull(env, clock);
  const freeTier = freeTierConfig(env, { warn });
  const instantTier = freeTierConfig(env, { maxTokens: INSTANT_MAX_OUTPUT_TOKENS, warn });
  const selectDeps: SelectDeps = { env, cipher: cipherOrNull(env, webRandom, warn), warn };
  let signerOnce: Promise<Signer | null> | null = null;
  const composition: Composition = {
    clock,
    identity: parity ? new FakeIdentityProvider(env.PREP_INTERNAL_TOKEN ?? '') : (clerk?.provider ?? new NoIdentityProvider()),
    signer: () => {
      signerOnce ??= resolveCookieSecret(env, warn).then((secret) => (secret ? new HmacSigner(secret) : null));
      return signerOnce;
    },
    cipher: selectDeps.cipher,
    clerk: clerk?.provider ?? null,
    clerkConfig: clerk?.config ?? null,
    webhooks: (env.CLERK_WEBHOOK_SECRET ?? '').trim() ? new SvixVerifier((env.CLERK_WEBHOOK_SECRET ?? '').trim()) : null,
    pat: new PatIssuer(
      { bytes: (n) => composition.randoms.tokens.bytes(n), choice: (seq) => composition.randoms.tokens.choice(seq) },
      new WebCryptoHasher(),
    ),
    openRouter: new OpenRouterOAuth(webRandom),
    agent: instantTier ? new FreeTierAgent(instantTier, warn) : new RefusingAgent(NO_FUNDING),
    agentFor: (load, opts = {}) => new SelectedAgent(load, { ...selectDeps, timeoutMs: opts.timeoutMs ?? selectDeps.timeoutMs }),
    // A deploy with no JOB binding has jobs off: every start refuses, and
    // the use cases take the branch Python takes when nothing funds one.
    runner: (ctx) =>
      env.JOB === undefined
        ? new StubWorkflowRunner()
        : new AlarmLedgerRunner({
            jobs: composition.jobCells,
            notify: { repos: ctx.repos, webPush: composition.webPush, vapidPublicKey: composition.vapidPublicKey },
            owner: ctx.owner,
            random: composition.randoms.tokens,
            clock,
          }),
    jobCells: namespaceJobs(env.JOB),
    jobGraphs: parity ? { ...JOB_GRAPHS, ...PROBE_GRAPHS } : { ...JOB_GRAPHS },
    stepRegistry: stepRegistryFor(parity),
    jobLlmTimeoutMs: jobLlmTimeoutMs(env),
    jobLedger: (storage) => new SqlJobLedger(storage),
    webPush: webPushOf(env, clock, warn),
    freeTierConfigured: freeTier !== null,
    vapidPublicKey: (env.PREP_VAPID_PUBLIC_KEY ?? '').trim(),
    authUrls: authUrlsOf(clerk?.provider ?? null),
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
    userCellsDirect: namespaceUserCells(env.USER, NO_RETRY),
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
    migrateJobCell: (storage) => migrate(storage.sql, JOB_MIGRATIONS),
    seedIdBlock: (storage, idx) => seedSequences(storage.sql, idx),
    resetIdBlock: (storage) => resetSequences(storage.sql),
  };
  compositions.set(env, composition);
  return composition;
}

/** The clock a request runs on: the parity instant it carries, else the
 * composition's. Both spellings are read, because the entry worker's response
 * hooks see the inbound request (`x-parity-now`) while a cell sees the
 * forwarded one (`x-prep-now`); one clock per request either way. */
export function clockFor(c: Composition, request: Request): Clock {
  if (!c.parity) return c.clock;
  const raw = request.headers.get(NOW_HEADER) ?? request.headers.get(PARITY_NOW_HEADER);
  if (!raw) return c.clock;
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

/** The header a handler sets to ask for a cookie it could not write itself:
 * `mint=<id>` for a freshly created account, `clear` to forget one. */
export const ANON_COOKIE_HEADER = 'x-prep-anon-cookie';

/**
 * What `resolve` could only record, applied on the way out: it has no
 * response. Runs for every response, not just HTML, or a JSON request never
 * clears a dead cookie and never refreshes a live one.
 *
 * Precedence is Python's. A `mint` supersedes both pending updates: the new
 * value is the account the response just handed out, and a stale-cookie
 * delete emitted afterwards would erase it. A `clear` from forget-device,
 * sign-out or a tombstoned cell wins over a refresh for the same reason.
 */
export async function cookieHooks(c: Composition, request: Request, verdict: CookieVerdict, res: Response): Promise<Response> {
  const asked = res.headers.get(ANON_COOKIE_HEADER);
  const secure = new URL(request.url).protocol === 'https:' || (request.headers.get('x-forwarded-proto') ?? '').split(',')[0]?.trim() === 'https';
  const now = clockFor(c, request).now();
  const signer = await c.signer();
  let header: string | null = null;
  if (asked?.startsWith('mint=') && signer) {
    header = setCookieHeader(await mintCookie(signer, asked.slice('mint='.length), Math.floor(now.getTime() / 1000)), secure);
  } else if (asked === 'clear' || verdict.kind === 'stale') {
    header = deleteCookieHeader(now, secure);
  } else if (verdict.kind === 'refresh' && signer) {
    header = setCookieHeader(await mintCookie(signer, verdict.externalId, Math.floor(now.getTime() / 1000)), secure);
  }
  if (header === null && asked === null) return res;
  const out = new Response(res.body, res);
  out.headers.delete(ANON_COOKIE_HEADER);
  if (header !== null) out.headers.append('set-cookie', header);
  return out;
}

/** The composition with some ports replaced, memoized for `env`: how a test
 * hands a cell or the router a spy through the same root. */
export function composeWith(env: Env, overrides: Partial<Composition>): Composition {
  const composition = { ...compose(env), ...overrides };
  compositions.set(env, composition);
  return composition;
}
