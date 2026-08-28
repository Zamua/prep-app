// Entry worker: a translation layer. Unauthenticated pages render here, the
// provider flows and the Clerk webhook live here, and an identified request
// is forwarded to its UserCell with the identity asserted in headers.
import { isRefusal } from '../domain/jobs/refusal.js';
import { appBase } from './appBase.js';
import { ANON_COOKIE_HEADER, clockFor, compose, cookieHooks, noCacheHtml, NOW_HEADER, TEST_NOW_HEADER, type Composition } from './compose.js';
import type { Env } from './env.js';
import { anonymousContext, errorPage, htmlResponse } from './errors.js';
import { serveStatic } from './assets.js';
import { offlineBuild, servePwa } from './sw.js';
import { NOT_AUTHENTICATED, UnknownProfile } from './cells/UserCell.js';
import {
  DISPLAY_NAME_HEADER,
  EMAIL_HEADER,
  IDENTITY_HEADERS,
  KIND_HEADER,
  matchRoute,
  PAT_HASH_HEADER,
  PICTURE_HEADER,
  SUBJECT_HEADER,
  wantsJson,
} from './cells/router.js';
import { pageRoutes } from './cells/routes/pages.js';
import { apiRoutes } from './cells/routes/api.js';
import { resolveIdentity, type CookieVerdict, type Resolution } from '../app/auth/resolve.js';
import { hexFrom, mergeAnonymous } from '../app/auth/mergeSaga.js';
import type { CellSnapshot } from '../app/entities.js';
import type { Identity } from '../app/ports.js';
import { landingContext } from '../app/landing.js';
import { COOKIE_NAME } from '../domain/anonCookie.js';
import { parseCookieHeader } from '../domain/cookies.js';
import { bearerValue, parseToken, TOKEN_PREFIX, BAD_TOKEN } from '../domain/pat.js';
import { isoUtc } from '../domain/time.js';
import { clerkWebhook } from './webhooks.js';
import { serveInstant } from './routes/instant.js';
import { observe, serveMetrics } from './routes/metrics.js';
import { servePublic } from './routes/openapi.js';
import { serveMigrate } from './routes/migrate.js';
import { serveMigrateDump } from './routes/migrateDump.js';
import { serveTestJobs } from './routes/testJobs.js';

export { UserCell } from './cells/UserCell.js';
export { DirectoryCell } from './cells/DirectoryCell.js';
export { InstantLimiterCell } from './cells/InstantLimiterCell.js';
export { JobCell } from './cells/JobCell.js';

/** The identity a cell receives; the display name is URI-encoded because a
 * header value is a byte string. */
export { SUBJECT_HEADER, DISPLAY_NAME_HEADER };
export const INTERNAL_TOKEN_HEADER = 'x-internal-token';
/** The shell's escape hatch: it sets this and reloads when recovery fails,
 * so the landing page is reachable and the reauth loop cannot close. */
export const REAUTH_FALLBACK_COOKIE = 'prep_reauth_fallback';

/** Bearer-only surfaces: with no token there is no cell to route to, so the
 * refusal is the entry worker's rather than a 404 from nowhere. */
const PAT_ONLY = (path: string): boolean => path.startsWith('/api/v1/') || path === '/api/v1' || path === '/mcp';

interface SeedApi {
  wipe(profile: string): Promise<void>;
  seed(profile: string, user: string, at: string | null): Promise<Record<string, unknown>>;
  dump(): Promise<CellSnapshot>;
}

/** The global ledger's own reset, which the seed drives. */
interface LimiterSeedApi {
  wipe(): Promise<void>;
}

/** One job cell's reset, which the seed drives. */
interface JobSeedApi {
  wipe(): Promise<void>;
}

/** The directory's own rows, which only the test-only dump reads. */
interface DirectorySeedApi {
  dumpTables(): Promise<Record<string, Record<string, unknown>[]>>;
}

/** What the response owes the anonymous cookie, decided before any handler. */
interface Outcome {
  response: Response;
  cookie: CookieVerdict;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    // Before composition, so a node that cannot configure itself still
    // reports, and outside the timing below, so the scraper is not most of
    // what the histogram holds.
    const scrape = serveMetrics(request, url);
    if (scrape) return scrape;
    // The runtime clock advances on I/O, so a request that does none records
    // as zero. Everything reaching storage or a cell is timed.
    const started = Date.now();
    let observed: Response | undefined;
    try {
      observed = await handle(request, url, env);
      return observed;
    } finally {
      // A thrown request is a 500, the way the reference middleware records
      // the response it never got.
      observe(request.method, url.pathname, observed?.status ?? 500, (Date.now() - started) / 1000);
    }
  },
};

async function handle(request: Request, url: URL, env: Env): Promise<Response> {
  // Liveness is independent of storage and configuration: a wedged process
  // should restart, a storage blip should not restart every node.
  if (url.pathname === '/healthz') return new Response('ok');
  let c: Composition;
  try {
    c = compose(env);
  } catch (e) {
    console.error(`compose failed: ${e instanceof Error ? e.message : e}`);
    return new Response('service misconfigured', { status: 500 });
  }
  // Readiness composes: a misconfigured worker must not take traffic.
  if (url.pathname === '/readyz') return new Response('ok');
  let cookie: CookieVerdict = { kind: 'none' };
  try {
    // A refusal is the runtime declining the work, not the work failing:
    // nothing was half-written, so the request is safe to run again. A
    // single-replica fleet answers one during a lease renewal or a
    // durability wait, and without this the user reads a 500 for a write
    // that never happened. Bounded, and only for a refusal.
    let outcome: Awaited<ReturnType<typeof route>> | undefined;
    for (let attempt = 0; ; attempt++) {
      try {
        outcome = await route(request, url, env, c);
        break;
      } catch (err) {
        if (!isRefusal(err) || attempt >= 2) throw err;
        console.error(`retrying ${request.method} ${url.pathname} after a refusal: ${err instanceof Error ? err.message : err}`);
        await new Promise((r) => setTimeout(r, 120 * (attempt + 1)));
      }
    }
    cookie = outcome!.cookie;
    return await cookieHooks(c, request, cookie, noCacheHtml(outcome!.response));
  } catch (e) {
    console.error(`unhandled exception on ${request.method} ${url.pathname}: ${e instanceof Error ? (e.stack ?? e.message) : e}`);
    return await cookieHooks(c, request, cookie, noCacheHtml(errorPage(c.renderer, c.buildToken, 500, request)));
  }
}

const plain = (response: Response): Outcome => ({ response, cookie: { kind: 'none' } });

async function route(request: Request, url: URL, env: Env, c: Composition): Promise<Outcome> {
  const path = url.pathname;
  const base = appBase(request);
  const page = (template: string, extra: Record<string, unknown> = {}, status = 200) =>
    htmlResponse(c.renderer.render(template, { ...anonymousContext(c.buildToken, base, c.authUrls), ...extra }), status);
  const readOnly = request.method === 'GET' || request.method === 'HEAD';
  const methodNotAllowed = () => errorPage(c.renderer, c.buildToken, 405, request, 'Method Not Allowed');

  const asset = await serveStatic(request, env, c.buildToken);
  if (asset) return plain(asset);
  const pwa = servePwa(url, c.buildToken);
  if (pwa) return plain(pwa);
  if (path === '/offline') return plain(readOnly ? page('offline.html', { build: offlineBuild(url, c.buildToken) }) : methodNotAllowed());
  if (path === '/privacy') return plain(readOnly ? page('privacy.html') : methodNotAllowed());
  const publicApi = servePublic(request, url, { testMode: c.testMode, vapidPublicKey: c.vapidPublicKey });
  if (publicApi) return plain(publicApi);
  // Signed by svix, not by any user credential, so it precedes identification.
  if (path === '/webhooks/clerk') return plain(request.method === 'POST' ? await clerkWebhook(request, c) : methodNotAllowed());
  // Outside the test-mode block on purpose: the migration runs where the data
  // goes. Its own token gate and the directory's seal are what bound it.
  const migration = (await serveMigrate(request, url, c)) ?? (await serveMigrateDump(request, url, c));
  if (migration) return plain(migration);

  if (c.testMode) {
    if (request.method === 'GET' && path === '/_test/raise') {
      return plain(
        url.searchParams.get('status') === '429'
          ? errorPage(c.renderer, c.buildToken, 429, request, 'test: deliberate throttle')
          : errorPage(c.renderer, c.buildToken, 500, request),
      );
    }
    if (request.method === 'GET' && path === '/_test/reauth') return plain(page('reauth.html'));
    if (request.method === 'GET' && path === '/_test/sign-out') {
      return plain(page('sign_out_interstitial.html', { redirect_url: '/' }));
    }
    if (request.method === 'POST' && path === '/_test/seed') return plain(await seed(request, env, c));
    if (request.method === 'GET' && path === '/_test/dump') return plain(await dumpCell(request, url, env, c));
    const job = await serveTestJobs(request, url, env, c, isoUtc(clockFor(c, request).now()));
    if (job) return plain(job);
  }

  // Inbound copies of the identity headers are stripped before anything
  // reads them: only the router may assert an identity to a cell. In test
  // mode the request clock travels the same way.
  const headers = new Headers(request.headers);
  for (const name of IDENTITY_HEADERS) headers.delete(name);
  const testNow = c.testMode ? request.headers.get(TEST_NOW_HEADER) : null;
  if (testNow) headers.set(NOW_HEADER, testNow);
  const clock = clockFor(c, new Request(request.url, { headers }));

  // A personal access token names its owner, so it routes without any
  // provider and without a directory read.
  if (PAT_ONLY(path) || isPatBearer(request)) return plain(await routeByToken(request, headers, env, c));

  const cookies = parseCookieHeader(request.headers.get('cookie'));
  // Identification reads headers only. The body is a one-shot stream, so
  // the request is rebuilt exactly once, below, for the cell.
  const resolution = await resolveIdentity(new Request(request.url, { method: request.method, headers }), {
    provider: c.identity,
    signer: await c.signer(),
    nowUnix: Math.floor(clock.now().getTime() / 1000),
    cookieValue: cookies[COOKIE_NAME] ?? null,
  });

  const flows = await providerFlows(request, url, c, resolution, cookies, page);
  if (flows) return flows;

  if (path === '/api/instant/generate') {
    if (request.method !== 'POST') return plain(methodNotAllowed());
    return { response: await instantGenerate(request, env, c, resolution), cookie: resolution.cookie };
  }

  if (!resolution.identity) return visitorResponse(request, url, c, resolution, cookies, page);

  let cookie = resolution.cookie;
  if (resolution.merge) cookie = await runMerge(c, resolution.identity, resolution.merge, clock.now(), cookie);

  headers.set(SUBJECT_HEADER, resolution.identity.subject);
  headers.set(KIND_HEADER, resolution.identity.kind);
  if (resolution.identity.displayName) headers.set(DISPLAY_NAME_HEADER, encodeURIComponent(resolution.identity.displayName));
  if (resolution.identity.email) headers.set(EMAIL_HEADER, encodeURIComponent(resolution.identity.email));
  if (resolution.identity.profilePicUrl) headers.set(PICTURE_HEADER, encodeURIComponent(resolution.identity.profilePicUrl));
  const stub = env.USER.get(env.USER.idFromName(resolution.identity.subject));
  const response = await stub.fetch(new Request(request, { headers }));
  return { response: await onTombstone(response, request, c, resolution.identity), cookie };
}

/** The one endpoint a visitor may spend on: the limiter, the directory and
 * the mint are the entry worker's because a visitor has no cell yet. */
async function instantGenerate(request: Request, env: Env, c: Composition, resolution: Resolution): Promise<Response> {
  const identity = resolution.identity;
  return serveInstant(
    request,
    {
      clock: clockFor(c, request),
      random: c.randoms.instant,
      limiter: c.limiter,
      directory: c.directory,
      cells: c.userCells,
      agent: c.freeTierConfigured ? c.agent : null,
      anonymousEnabled: (await c.signer()) !== null,
      PREP_CLIENT_IP_HEADER: env.PREP_CLIENT_IP_HEADER,
    },
    { userId: identity?.subject ?? null, userIsAnonymous: identity ? identity.kind === 'anon' : null },
  );
}

// ---- provider flows -------------------------------------------------------

type PageFn = (template: string, extra?: Record<string, unknown>, status?: number) => Response;

/**
 * Whether the browser says this request came from our own pages. Guards the
 * two responses that clear `prep_anon`: for an anonymous account the cookie
 * is the only credential, and SameSite=Lax stops it riding a cross-site
 * request but not the response's delete from applying. Every browser that
 * would honour that delete sends `Sec-Fetch-Site`, so its absence means a
 * non-browser caller and is judged by `Origin` instead.
 */
export function sameOrigin(request: Request): boolean {
  const site = request.headers.get('sec-fetch-site');
  if (site !== null) return site === 'same-origin' || site === 'none';
  const origin = request.headers.get('origin');
  if (origin === null) return true;
  const scheme = origin.indexOf('//');
  // The URL is the authority the runtime built from Host; a request assembled
  // in-process carries no Host header at all, and comparing against '' would
  // read every same-origin post as cross-site.
  return (scheme === -1 ? origin : origin.slice(scheme + 2)) === (request.headers.get('host') ?? new URL(request.url).host);
}

async function providerFlows(
  request: Request,
  url: URL,
  c: Composition,
  resolution: Resolution,
  cookies: Record<string, string>,
  page: PageFn,
): Promise<Outcome | null> {
  const urls = c.identity.urls();
  if (url.pathname === '/sign-in') {
    if (request.method !== 'GET') return plain(errorPage(c.renderer, c.buildToken, 405, request, 'Method Not Allowed'));
    if (!urls.sign_in) return plain(errorPage(c.renderer, c.buildToken, 404, request, 'this deploy has no in-app sign-in flow'));
    return { response: new Response(null, { status: 303, headers: { location: urls.sign_in } }), cookie: resolution.cookie };
  }
  if (url.pathname === '/sign-out') {
    if (request.method !== 'GET') return plain(errorPage(c.renderer, c.buildToken, 405, request, 'Method Not Allowed'));
    if (!urls.sign_out) return plain(errorPage(c.renderer, c.buildToken, 404, request, 'this deploy has no in-app sign-out flow'));
    // ClerkJS owns session revocation: it clears cookies across the apex and
    // Clerk's own host and broadcasts to other tabs, and the hosted
    // `/sign-out` URL is not a page. The anonymous cookie goes too, or the
    // browser resolves straight back into the pre-signup account.
    const response =
      c.identity.name === 'clerk'
        ? page('sign_out_interstitial.html', { redirect_url: '/' })
        : new Response(null, { status: 303, headers: { location: urls.sign_out } });
    if (sameOrigin(request)) response.headers.set(ANON_COOKIE_HEADER, 'clear');
    return { response, cookie: resolution.cookie };
  }
  if (url.pathname === '/forget-device') {
    if (request.method !== 'POST') return plain(errorPage(c.renderer, c.buildToken, 405, request, 'Method Not Allowed'));
    if (!sameOrigin(request)) {
      const detail = 'cross-site request';
      return plain(wantsJson(request) ? Response.json({ detail }, { status: 403 }) : errorPage(c.renderer, c.buildToken, 403, request, detail));
    }
    // Deletes nothing: the account and its decks survive and age out on the
    // reaper's schedule. A request with no cookie is a no-op that still
    // lands on the landing page.
    const response = new Response(null, { status: 303, headers: { location: '/' } });
    response.headers.set(ANON_COOKIE_HEADER, 'clear');
    return { response, cookie: resolution.cookie };
  }
  return null;
}

/** No identity: the landing page, the session-restoring shell, or a refusal. */
function visitorResponse(request: Request, url: URL, c: Composition, resolution: Resolution, cookies: Record<string, string>, page: PageFn): Outcome {
  const cookie = resolution.cookie;
  if (request.method === 'GET' && url.pathname === '/') {
    // A returning user whose short-lived token expired must not be flashed
    // the marketing page: the shell recovers the session and reloads.
    if (resolution.dormant && cookies[REAUTH_FALLBACK_COOKIE] !== '1') return { response: page('reauth.html'), cookie };
    return { response: page('landing.html', landingContext(c)), cookie };
  }
  // A route that exists but needs an identity is 401; anything else never
  // existed, and saying so is not a signal worth withholding.
  if (!matchRoute([...pageRoutes, ...apiRoutes], request.method, url.pathname)) {
    return { response: errorPage(c.renderer, c.buildToken, 404, request, 'Not Found'), cookie };
  }
  if (wantsJson(request)) return { response: Response.json({ detail: NOT_AUTHENTICATED }, { status: 401 }), cookie };
  const signIn = c.identity.urls().sign_in;
  if (signIn) return { response: new Response(null, { status: 303, headers: { location: signIn } }), cookie };
  return { response: errorPage(c.renderer, c.buildToken, 401, request, NOT_AUTHENTICATED), cookie };
}

/** A tombstoned cell answers 410; the browser is told what that means. */
async function onTombstone(response: Response, request: Request, c: Composition, identity: Identity): Promise<Response> {
  if (response.status !== 410 || !response.headers.has('x-prep-tombstoned')) return response;
  if (identity.kind !== 'anon') return errorPage(c.renderer, c.buildToken, 404, request, 'Not Found');
  const out = wantsJson(request)
    ? Response.json({ detail: NOT_AUTHENTICATED }, { status: 401 })
    : errorPage(c.renderer, c.buildToken, 401, request, NOT_AUTHENTICATED);
  out.headers.set(ANON_COOKIE_HEADER, 'clear');
  return out;
}

/**
 * The merge, after the target's row exists and never failing the request.
 * Every route reaches this, so an uncaught exception here would turn every
 * authenticated request carrying an anonymous cookie into a 500; a tripped
 * guard keeps the cookie and retries on the next one.
 */
async function runMerge(c: Composition, identity: Identity, anonId: string, now: Date, fallback: CookieVerdict): Promise<CookieVerdict> {
  try {
    const at = isoUtc(now);
    const { idx } = await c.directory.register(identity.subject, false, at);
    await c.userCells
      .cell(identity.subject)
      .upsert(identity.subject, { email: identity.email, displayName: identity.displayName, profilePicUrl: identity.profilePicUrl }, at, idx);
    const result = await mergeAnonymous(anonId, identity.subject, {
      cells: c.userCells,
      jobs: c.jobCells,
      directory: c.directory,
      limiter: c.limiter,
      clock: { now: () => now },
      randomHex: hexFrom(c.randoms.merge),
    });
    return result.resolved ? { kind: 'stale' } : fallback;
  } catch (e) {
    console.error(`anon merge failed: anon=${anonId}: ${e instanceof Error ? e.message : e}`);
    return fallback;
  }
}

// ---- personal access tokens ----------------------------------------------

function isPatBearer(request: Request): boolean {
  const value = bearerValue(request.headers.get('authorization'));
  return 'token' in value && value.token.trim().startsWith(TOKEN_PREFIX);
}

/**
 * Bearer resolution split across the hop: the refusals that need no storage
 * answer here, and the hash is checked by the owner's own cell.
 */
async function routeByToken(request: Request, headers: Headers, env: Env, c: Composition): Promise<Response> {
  const value = bearerValue(request.headers.get('authorization'));
  if ('refusal' in value) return Response.json({ detail: value.refusal }, { status: 401 });
  const parsed = parseToken(value.token);
  if (!parsed) return Response.json({ detail: BAD_TOKEN }, { status: 401 });
  headers.set(SUBJECT_HEADER, parsed.subject);
  headers.set(KIND_HEADER, 'pat');
  headers.set(PAT_HASH_HEADER, await c.hasher.sha256Hex(value.token.trim()));
  const stub = env.USER.get(env.USER.idFromName(parsed.subject));
  const response = await stub.fetch(new Request(request, { headers }));
  if (response.status === 410 && response.headers.has('x-prep-tombstoned')) {
    return Response.json({ detail: BAD_TOKEN }, { status: 401 });
  }
  return response;
}

/**
 * The anonymous accounts a previous run minted. They belong to no profile,
 * and the seeded generator hands out the same ids again, so leaving them
 * makes the next run's mint land in a cell that already holds its deck.
 * Wiped rather than destroyed: a tombstone would refuse the id forever.
 */
async function clearAnonymous(c: Composition): Promise<void> {
  for (let page = 1; page <= ANONYMOUS_PAGES; page++) {
    const users = await c.directory.listAnonymous(null, ANONYMOUS_PAGE);
    if (users.length === 0) return;
    for (const user of users) {
      const cell = c.userCells.cell(user.id) as unknown as SeedApi;
      // Two RPCs, as the profile seed is: the wipe takes the schema with it,
      // and the empty seed puts it back so the id is usable again.
      await cell.wipe(ANONYMOUS_PROFILE);
      await cell.seed(ANONYMOUS_PROFILE, user.id, null);
      await c.directory.remove(user.id);
    }
  }
}

/**
 * One cell's rows, for a test that used to open the server's database file.
 * Read-only and test-only, behind the same token the seed takes.
 */
async function dumpCell(request: Request, url: URL, env: Env, c: Composition): Promise<Response> {
  if (!c.internalToken) return Response.json({ detail: 'PREP_INTERNAL_TOKEN not configured' }, { status: 503 });
  if (request.headers.get(INTERNAL_TOKEN_HEADER) !== c.internalToken) {
    return Response.json({ detail: 'invalid X-Internal-Token' }, { status: 401 });
  }
  if (url.searchParams.get('cell') === 'directory') {
    return Response.json({ tables: await (c.directory as unknown as DirectorySeedApi).dumpTables() });
  }
  const user = url.searchParams.get('user');
  if (!user) return Response.json({ detail: 'user is required' }, { status: 422 });
  const stub = env.USER.get(env.USER.idFromName(user)) as unknown as SeedApi;
  return Response.json(await stub.dump());
}

/**
 * The job cells a previous run started. Their ids come from the same seeded
 * generator, so the next run addresses the same cells, and a cell holding a
 * finished ledger answers `start` with that run's outcome instead of doing
 * the work again. Read before the owner's wipe, which takes the rows naming
 * them.
 */
async function clearJobs(stub: SeedApi, c: Composition): Promise<void> {
  const snapshot = await stub.dump();
  const ids = new Set<string>();
  for (const table of JOB_TABLES) {
    for (const row of snapshot.tables[table] ?? []) {
      const id = row['workflow_id'];
      if (typeof id === 'string') ids.add(id);
    }
  }
  for (const id of ids) await (c.jobCells.cell(id) as unknown as JobSeedApi).wipe();
}

const JOB_TABLES = ['active_workflows', 'job_progress'] as const;
const ANONYMOUS_PROFILE = 'anonymous';
const ANONYMOUS_PAGE = 100;
const ANONYMOUS_PAGES = 20;

async function seed(request: Request, env: Env, c: Composition): Promise<Response> {
  if (!c.internalToken) return Response.json({ detail: 'PREP_INTERNAL_TOKEN not configured' }, { status: 503 });
  if (request.headers.get(INTERNAL_TOKEN_HEADER) !== c.internalToken) {
    return Response.json({ detail: 'invalid X-Internal-Token' }, { status: 401 });
  }
  let body: { user?: unknown; profile?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return Response.json({ detail: 'bad json' }, { status: 400 });
  }
  if (typeof body.user !== 'string' || !body.user || typeof body.profile !== 'string' || !body.profile) {
    return Response.json({ detail: 'user and profile are required' }, { status: 422 });
  }
  const stub = env.USER.get(env.USER.idFromName(body.user)) as unknown as SeedApi;
  try {
    // The instant ledger is global and durable, and the pinned clock never
    // advances, so one run's spend would refuse the next run's first
    // generation. It leaves with the data it was recorded against.
    await (c.limiter as unknown as LimiterSeedApi).wipe();
    await clearAnonymous(c);
    // The entry worker draws the anonymous id and the instant slug from its
    // own isolate's generator, which no cell's reset reaches: without this
    // the ids a run mints depend on how many the runs before it did.
    c.resetRandom();
    await clearJobs(stub, c);
    // Two RPCs: the wipe cannot share a call with the rows it makes room for.
    await stub.wipe(body.profile);
    return Response.json(await stub.seed(body.profile, body.user, request.headers.get(TEST_NOW_HEADER)));
  } catch (e) {
    if (e instanceof UnknownProfile) return Response.json({ detail: e.message }, { status: 400 });
    throw e;
  }
}
