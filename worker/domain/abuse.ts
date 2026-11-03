// Abuse events: which responses are worth a log line, and the one shape that
// line takes. The key set is fixed and the route comes from a closed
// vocabulary, so a caller cannot put a path, header or id into a line.
import { isPatSurface } from './pat.js';
import { logSubnet } from './instant/ip.js';

export type RouteKind = 'pat' | 'webhook' | 'instant' | 'badge' | 'migrate' | 'api' | 'page';
export type AbuseEvent = 'auth-failure' | 'rate-limited' | 'webhook-rejected';

export interface AbuseLine {
  event: AbuseEvent;
  route: RouteKind;
  status: number;
  subnet: string;
  country: string | null;
}

// The migrate surface is the runtime's (`routes/migrate.ts`); a test pins
// that the two prefixes agree.
const MIGRATE_PREFIX = '/_migrate';

export function routeKindOf(path: string): RouteKind {
  if (isPatSurface(path)) return 'pat';
  if (path === '/webhooks' || path.startsWith('/webhooks/')) return 'webhook';
  if (path.startsWith('/api/instant/')) return 'instant';
  if (path === '/api/active-workflows-badge') return 'badge';
  if (path === MIGRATE_PREFIX || path.startsWith(`${MIGRATE_PREFIX}/`)) return 'migrate';
  if (path.startsWith('/api/')) return 'api';
  return 'page';
}

/** The event a response is, or null when it is not a signal. */
export function abuseEventOf(route: RouteKind, status: number): AbuseEvent | null {
  if (route === 'webhook' && (status === 400 || status === 401)) return 'webhook-rejected';
  if (status === 401 || status === 403) return 'auth-failure';
  if (status === 429) return 'rate-limited';
  return null;
}

const COUNTRY = /^[A-Za-z0-9]{2}$/;

export interface AbuseInput {
  route: RouteKind;
  status: number;
  /** The raw client address; only its subnet leaves this function. */
  ip: unknown;
  country?: unknown;
}

export function abuseLine(event: AbuseEvent, input: AbuseInput): AbuseLine {
  const { route, status } = input;
  const country = typeof input.country === 'string' && COUNTRY.test(input.country) ? input.country.toUpperCase() : null;
  return { event, route, status, subnet: logSubnet(input.ip), country };
}
