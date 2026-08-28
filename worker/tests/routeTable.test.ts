// The cell route table's own invariants. A gate promoted by accident is the
// failure this exists for: `signedIn` on a route an anonymous account has to
// reach takes the whole instant-start product offline, and no page test
// covers it because the page suites run as a signed-in user.
import { describe, expect, it } from 'vitest';
import { apiRoutes } from '../runtime/cells/routes/api.js';
import { pageRoutes } from '../runtime/cells/routes/pages.js';
import { servePublic } from '../runtime/routes/openapi.js';
import { ENTRY_ROUTES } from '../runtime/routes/metrics.js';
import { jobRoutes } from '../runtime/cells/routes/jobs.js';

type Key = string;
const key = (method: string, pattern: string): Key => `${method} ${pattern}`;

const GATES = new Set(['user', 'signedIn', 'pat']);

/** Every route an anonymous account has to reach. An instant-start visitor
 * holds a `prep_anon` cookie and no Clerk session, so `signedIn` here is a
 * dead product surface rather than a 401 someone notices. */
const ANONYMOUS_REACHABLE: readonly Key[] = [
  'GET /',
  'GET /deck/{name}',
  'GET /study/{name}',
  'POST /study/{name}/begin',
  'GET /api/dashboard/overview',
  'GET /api/study/decks/{name}/next',
  'POST /api/study/sessions/{sid}/submit',
  'GET /deck/{name}/edit-with-ai',
  'GET /deck/{name}/edit-with-claude',
];

/** Routes with no counterpart on any other surface, kept because a rename
 * would strand the OAuth callback the provider is configured with. */
const OAUTH: readonly Key[] = ['GET /settings/agent/openrouter/start', 'GET /settings/agent/openrouter/callback'];

const DEBUG_ROUTE = /^[A-Z]+ \/_?debug\//;

const cell = new Map<Key, string>([...pageRoutes, ...jobRoutes, ...apiRoutes].map((r) => [key(r.method, r.pattern), r.gate]));

describe('the cell route table', () => {
  it('declares each method and pattern once', () => {
    const keys = [...pageRoutes, ...jobRoutes, ...apiRoutes].map((r) => key(r.method, r.pattern));
    expect(keys).toEqual([...new Set(keys)]);
    expect(keys).toHaveLength(119);
  });

  it('gives every route one of the three gates', () => {
    const unknown = [...cell.entries()].filter(([, gate]) => !GATES.has(gate));
    expect(unknown).toEqual([]);
  });

  it('leaves every anonymous-reachable route at `user`', () => {
    for (const k of ANONYMOUS_REACHABLE) {
      expect(cell.get(k), `${k} is not a cell route`).toBe('user');
    }
  });

  it('serves the OAuth pair the provider is configured with', () => {
    for (const k of OAUTH) expect(cell.has(k), `${k} is not a cell route`).toBe(true);
  });

  it('keeps the debug routes out of the cell table', () => {
    expect([...cell.keys()].filter((k) => DEBUG_ROUTE.test(k))).toEqual([]);
  });

  it('claims no route the entry worker already answers', () => {
    const entry = ENTRY_ROUTES.map(([method, pattern]) => key(method, pattern));
    expect(entry.filter((k) => cell.has(k))).toEqual([]);
  });

  it('answers the public routes it claims', () => {
    const env = { testMode: false, vapidPublicKey: 'BCT1' };
    for (const path of ['/openapi.json', '/docs', '/docs/oauth2-redirect', '/redoc', '/llms.txt', '/notify/vapid-public-key']) {
      const url = new URL(`https://prep.example.test${path}`);
      expect(servePublic(new Request(url), url, env), `${path} is not served`).not.toBeNull();
    }
  });
});
