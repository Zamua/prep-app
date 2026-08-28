// Static assets over the wrangler `assets` binding. The worker owns every
// path (run_worker_first), so the versioned-path alias rule and the cache
// headers live here, not in the asset server.
import { isAcceptedVersionToken } from './tokenRules.js';
import type { Env } from './env.js';

// Versioned URLs are content-addressed by construction: a new build mints a
// new prefix, so any URL caches forever.
const IMMUTABLE = 'public, max-age=31536000, immutable';

// Unversioned paths revalidate on every use (etag/304), so a deploy is
// visible without a hard refresh even on iOS Safari's heuristic cache.
const REVALIDATE = 'no-cache';

// `/static/js/v<seg>/<rest>` and the css twin. The segment is opaque: any
// accepted token, current or legacy, aliases onto the current tree so a
// page cached from a prior deploy keeps resolving its assets. A segment
// that is not a token is a literal sub-path (`/static/js/vendor/...`).
const VERSIONED = /^\/static\/(js|css)\/v([^/]*)\/(.+)$/;

/** `null` for anything outside `/static/`, for a non-GET, and for a missing
 * asset, so the router's own 404 page answers rather than a bare status.
 * The token is not consulted: the alias rule
 * accepts any token so pages cached from a prior build keep resolving. */
export async function serveStatic(
  request: Request,
  env: Pick<Env, 'ASSETS'>,
  _token: string,
): Promise<Response | null> {
  const url = new URL(request.url);
  if (!url.pathname.startsWith('/static/')) return null;
  if (request.method !== 'GET' && request.method !== 'HEAD') return null;

  let path = url.pathname;
  let cacheControl = REVALIDATE;
  const m = VERSIONED.exec(path);
  if (m && isAcceptedVersionToken(m[2]!)) {
    path = `/static/${m[1]}/${m[3]}`;
    cacheControl = IMMUTABLE;
  }

  const upstream = await env.ASSETS.fetch(
    new Request(new URL(path, url.origin), { method: request.method, headers: request.headers }),
  );
  if (upstream.status === 404) return null;
  const response = new Response(upstream.body, upstream);
  response.headers.set('Cache-Control', cacheControl);
  return response;
}
