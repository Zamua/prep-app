// The translation layer every HTML route table shares: one request parsed
// into the shape a page use case takes, and its answer turned back into the
// router's `Handled`. No route logic lives here and no use case is named.
import { AppError } from '../../../app/errors.js';
import type { PageRequest, PageResult, Upload } from '../../../app/pageResult.js';
import type { UserRepos } from '../../../app/ports.js';
import { parseMultipart } from '../../../domain/multipart.js';
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

export async function pageRequestOf(req: CellRequest, parsed?: { form: URLSearchParams; upload: Upload | null }): Promise<PageRequest> {
  const hxHeader = req.request.headers.get('hx-request');
  return {
    params: req.params,
    query: req.url.searchParams,
    form: parsed ? parsed.form : await formOf(req.request),
    htmx: hxHeader === 'true',
    hxHeader,
    userAgent: req.request.headers.get('user-agent'),
    cookies: cookiesOf(req.request),
    now: isoUtc(req.clock.now()),
    upload: parsed ? parsed.upload : null,
  };
}

/** The body read under a ceiling. `Content-Length` decides first; a chunked
 * body is counted as it arrives and abandoned at the cap, so nothing past it
 * is ever held. Null means the cap was reached. */
export async function readCapped(request: Request, max: number): Promise<Uint8Array | null> {
  const declared = request.headers.get('content-length');
  if (declared !== null && Number(declared) > max) return null;
  const body = request.body;
  if (!body) return new Uint8Array(0);
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!value) continue;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.byteLength;
  }
  return out;
}

/** The multipart body as fields plus the `file` part. A part carrying a
 * filename is the upload; every other part decodes as a field, which is what
 * an empty file input also posts. */
function multipartOf(bytes: Uint8Array, contentType: string): { form: URLSearchParams; upload: Upload | null } {
  const form = new URLSearchParams();
  let upload: Upload | null = null;
  for (const part of parseMultipart(bytes, contentType)) {
    if (part.filename === null) {
      form.append(part.name, new TextDecoder().decode(part.bytes));
      continue;
    }
    form.append(part.name, part.filename);
    if (part.name === 'file' && upload === null) upload = { filename: part.filename || null, bytes: part.bytes };
  }
  return { form, upload };
}

/**
 * A route whose body is a file. The ceiling is checked before any parsing,
 * and an overflow renders `template` with the error the page already shows
 * for a refused import.
 */
export function uploadRoute(method: string, pattern: string, gate: Gate, template: string, tooLarge: string, max: number, handler: Handler): Route {
  return {
    method,
    pattern,
    gate,
    async handler(req: CellRequest): Promise<Handled> {
      const contentType = req.request.headers.get('content-type') ?? '';
      const body = await readCapped(req.request, max);
      if (body === null) return { page: template, context: { outcome: null, error: tooLarge }, status: 413 };
      const parsed = contentType.startsWith('multipart/form-data')
        ? multipartOf(body, contentType)
        : { form: new URLSearchParams(contentType.startsWith('application/x-www-form-urlencoded') ? new TextDecoder().decode(body) : ''), upload: null };
      const request = await pageRequestOf(req, parsed);
      try {
        return handled(await handler(request, { repos: req.repos, ports: req.ports, subject: req.identity.subject }), req);
      } catch (e) {
        if (e instanceof AppError) return errorPageOf(e, req);
        throw e;
      }
    },
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
  if ('bytes' in result) return { bytes: result.bytes, status: result.status, headers: result.headers };
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
