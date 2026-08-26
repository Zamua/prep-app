// The route table against the Python inventory: every route the reference
// app serves is a cell route with the same gate, an entry-worker route, or
// named out of scope with its phase. A gate promoted by accident is the
// failure this exists for - `signedIn` on a route Python lets an anonymous
// account reach takes the whole instant-start product offline, and no corpus
// pair covers it because the corpora are recorded as a signed-in user.
import { describe, expect, it } from 'vitest';
import { apiRoutes } from '../runtime/cells/routes/api.js';
import { pageRoutes } from '../runtime/cells/routes/pages.js';
import { servePublic } from '../runtime/routes/openapi.js';
import { jobRoutes } from '../runtime/cells/routes/jobs.js';
import { pythonJson } from './pyoracle.js';

const PY_ROUTES = `
import json, logging, os
os.environ.setdefault('PREP_AUTH_MODE', 'fake')
logging.disable(logging.CRITICAL)
from prep.app import app

def gate_of(r):
    dep = getattr(r, 'dependant', None)
    if dep is None: return 'none'
    names, stack, seen = set(), [dep], set()
    while stack:
        d = stack.pop()
        if id(d) in seen: continue
        seen.add(id(d))
        c = getattr(d, 'call', None)
        if c is not None: names.add(getattr(c, '__name__', str(c)))
        stack.extend(getattr(d, 'dependencies', []) or [])
    for name, gate in (('signed_in_user', 'signedIn'), ('bearer_user', 'pat'), ('current_user', 'user')):
        if name in names: return gate
    return 'none'

rows = set()
def walk(routes, prefix=''):
    for r in routes:
        orig = getattr(r, 'original_router', None)
        if orig is not None:
            walk(orig.routes, prefix + (getattr(getattr(r, 'include_context', None), 'prefix', '') or ''))
            continue
        sub, path = getattr(r, 'routes', None), getattr(r, 'path', None)
        if sub is not None and path is not None: continue
        if sub is not None:
            walk(sub, prefix + (getattr(r, 'prefix', '') or ''))
            continue
        if path is None: continue
        for m in getattr(r, 'methods', None) or []:
            if m != 'HEAD': rows.add((m, prefix + path, gate_of(r)))

walk(app.routes)
print(json.dumps(sorted(rows)))
`;

type Key = string;
const key = (method: string, pattern: string): Key => `${method} ${pattern}`;

/** Answered before any identity is resolved, so no cell and no gate. */
const ENTRY_WORKER: readonly Key[] = [
  'GET /healthz',
  'GET /openapi.json',
  'GET /docs',
  'GET /docs/oauth2-redirect',
  'GET /redoc',
  'GET /llms.txt',
  'GET /privacy',
  'GET /offline',
  'GET /manifest.json',
  'GET /sw.js',
  'GET /sign-in',
  'GET /sign-out',
  'POST /forget-device',
  'POST /api/instant/generate',
  'GET /notify/vapid-public-key',
  'GET /static/css/v{build}/{path:path}',
  'GET /static/js/v{build}/{path:path}',
];

/** Every route the phase does not own, with the phase that does (PHASE-3 G). */
const OUT_OF_SCOPE: Record<Key, string> = {
  'GET /metrics': 'phase 5',
  'GET /decks/import-anki': 'phase 5',
  'POST /decks/import-anki': 'phase 5',
  'GET /decks/import-csv': 'phase 5',
  'POST /decks/import-csv': 'phase 5',
  'GET /decks/import-prepdeck': 'phase 5',
  'POST /decks/import-prepdeck': 'phase 5',
  'GET /deck/{name}/export.apkg': 'phase 5',
  'GET /deck/{name}/export.prepdeck': 'phase 5',
  'GET /_debug/auth': 'decision 7.6',
  'GET /debug/session': 'decision 7.6',
};

/**
 * Python routes this app deletes rather than ports, with why nothing calls
 * them. Both existed only so an out-of-process Go worker could call back
 * into the app; on celld the step handler holds the `AgentPort` and writes
 * through the owner's repositories in the same isolate.
 */
const REMOVED: Record<Key, string> = {
  'POST /api/agent/run': 'the worker’s own agent call; the step handler holds the port',
  'POST /api/internal/record-review': 'the worker’s own review write; the record step is in the owner’s cell',
};

/** A cell route Python answers with no dependency at all. */
const UNGATED_IN_PYTHON: Record<Key, string> = {
  // Python's handler resolves the identity itself and renders the landing
  // page for a visitor; here the entry worker owns that branch, so the cell's
  // copy is the one an account reaches.
  'GET /': 'the visitor branch is the entry worker’s',
  // A bare redirect to /deck/{name}/edit-with-ai, whose target needs an
  // identity anyway.
  'GET /deck/{name}/edit-with-claude': 'a redirect whose target is gated',
};

/** Cell routes with no Python counterpart (PHASE-3 C, decision 7.4). */
const NEW_IN_THIS_APP: readonly Key[] = ['GET /settings/agent/openrouter/start', 'GET /settings/agent/openrouter/callback'];

const python = new Map<Key, string>(pythonJson<[string, string, string][]>(PY_ROUTES).map(([m, p, g]) => [key(m, p), g]));
const cell = new Map<Key, string>([...pageRoutes, ...jobRoutes, ...apiRoutes].map((r) => [key(r.method, r.pattern), r.gate]));

describe('the cell route table against the Python inventory', () => {
  it('reads the inventory the phase was specified against', () => {
    expect(python.size).toBe(139);
  });

  it('classifies every Python route exactly once', () => {
    const unclassified = [...python.keys()].filter((k) => !cell.has(k) && !ENTRY_WORKER.includes(k) && !(k in OUT_OF_SCOPE) && !(k in REMOVED));
    expect(unclassified).toEqual([]);
    const doubled = [...python.keys()].filter((k) => [cell.has(k), ENTRY_WORKER.includes(k), k in OUT_OF_SCOPE, k in REMOVED].filter(Boolean).length > 1);
    expect(doubled).toEqual([]);
  });

  it('gives every cell route the gate Python gives it', () => {
    const wrong = [...cell.entries()]
      .filter(([k, gate]) => python.has(k) && !(k in UNGATED_IN_PYTHON) && python.get(k) !== gate)
      .map(([k, gate]) => `${k}: python=${python.get(k)} cell=${gate}`);
    expect(wrong).toEqual([]);
  });

  it('serves an anonymous account every route Python does', () => {
    const promoted = [...cell.entries()].filter(([k, gate]) => gate === 'signedIn' && python.get(k) === 'user');
    expect(promoted.map(([k]) => k)).toEqual([]);
  });

  it('lets a route Python leaves open reach the cell without a stricter gate than user', () => {
    for (const k of Object.keys(UNGATED_IN_PYTHON)) {
      expect(python.get(k), `${k} is no longer ungated in Python`).toBe('none');
      expect(cell.get(k), `${k} is no longer a cell route`).toBe('user');
    }
  });

  it('declares nothing the Python app does not serve', () => {
    const invented = [...cell.keys()].filter((k) => !python.has(k) && !NEW_IN_THIS_APP.includes(k));
    expect(invented).toEqual([]);
    for (const k of [...ENTRY_WORKER, ...Object.keys(OUT_OF_SCOPE), ...Object.keys(REMOVED)]) expect(python.has(k), `${k} is not a Python route`).toBe(true);
    for (const k of NEW_IN_THIS_APP) expect(cell.has(k), `${k} is not a cell route`).toBe(true);
  });

  it('serves every phase-4 route and deletes only the two with no caller', () => {
    const phase4 = [...python.keys()].filter((k) => /^(GET|POST) \/(plan|transform|reorganize|trivia\/gen)\b/.test(k) || k === 'POST /deck/{name}/transform');
    expect(phase4.filter((k) => !cell.has(k))).toEqual([]);
    expect(Object.keys(REMOVED).filter((k) => cell.has(k))).toEqual([]);
    expect([...cell.keys()].filter((k) => k in REMOVED)).toEqual([]);
  });

  it('answers the public routes it claims', () => {
    const env = { parity: false, vapidPublicKey: 'BCT1' };
    for (const path of ['/openapi.json', '/docs', '/docs/oauth2-redirect', '/redoc', '/llms.txt', '/notify/vapid-public-key']) {
      const url = new URL(`https://parity.example.test${path}`);
      expect(servePublic(new Request(url), url, env), `${path} is not served`).not.toBeNull();
    }
  });
});
