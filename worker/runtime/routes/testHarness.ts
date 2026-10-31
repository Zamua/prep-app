// The `/_test/*` surface a test-mode deploy serves: the seed that resets a
// user cell and everything global a run touches, a read-only cell dump, the
// job probes, and pages a test cannot otherwise reach. The composition root
// refuses test mode on prod, so none of this is reachable there.
import type { UserCellRpc } from '../../app/ports.js';
import { isoUtc } from '../../domain/time.js';
import { ANONYMOUS_PROFILE, UnknownProfile, type SeedRpc } from '../cells/seed/cell.js';
import { clockFor, TEST_NOW_HEADER, type Composition } from '../compose.js';
import type { Env } from '../env.js';
import { errorPage, type PageFn } from '../errors.js';
import { refuseWithoutInternalToken } from '../internalToken.js';
import { serveTestJobs } from './testJobs.js';

/** A user cell as the seed drives it: its test RPCs plus the snapshot. */
type SeedApi = SeedRpc & Pick<UserCellRpc, 'dump'>;

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

/** The response for a `/_test/*` request, or null when the path is not one. */
export async function serveTestHarness(request: Request, url: URL, env: Env, c: Composition, page: PageFn): Promise<Response | null> {
  const path = url.pathname;
  if (request.method === 'GET' && path === '/_test/raise') {
    return url.searchParams.get('status') === '429'
      ? errorPage(c.renderer, c.buildToken, 429, request, 'test: deliberate throttle')
      : errorPage(c.renderer, c.buildToken, 500, request);
  }
  if (request.method === 'GET' && path === '/_test/reauth') return page('reauth.html');
  if (request.method === 'GET' && path === '/_test/sign-out') return page('sign_out_interstitial.html', { redirect_url: '/' });
  if (request.method === 'POST' && path === '/_test/seed') return seed(request, env, c);
  if (request.method === 'GET' && path === '/_test/dump') return dumpCell(request, url, env, c);
  return serveTestJobs(request, url, env, c, isoUtc(clockFor(c, request).now()));
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
  const refused = refuseWithoutInternalToken(request, c.internalToken);
  if (refused) return refused;
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
const ANONYMOUS_PAGE = 100;
const ANONYMOUS_PAGES = 20;

async function seed(request: Request, env: Env, c: Composition): Promise<Response> {
  const refused = refuseWithoutInternalToken(request, c.internalToken);
  if (refused) return refused;
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
