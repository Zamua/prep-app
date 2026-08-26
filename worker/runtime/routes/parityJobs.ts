// The crash harness's only door into a JobCell. Parity-only and behind the
// internal token, because the app itself never addresses a job cell from
// outside its owner: routes go through the runner, which goes through the
// owner's cell.
//
// It exists so a kill point is reachable on a real node before the four
// workflows are written: the probe job's steps do exactly what theirs do to
// the ledger, and this endpoint starts one, signals it, and reads back the
// rows an assertion needs.
import type { JobCellRpc } from '../../app/ports.js';
import type { Composition } from '../compose.js';
import type { Env } from '../env.js';

export const PARITY_JOB_PREFIX = '/_parity/job';

interface StartBody {
  id?: unknown;
  kind?: unknown;
  owner?: unknown;
  input?: unknown;
}

interface SignalBody {
  id?: unknown;
  name?: unknown;
  payload?: unknown;
}

interface AbandonBody {
  id?: unknown;
  owner?: unknown;
}

/** The two parity-only resets, which no port declares: a job cell empties
 * itself, and an owner drops what a deleted execution left behind. */
interface JobReset {
  wipe(): Promise<void>;
}
interface OwnerReset {
  forgetJob(jobId: string, at: string): Promise<void>;
}

/** Null when the path is not one of ours, so the caller falls through. */
export async function serveParityJobs(request: Request, url: URL, env: Env, c: Composition, at: string): Promise<Response | null> {
  if (!c.parity || !url.pathname.startsWith(PARITY_JOB_PREFIX)) return null;
  if (!c.internalToken) return Response.json({ detail: 'PREP_INTERNAL_TOKEN not configured' }, { status: 503 });
  if (request.headers.get('x-internal-token') !== c.internalToken) return Response.json({ detail: 'invalid X-Internal-Token' }, { status: 401 });

  const cell = (id: string): JobCellRpc => env.JOB.get(env.JOB.idFromName(id)) as unknown as JobCellRpc;
  const rest = url.pathname.slice(PARITY_JOB_PREFIX.length);

  if (request.method === 'POST' && rest === '/start') {
    const body = (await request.json()) as StartBody;
    if (typeof body.id !== 'string' || typeof body.kind !== 'string' || typeof body.owner !== 'string') {
      return Response.json({ detail: 'id, kind and owner are required' }, { status: 422 });
    }
    const input = (body.input ?? {}) as Record<string, unknown>;
    return Response.json(
      await cell(body.id).start({
        id: body.id,
        kind: body.kind,
        owner: body.owner,
        input,
        urlPath: `/_parity/job/${body.id}`,
        workflowType: body.kind,
        deckId: null,
        deckName: typeof input['deckName'] === 'string' ? (input['deckName'] as string) : null,
        at,
      }),
    );
  }

  if (request.method === 'POST' && rest === '/signal') {
    const body = (await request.json()) as SignalBody;
    if (typeof body.id !== 'string' || typeof body.name !== 'string') return Response.json({ detail: 'id and name are required' }, { status: 422 });
    return Response.json(await cell(body.id).signal({ name: body.name, payload: body.payload, at }));
  }

  // The pixel gate's `gone` screen. Python deletes the Temporal execution
  // out from under a running job; here the job's cell is emptied and the
  // owner is left with the closed badge row and no progress to answer from.
  if (request.method === 'POST' && rest === '/abandon') {
    const body = (await request.json()) as AbandonBody;
    if (typeof body.id !== 'string' || typeof body.owner !== 'string') return Response.json({ detail: 'id and owner are required' }, { status: 422 });
    await (cell(body.id) as unknown as JobReset).wipe();
    await (c.userCells.cell(body.owner) as unknown as OwnerReset).forgetJob(body.id, at);
    return Response.json({ abandoned: body.id });
  }

  if (request.method === 'GET' && rest.startsWith('/')) {
    const status = await cell(rest.slice(1)).peek();
    return Response.json(status);
  }

  return Response.json({ detail: 'not found' }, { status: 404 });
}
