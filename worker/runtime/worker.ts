// Entry worker: a translation layer. Unauthenticated pages render here; an
// identified request is forwarded to its UserCell.
import { compose, cookieHooks, noCacheHtml, type Composition } from './compose.js';
import type { Env } from './env.js';
import { anonymousContext, errorPage, htmlResponse } from './errors.js';
import { serveStatic } from './assets.js';
import { offlineBuild, servePwa } from './sw.js';
import { UnknownProfile } from './cells/UserCell.js';

export { UserCell } from './cells/UserCell.js';
export { DirectoryCell } from './cells/DirectoryCell.js';
export { InstantLimiterCell } from './cells/InstantLimiterCell.js';
export { JobCell } from './cells/JobCell.js';

/** The identity a cell receives; the display name is URI-encoded because a
 * header value is a byte string. */
export const SUBJECT_HEADER = 'x-prep-subject';
export const DISPLAY_NAME_HEADER = 'x-prep-display-name';
export const INTERNAL_TOKEN_HEADER = 'x-internal-token';

interface SeedApi {
  seed(profile: string): Promise<Record<string, unknown>>;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    // Liveness is independent of storage: a wedged process should restart, a
    // storage blip should not restart every node.
    if (url.pathname === '/healthz' || url.pathname === '/readyz') return new Response('ok');
    let c: Composition;
    try {
      c = compose(env);
    } catch (e) {
      console.error(`compose failed: ${e instanceof Error ? e.message : e}`);
      return new Response('service misconfigured', { status: 500 });
    }
    try {
      const res = await route(request, url, env, c);
      return cookieHooks(request, noCacheHtml(res));
    } catch (e) {
      console.error(`unhandled exception on ${request.method} ${url.pathname}: ${e instanceof Error ? (e.stack ?? e.message) : e}`);
      return noCacheHtml(errorPage(c.renderer, c.buildToken, 500, url.pathname));
    }
  },
};

async function route(request: Request, url: URL, env: Env, c: Composition): Promise<Response> {
  const path = url.pathname;
  const page = (template: string, extra: Record<string, unknown> = {}, status = 200) =>
    htmlResponse(c.renderer.render(template, { ...anonymousContext(c.buildToken), ...extra }), status);

  const asset = await serveStatic(request, env, c.buildToken);
  if (asset) return asset;
  const pwa = servePwa(url, c.buildToken);
  if (pwa) return pwa;
  if (path === '/offline') return page('offline.html', { build: offlineBuild(url, c.buildToken) });
  if (path === '/privacy') return page('privacy.html');

  if (c.parity) {
    if (request.method === 'GET' && path === '/_parity/raise') {
      return url.searchParams.get('status') === '429'
        ? errorPage(c.renderer, c.buildToken, 429, path, 'parity: deliberate throttle')
        : errorPage(c.renderer, c.buildToken, 500, path);
    }
    if (request.method === 'GET' && path === '/_parity/reauth') return page('reauth.html');
    if (request.method === 'GET' && path === '/_parity/sign-out') {
      return page('sign_out_interstitial.html', { redirect_url: '/' });
    }
    if (request.method === 'POST' && path === '/_parity/seed') return seed(request, env, c);
  }

  // Inbound copies of the identity headers are stripped before anything
  // reads them: only the router may assert an identity to a cell.
  const headers = new Headers(request.headers);
  headers.delete(SUBJECT_HEADER);
  headers.delete(DISPLAY_NAME_HEADER);
  const clean = new Request(request, { headers });

  const who = await c.identity.identify(clean);
  if (!who) {
    if (request.method === 'GET' && path === '/') {
      const landing = c.pages.resolve('anonymous', 'GET', '/', []);
      if (landing?.template) return page(landing.template, landing.context ?? {}, landing.status);
    }
    return errorPage(c.renderer, c.buildToken, 404, path, 'Not Found');
  }
  headers.set(SUBJECT_HEADER, who.subject);
  headers.set(DISPLAY_NAME_HEADER, encodeURIComponent(who.displayName));
  const stub = env.USER.get(env.USER.idFromName(who.subject));
  return stub.fetch(new Request(request, { headers }));
}

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
    return Response.json(await stub.seed(body.profile));
  } catch (e) {
    if (e instanceof UnknownProfile) return Response.json({ detail: e.message }, { status: 400 });
    throw e;
  }
}
