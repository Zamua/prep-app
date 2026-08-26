// The translation layer every HTML route table shares: one request parsed
// into the shape a page use case takes, and its answer turned back into the
// router's `Handled`. No route logic lives here and no use case is named.
import { AppError } from '../../../app/errors.js';
import type { PageRequest, PageResult } from '../../../app/pageResult.js';
import type { UserRepos } from '../../../app/ports.js';
import { isoUtc } from '../../../domain/py.js';
import { errorContext } from '../../errors.js';
import { HTML, type CellPorts, type CellRequest, type Gate, type Handled, type Route } from '../router.js';

/** A urlencoded body, parsed once. Any other content type posts no fields,
 * which is what FastAPI's `Form(...)` also sees for a body it cannot read. */
export async function formOf(request: Request): Promise<URLSearchParams> {
  if (request.method === 'GET' || request.method === 'HEAD') return new URLSearchParams();
  const type = request.headers.get('content-type') ?? '';
  if (type.startsWith('multipart/form-data')) {
    const data = await request.formData();
    const out = new URLSearchParams();
    for (const [k, v] of data.entries()) out.append(k, typeof v === 'string' ? v : '');
    return out;
  }
  if (!type.startsWith('application/x-www-form-urlencoded')) return new URLSearchParams();
  return new URLSearchParams(await request.text());
}

export function cookiesOf(request: Request): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (request.headers.get('cookie') ?? '').split(';')) {
    const eq = part.indexOf('=');
    if (eq <= 0) continue;
    out[part.slice(0, eq).trim()] = part.slice(eq + 1).trim();
  }
  return out;
}

export async function pageRequestOf(req: CellRequest): Promise<PageRequest> {
  const hxHeader = req.request.headers.get('hx-request');
  return {
    params: req.params,
    query: req.url.searchParams,
    form: await formOf(req.request),
    htmx: hxHeader === 'true',
    hxHeader,
    userAgent: req.request.headers.get('user-agent'),
    cookies: cookiesOf(req.request),
    now: isoUtc(req.clock.now()),
  };
}

/** The Referer when it is same-origin, else the route's own default: a
 * cross-site Referer is an open-redirect target, not a destination. */
function backTo(req: CellRequest, fallback: string): string {
  const referer = req.request.headers.get('referer') ?? '';
  if (!referer) return fallback;
  try {
    const url = new URL(referer);
    if (url.host !== req.url.host) return fallback;
    return url.pathname + (url.search || '');
  } catch {
    return fallback;
  }
}

function handled(result: PageResult, req: CellRequest): Handled {
  if ('redirect' in result) {
    return { redirect: result.back ? backTo(req, result.redirect) : result.redirect, status: result.status ?? 303, headers: result.headers };
  }
  if ('page' in result) return { page: result.page, context: result.context, status: result.status, headers: result.headers };
  if ('json' in result) return { json: result.json, status: result.status, headers: result.headers };
  if ('text' in result) return { text: result.text, status: result.status, headers: { 'content-type': HTML, ...result.headers } };
  return { empty: true, status: result.status, headers: result.headers };
}

/** Python raises `HTTPException` and the shared handler renders the error
 * page; here the page carries the caller's own context, so the masthead
 * still shows who is signed in. */
function errorPageOf(e: AppError, req: CellRequest): Handled {
  return { page: 'error.html', context: errorContext(e.status, req.url.pathname, e.detail), status: e.status };
}

export interface Ctx {
  repos: UserRepos;
  ports: CellPorts;
  subject: string;
}

export type Handler = (req: PageRequest, ctx: Ctx) => PageResult | Promise<PageResult>;

export function route(method: string, pattern: string, gate: Gate, handler: Handler): Route {
  return {
    method,
    pattern,
    gate,
    async handler(req: CellRequest): Promise<Handled> {
      const parsed = await pageRequestOf(req);
      try {
        return handled(await handler(parsed, { repos: req.repos, ports: req.ports, subject: req.identity.subject }), req);
      } catch (e) {
        if (e instanceof AppError) return errorPageOf(e, req);
        throw e;
      }
    },
  };
}
