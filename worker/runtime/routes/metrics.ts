// GET /metrics, and the hook that feeds it.
//
// The scrape is the entry worker's: unauthenticated, the same path the
// reference serves, and answered before the worker composes so a
// misconfigured node still reports. It is never itself observed, or the
// histogram would mostly be a record of the scraper.
import { METRICS_CONTENT_TYPE, observeHttpRequest, renderMetrics } from '../../app/metrics.js';
import { apiRoutes } from '../cells/routes/api.js';
import { jobRoutes } from '../cells/routes/jobs.js';
import { pageRoutes } from '../cells/routes/pages.js';
import { matchRoute, type Route } from '../cells/router.js';

/** No route matched, so the raw path is not worth a series of its own. */
export const UNMATCHED = '<unmatched>';

/** Everything outside the standard verbs. `method` is the one label a client
 * chooses freely, and an unbounded label is a leak in a 128 MB isolate. */
export const OTHER_METHOD = '<other>';

const METHODS = new Set(['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT']);

/**
 * The routes the entry worker answers itself, as route TEMPLATES: `{name}`
 * matches one segment, `{name:path}` the rest. The `route` label is the
 * template, never the raw URL, which is what keeps cardinality bounded.
 *
 * `tests/routeTable.test.ts` reads this as its entry-worker inventory: a
 * route the worker answers without landing here labels as `<unmatched>`.
 */
export const ENTRY_ROUTES: readonly (readonly [string, string])[] = [
  ['GET', '/healthz'],
  ['GET', '/metrics'],
  ['GET', '/openapi.json'],
  ['GET', '/docs'],
  ['GET', '/docs/oauth2-redirect'],
  ['GET', '/redoc'],
  ['GET', '/llms.txt'],
  ['GET', '/privacy'],
  ['GET', '/offline'],
  ['GET', '/manifest.json'],
  ['GET', '/sw.js'],
  ['GET', '/sign-in'],
  ['GET', '/sign-out'],
  ['POST', '/forget-device'],
  ['POST', '/api/instant/generate'],
  ['GET', '/notify/vapid-public-key'],
  ['GET', '/static/css/v{build}/{path:path}'],
  ['GET', '/static/js/v{build}/{path:path}'],
];

/** Infrastructure routes, which need a label of their own or a readiness
 * probe every few seconds buries real 404s under `<unmatched>`. Kept apart
 * from `ENTRY_ROUTES` so the product inventory stays readable. */
const LOCAL_ROUTES: readonly (readonly [string, string])[] = [['GET', '/readyz']];

const CELL_ROUTES: readonly Route[] = [...pageRoutes, ...jobRoutes, ...apiRoutes];

export function serveMetrics(request: Request, url: URL): Response | null {
  if (url.pathname !== '/metrics') return null;
  if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405 });
  return new Response(renderMetrics(), { headers: { 'content-type': METRICS_CONTENT_TYPE, 'cache-control': 'no-store' } });
}

/** One request: the route template if something matched, `<unmatched>`
 * otherwise, and the status a thrown request would have carried. */
export function observe(method: string, path: string, status: number, seconds: number): void {
  try {
    observeHttpRequest(METHODS.has(method) ? method : OTHER_METHOD, routeLabel(method, path), status, seconds);
  } catch (e) {
    // A path that fails to decode must not take the response with it.
    console.error(`metrics: ${e instanceof Error ? e.message : e}`);
  }
}

export function routeLabel(method: string, path: string): string {
  for (const [m, pattern] of ENTRY_ROUTES) if (m === method && matchPattern(pattern, path)) return pattern;
  for (const [m, pattern] of LOCAL_ROUTES) if (m === method && matchPattern(pattern, path)) return pattern;
  return matchRoute(CELL_ROUTES, method, path)?.route.pattern ?? UNMATCHED;
}

const compiled = new Map<string, RegExp>();

function matchPattern(pattern: string, path: string): boolean {
  let re = compiled.get(pattern);
  if (!re) {
    re = compile(pattern);
    compiled.set(pattern, re);
  }
  return re.test(path);
}

const escapeRe = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** `{name}` takes one segment and will not take an empty one; `{name:path}`
 * takes everything left, an empty tail included. A parameter can sit inside
 * a segment, as the versioned asset prefix does. */
function compile(pattern: string): RegExp {
  const token = /\{([A-Za-z_][A-Za-z0-9_]*)(:path)?\}/g;
  let source = '';
  let last = 0;
  for (let m = token.exec(pattern); m !== null; m = token.exec(pattern)) {
    source += escapeRe(pattern.slice(last, m.index)) + (m[2] ? '.*' : '[^/]+');
    last = m.index + m[0].length;
  }
  return new RegExp(`^${source + escapeRe(pattern.slice(last))}$`);
}
