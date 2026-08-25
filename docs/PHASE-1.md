# Phase 1: the skeleton

Spec for phase 1 of `docs/CELLD-REWRITE.md` (2.2, 2.3, 3, 5.3). Four
lanes, A to D, no shared files; E is the order and the gates. The gate is
phase 0's (`docs/PARITY-GATE.md`): same constants, goldens, differ and
harness, pointed at a TypeScript server.

## 0. Constants and layout

Section 0 of PARITY-GATE holds: `PARITY_NOW`, `PARITY_TZ`, `PARITY_USER`,
`PARITY_BUILD_ID = ce11d0000000`, `PARITY_INTERNAL_TOKEN`. The worker
reads them as vars: `PREP_FAKE_NOW`, `PREP_BUILD_ID`,
`PREP_PLACEHOLDER_INDEX`, `PREP_INTERNAL_TOKEN`, `PREP_PARITY_MODE`.

```
worker/
  package.json tsconfig.json vitest.config.ts .gitignore (build/ dist/ node_modules/)
  wrangler.dev.jsonc wrangler.staging.jsonc wrangler.prod.jsonc
  domain/index.ts    empty until phase 2
  app/ports.ts       Clock, Identity, IdentityProvider, Renderer
  app/viewmodels/    derive.ts + one file per template needing fields
  runtime/worker.ts env.ts compose.ts errors.ts buildToken.ts assets.ts sw.ts
  runtime/cells/{UserCell,DirectoryCell,InstantLimiterCell,JobCell}.ts
  runtime/adapters/{fakeIdentity,clock,fixturePages}.ts
  runtime/adapters/nunjucks/{index,shims,icons}.ts
  templates/         the 49 templates, ported
  scripts/build.mjs scripts/run-node.sh scripts/render-fixtures.mjs
  tests/ build/ dist/
```

`worker/` is the celld project: `celld deploy worker --config
wrangler.<env>.jsonc`. No bare `wrangler.jsonc` exists, so nothing deploys
without naming an environment. `package.json` (`"type": "module"`):
`nunjucks@3.2.4`; dev `@cloudflare/workers-types`, `esbuild@0.28.x`,
`typescript`, `vitest`, `@types/node`. tsconfig as kcal's; `tests/tsconfig.json`
adds `node` and `vitest/globals`.

Every lane: no operator context in the app repo; every behavior lands
with its test; no push.

## A. Worker skeleton (lane A)

Owns everything under `worker/` except lane B's and lane C's files, and
`tests/parity/oracles/pages.py` plus its corpus. Lands first.

### A1. Wrangler files

Identical shape in all three, differing only in `vars`:

```jsonc
{ "name": "prep", "main": "runtime/worker.ts", "compatibility_date": "2026-01-01",
  "durable_objects": { "bindings": [
    { "name": "USER", "class_name": "UserCell" },
    { "name": "DIRECTORY", "class_name": "DirectoryCell" },
    { "name": "INSTANT_LIMITER", "class_name": "InstantLimiterCell" },
    { "name": "JOB", "class_name": "JobCell" } ] },
  "migrations": [{ "tag": "v1",
    "new_sqlite_classes": ["UserCell", "DirectoryCell", "InstantLimiterCell", "JobCell"] }],
  "assets": { "directory": "dist/assets", "binding": "ASSETS", "run_worker_first": true },
  "vars": { "PREP_ENV": "staging" } }
```

`PREP_ENV` is `dev`, `staging`, `prod`. The dev file alone also carries the
parity pins as vars: `PREP_PARITY_MODE: "1"`, `PREP_FAKE_NOW`,
`PREP_BUILD_ID: "ce11d0000000"`, `PREP_PLACEHOLDER_INDEX: "0"`,
`PREP_INTERNAL_TOKEN: "parity-internal-token"` (local-only constant,
PARITY-GATE section 0). Public Clerk values arrive in phase 3.

`tests/wrangler.test.ts`: the three files parse (strip `//` comments);
bindings, migrations and `assets` are deep-equal across them; the four
class names are exactly these; `wrangler.prod.jsonc` has `PREP_ENV=prod`;
only dev carries keys starting `PREP_PARITY`, `PREP_FAKE`, `PREP_INTERNAL`
or `PREP_BUILD_ID`; every `vars` key is on an allow-list of public names.

### A2. Layering test

`tests/layering.test.ts` is kcal's, copied, with the ports rule added:

1. `domain/` imports nothing from `app/`, `runtime/`, `cloudflare:`, `node:`.
2. `app/` imports only `../domain/` and `./`; contains none of `fetch(`,
   `new Response`, `.sql.exec`, `DurableObject`, `nunjucks`.
3. Ports rule: outside `runtime/compose.ts` and `tests/`, no file imports
   from `runtime/adapters/`. Cells and the router receive adapters through
   the `app/ports` types from the composition root only.
4. Only `runtime/adapters/nunjucks/` imports `build/templates.js` or
   `nunjucks`.
5. The walk inspected at least one file per layer.

### A3. Ports (`app/ports.ts`)

```ts
export interface Clock { now(): Date }
export interface Identity { subject: string; displayName: string }
export interface IdentityProvider { identify(request: Request): Promise<Identity | null> }
export interface Renderer { render(template: string, context: Record<string, unknown>): string }
```

### A4. Adapters

- `runtime/adapters/clock.ts`: `SystemClock`; `FixedClock(at)`;
  `clockFromEnv(env)`: `PREP_FAKE_NOW` set means `FixedClock` (ISO with
  `Z` or offset; naive means UTC; malformed throws naming the var), with
  one warning per isolate. Tests mirror `tests/infrastructure/test_clock.py`.
- `runtime/adapters/fakeIdentity.ts`: `FakeIdentityProvider` reads
  `tailscale-user-login` (subject) and `tailscale-user-name` (display
  name, default `Parity`); absent login means `null`. Exactly the headers
  `tests/parity/harness/contextspec.py` injects.
- `runtime/adapters/fixturePages.ts`: loads `build/pages.js` (A7) and
  answers `(profile, method, path, flags) -> FixturePage | null`.

### A5. Composition root (`runtime/compose.ts`)

`compose(env)`, memoized per isolate, returns `{ clock, identity, renderer,
buildToken, parity }`. `parity = env.PREP_PARITY_MODE === "1"`. **Guard:**
every var matching `PREP_PARITY*`, `PREP_FAKE*` or `PREP_PLACEHOLDER*` is
refused unless `PREP_ENV` is exactly `dev` or `staging`; a missing or
misspelt `PREP_ENV` refuses too (allow-on-known, never deny-on-prod), and
the error names the offending vars. Without parity, phase 1 has no
identity provider (`identify` returns `null`) and phase 3 adds Clerk.
Cross-cutting wrappers live here, never in handlers: `noCacheHtml(res)`
sets `Cache-Control: no-cache` on `text/html`; `cookieHooks(req, res)` is
the identity function with the signature the anonymous-cookie wrapper
takes in phase 3. Test: parity on prod, parity with no `PREP_ENV`, a
frozen clock on prod and a placeholder pin under `production` all throw;
prod without pins composes on the system clock; staging + parity composes
the fake provider; no-cache lands on HTML, not on JSON.

Staging gets the flag without a wrangler var: celld reads
`CELLD_VAR_PREP_PARITY_MODE=1` from the node process env (spike 1), set
explicitly in the staging deploy (lane D). No prod deploy carries it, and
the guard above refuses it under `PREP_ENV=prod` anyway. **This overrides
plan 2.5** ("a fake identity provider, never enabled by a deploy file"):
the staging parity host enables it on an internet-reachable ingress, which
is inert while cells hold fixture pages only. Plan section 7 carries the
decision that must land before phase 3 gives that host real data.

### A6. Router (`runtime/worker.ts`) and cells

Dispatch order, each step a translation only:

1. `/healthz`: `ok` before anything composes, no storage touched.
   `/readyz`: `ok` only after `compose(env)` succeeds, so a misconfigured
   worker is alive but never ready.
2. `serveStatic(request, env, buildToken)` (C2); a non-null response returns.
3. `/sw.js`, `/manifest.json` (C3); `/offline` renders `offline.html`
   with `{ build }`, echoing `?build=` only when
   `isAcceptedVersionToken` (C1) accepts it.
4. `/privacy`: `privacy.html`, `user: null`. `/offline` and `/privacy`
   answer `GET` and `HEAD`; any other method is the 405 page with detail
   `Method Not Allowed`, as Starlette answers a GET route.
5. Under parity only: `GET /_parity/raise` renders the 500 page (`?status=429`
   the 429 page); `GET /_parity/reauth` renders `reauth.html`
   `{ user: null }`; `GET /_parity/sign-out` renders
   `sign_out_interstitial.html` `{ user: null, redirect_url: "/" }`;
   `POST /_parity/seed` checks `X-Internal-Token` against
   `PREP_INTERNAL_TOKEN` (401 otherwise), body `{ user, profile }`,
   forwards to `USER.idFromName(user)`, returns the cell's seed JSON.
6. `identity.identify(request)`: `null` and `GET /` renders `landing.html`
   from the anonymous fixture page (A7); `null` elsewhere is a 404 page.
   An identity forwards the request to `USER.get(USER.idFromName(subject))`
   with the identity in `x-prep-subject` / `x-prep-display-name` headers,
   inbound copies of which the router strips first. Identification reads a
   bodiless copy of the headers; the request itself is rebuilt exactly
   once, for the cell, so a form body arrives intact (a body stream can be
   consumed once, and a second `new Request(request)` throws).
7. Anything else: the 404 page.

`runtime/errors.ts` ports `_ERROR_COPY` from `prep/web/errors.py` verbatim
and renders `error.html` with `{ status_code, headline, blurb, path }` at
that status. Router-rendered pages get the nine processor names: `user:
null`, `agent_available: false`, `auth_provider: "tailscale"`, empty
sign-in and sign-out URLs, Clerk keys `null`, `notif_unseen_count: 0`,
`deck_display: {}`, `static_css_mtime: buildToken`; plus `app_base`, the
request origin (`runtime/appBase.ts`: `scheme://host`, the forwarded
scheme first because TLS ends at the ingress), which stands in for the
`request` object every Python context carried and is the only thing a
template may read of the request. Cells add the same field.

`UserCell` (`extends DurableObject`) keeps `{ profile, flags }` in
`ctx.storage` so an eviction mid-flow loses nothing. `seed(profile)`
resets to `{ profile, flags: {} }` and returns the corpus `seed.json`.
`fetch` resolves the fixture page for `(profile, method, path, flags)`,
adds `app_base`, applies `derive(template, context)` (B4), renders, answers with the
recorded status and headers, then flips the page's `sets` flags; JSON
pages return the recorded body; no page is a 404 page. `DirectoryCell`,
`InstantLimiterCell`, `JobCell`: declared, exported from `worker.ts`,
`fetch` returns 501. Router tests use a fake `Env` (stub namespaces, stub
`ASSETS`): each dispatch rule, the header strip, the 501 stubs.

### A7. Page-context corpus (Python, the fixture the cells serve)

Phase 1's pixel screens come from the `reader` and `empty` profiles, so
the stub repos are a recording of what the Python routes passed to their
templates. `python -m tests.parity.oracles.pages` (an extractor like the
others: `NAME = "pages"`, `extract()`, `pin_clock()`, `PREP_PARITY_MODE=1`,
a fresh scratch DB seeded through `prep.dev.parity_seed.seed`) drives a
`TestClient` with the contextspec headers through this script, hooking
`jinja2.Template.render` to record `(template, context)`:

| profile | requests (state after `->`) |
| --- | --- |
| anonymous | `GET /`, `GET /privacy`, `GET /_parity/reauth`, `GET /_parity/sign-out`, `GET /no-such-page-parity`, `GET /_parity/raise`, `GET /_parity/raise?status=429` |
| empty | `GET /`, `GET /api/dashboard/deck-menus`, `GET /api/active-workflows-badge` |
| reader | `GET /`, `GET /api/dashboard/deck-menus`, `GET /api/active-workflows-badge`, `GET /deck/world-capitals`, `POST /deck/world-capitals/pin -> pinned`, `GET /deck/world-capitals`@pinned, `GET /deck/world-history`, `GET /deck/scratch`, `GET /decks/new`, `GET /decks/new/srs`, `GET /decks/new/trivia`, `GET /deck/world-capitals/question/new`, `GET /question/<code>/edit`, `GET /settings/agent`, `POST /settings/agent/byok/anthropic-api/connect` (form `api_key` = `BYOK_KEY` from `flows/settings.py`) `-> byok`, `GET /settings/agent`@byok, `GET /settings/srs`, `GET /settings/editor`, `GET /settings/api`, `GET /notify`, `GET /notify/log` |

Output `tests/fixtures/parity/pages/<profile>/seed.json` (the seed
response; `{}` for anonymous) and `<profile>/<NN>-<METHOD>-<path-slug>[@state].json`:
`{ method, path, status, headers: { location?, content-type }, template?,
context?, body?, sets: [flag...] }`. Contexts pass through `to_jsonable`
in `tests/parity/oracles/__init__.py` (scalars, dict, list; pydantic
`model_dump`; dataclass `asdict`; `SimpleNamespace` and `sqlite3.Row` to
dict; `deck_display` materialized as `{ slug: display }` over the
profile's decks; `request` dropped; anything else raises naming its
path). `test_oracles.py` covers `pages` like every corpus. Lane B's
`contexts/` corpus (B5) reuses `to_jsonable`.

## B. Templates (lane B)

Owns the files the lane table (G) lists.

### B1. Renderer adapter

`createRenderer({ clock, root }): Renderer` over `nunjucks-slim` and the
precompiled map `build/templates.js` (C4; the spike's `precompile.mjs`
shape, `autoescape: true`). One module-level environment per isolate,
shared by every cell: fine for compiled templates, never for per-cell
state. `render` merges `{ root }` over the context.

### B2. The shim table (plan 2.3), each with a unit test in `shims.test.ts`

| shim | contract |
| --- | --- |
| `root` global | `""`; replaces the 100 `request.scope.get('root_path','')` sites |
| `get(obj, key, default)` global | the 8 `.get()` sites that pass a default; plain property access elsewhere |
| `format` filter | Python `%` for `%s`, `%d`, `%.Nf`, `%%`; the 6 sites |
| `slice(start, end)` filter | Python slice semantics on strings and arrays, negatives included; the 20 sites |
| `x in [...]` | tuple literals rewritten to arrays; `true`/`false`/`null` literals rewritten |
| `items(obj)` global | `Object.entries` in insertion order; `replace(old, new)` replaces every occurrence; `join(sep)` |
| `tojson` filter | `SafeString` of `JSON.stringify` with `<`, `>`, `&`, `'` escaped as `\u003c`, `\u003e`, `\u0026`, `\u0027`, the markupsafe `htmlsafe_json_dumps` contract |
| `round` filter | banker's rounding (`round(0.5) = 0`, `round(1.5) = 2`) |
| `pyfloat` filter | Python `str(float)`: `50.0` stays `50.0`, `repr` shortest form |
| `int` filter | Python `int()` truncation |
| `icon(name, { class_, title })` global | the SVG map (`build/icons.js`, C4); injection exactly as `prep/icons.py`: `class`, `aria-hidden="true"`, or `role="img" aria-label` when titled; unknown name renders `""` |
| `markdown` filter | `static/js/modules/markdown.js` imported as the server renderer (the plan's single implementation), `SafeString`; `""` for empty |
| `wakes_in`, `relative_time` filters | ported from `prep/app.py` against `clock.now()`, every branch tested |

### B3. The 49 templates

Copy `templates/` to `worker/templates/`. Thirty-eight precompile
unchanged. The eleven needing syntax edits, measured with nunjucks 3.2.4
on this tree: `deck.html`, `index.html`, `notify/log.html`,
`notify_settings.html`, `partials/transform_diff_card.html`,
`partials/transform_progress.html`,
`partials/trivia_generating_progress.html`, `reorganize.html`,
`settings_agent.html`, `settings_api.html`, `settings_editor.html`. Edits
are the smallest that parse: slices to `|slice`, tuple `in` to arrays,
capitalized literals, the trailing comma, the dict of tuples, the one
`namespace()` site as a `set`. The 4 `dict.update` group-bys and the 3
`selectattr`/`rejectattr` counts become fields the template reads.

### B4. View models (`app/viewmodels/`)

`derive(template, context)` returns the context plus the fields B3 moved
out, pure functions in one file per template, each unit-tested on a
hand-written input. Nothing else lives here in phase 1.

### B5. The DOM gate

`render_templates.py` gains `contexts/<stem>@<name>.json` beside every
golden (`to_jsonable`, A7, plus `app_base`, the origin of the fake
`request` the golden was rendered with; `test_oracles[html]` pins it).
`scripts/render-fixtures.mjs` (bundled by C4 for node) loads every
`contexts/*.json`, applies `derive`, renders under `FixedClock(PARITY_NOW)`,
writes `artifacts/parity/ts-html/<name>.html`; it injects nothing of its
own, so a template that still reads `request` fails the gate. The
templates unit test greps for `request.` as a Python-only smell.
`tests/parity/test_ts_templates.py` runs it once per session, then
`dom_diff(golden, candidate)` from `tests/parity/dom_diff.py` for every
file in `tests/fixtures/parity/html/` except `index.json` and `contexts/`;
one parametrized test per golden, failing with the diff paths.
Acceptance: all 136 goldens equivalent, the two `xss-deck-name` included.

## C. Assets, service worker, build, local run (lane C)

Owns the files the lane table (G) lists.

### C1. Build token (`runtime/buildToken.ts`)

`resolveBuildToken(raw)`: lowercase hex 7-40 verbatim; any other non-empty
value `sha1(value)[:12]`; empty means the baked default (C4).
`isAcceptedVersionToken(seg)`: that hex shape, or ASCII digits only.
`env.PREP_BUILD_ID` overrides the baked token, as in
`prep/web/templates.py`. Tests mirror the Python ones.

### C2. Static serving (`runtime/assets.ts`)

`serveStatic(request, env, token)`; `null` unless the path starts
`/static/`. `/static/js/v<seg>/<rest>` and `/static/css/v<seg>/<rest>`:
when `seg` is accepted, fetch `/static/{js,css}/<rest>` from
`env.ASSETS` and answer with `Cache-Control: public, max-age=31536000,
immutable`; when not accepted (`vendor/...`), serve the literal path. Any
other `/static/*` path serves through `env.ASSETS` with
`Cache-Control: no-cache` (the `_RevalidatingStaticFiles` contract). Path
traversal cannot escape because the binding sees only the asset tree.
`run_worker_first: true` makes the worker own every path. The lane's first
task is a smoke on the local node that `/static/css/index.css` and
`/static/js/v<token>/app.js` answer, because the spikes never exercised
celld's `ASSETS` binding. If it is missing at runtime, stop and report;
do not degrade to a route list.

### C3. Service worker and manifest (`runtime/sw.ts`)

`/sw.js`: the baked `static/sw.js` source with `__BUILD__` replaced by the
token and `__PRECACHE__` by the JSON list from the baked enumeration:
`/offline?build=<token>`, every file under `static/css/` at
`/static/css/v<token>/<rel>`, every file under `static/js/{offline,study,
dashboard,modules}/` at `/static/js/v<token>/<rel>`, then the two PWA
icons; sorted as `prep/web/pwa.py` sorts; `application/javascript`,
`Cache-Control: no-cache`. `/manifest.json`: the `pwa.py` document with
`root = ""`. Test: the list equals a checked-in copy of
`_precache_urls(token, "")` for the same tree.

### C4. Build (`scripts/build.mjs`, `npm run build`)

1. Precompile `worker/templates/**` to `build/templates.js`; a parse
   failure fails the build and names the template.
2. `static/icons/*.svg` to `build/icons.js` (name to source).
3. `static/sw.js` and the precache enumeration to `build/sw.js`.
4. `PREP_BUILD_ID` (env; default `git rev-parse HEAD`) through C1 into
   `build/buildinfo.js`.
5. `dist/assets/static/`: `static/` copied except `sw.js`, `mockups/`,
   `cm/` (build inputs; `cm-bundle.js` ships).
6. `scripts/render-fixtures.mjs` bundled for node to
   `build/render-fixtures.cjs`.
7. `tsc --noEmit` for `worker/` and `worker/tests/`.

`celld deploy` bundles with esbuild (`CELLD_ESBUILD` at
`worker/node_modules/.bin/esbuild`). `build/` and `dist/` are never committed.

### C5. Local run (`scripts/run-node.sh`)

The spike harness (`git show spikes/celld-0b:worker/spikes/run-node.sh`)
made repo-local: `npm run build`, `celld deploy worker --config
wrangler.dev.jsonc --bucket s3://prep-dev --endpoint http://127.0.0.1:9010`
(the scratch MinIO container `celld-scratch-minio`, its root credential
taken from `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` with no default;
`docker start` it when exited; create the bucket once), node on
`127.0.0.1:8791` (`PREP_DEV_PORT`; 8790 is taken on the dev box), internal
listener on port + 10, wait for `/healthz`; `run-node.sh stop`. State
under `/private/tmp/prep-dev-state/`. The dev file carries the pins, so no
`CELLD_VAR_*` is needed.

## D. The staging fleet (lane D, in the operator's infra repo)

The staging fleet, its bucket and credentials, its DNS, the Clerk
registration of the parity host and the deploy targets are operator
concerns and are specified in the operator's private infra repo
(`infra/prep/`, runbook `DEPLOY-CELLD.md`), never here. What this repo
relies on: a celld fleet named for staging that serves
`wrangler.staging.jsonc`, carries the three parity pins as node-side
`CELLD_VAR_*` (A5), mounts the worker secrets the same way, and answers
the eleven phase-1 flows over HTTPS. Staging only; nothing named prod is
created in phase 1.

## E. Integration and gates

Order: A lands, with placeholder `runtime/adapters/nunjucks/index.ts` and
`runtime/assets.ts` exporting the agreed signatures and throwing; B and C
replace them in parallel; D runs from the start (D1-D4 need no app); then
the deploy and the gates. Run only what is named, check exit codes,
browser files one per invocation:

1. `cd worker && npm run typecheck && npx vitest run` (layering, wrangler,
   shims, router, cells, assets, sw).
2. Oracles: `.venv/bin/python -m tests.parity.oracles.render_templates`,
   `.venv/bin/python -m tests.parity.oracles.pages`, then
   `.venv/bin/pytest tests/parity/oracles/test_oracles.py -q`.
3. DOM gate: `cd worker && npm run build`, then
   `.venv/bin/pytest tests/parity/test_ts_templates.py -q`.
4. Local pixel gate: `worker/scripts/run-node.sh`, then for each flow in
   `landing privacy errors reauth dashboard dashboard_empty deck deck_new
   question settings sign_out`:
   `PARITY_BASE_URL=http://127.0.0.1:8791 PARITY_INTERNAL_TOKEN=parity-internal-token
   PARITY_PHASE=1 .venv/bin/pytest tests/parity/test_flows_<flow>.py -q`.
5. Release, then deploy: merge to `main` and cut the semver tag BEFORE the
   first deploy of anything, staging included; the operator's staging
   deploy target refuses a commit that is untagged or not on `main`. Never
   deploy a branch tip or a working tree.
6. Staging pixel gate: the same eleven files against the staging fleet's
   URL, `PARITY_INTERNAL_TOKEN` read from the fleet's secret; both in the
   operator's runbook.

Acceptance: DOM equivalence for every golden of all 49 templates; every
shot of the eleven phase-1 flows passes the comparator, both schemes,
both targets, goldens untouched. Phase 3 flows are not run.

## F. Out of scope

Real repositories and schema, Clerk and the anonymous cookie, workflow
and cell logic beyond `UserCell`'s fixtures, offline sync, the API and
MCP surfaces, migration, anything named prod, any visual change.

## G. Lanes

| lane | owns | test / deploy commands |
| --- | --- | --- |
| A | `worker/**` minus B and C files; `tests/parity/oracles/pages.py`, `to_jsonable`, `tests/fixtures/parity/pages/**` | `cd worker && npm run typecheck && npx vitest run tests/{layering,wrangler,router,cells,compose,clock}.test.ts`; `.venv/bin/python -m tests.parity.oracles.pages && .venv/bin/pytest tests/parity/oracles/test_oracles.py -q -k pages` |
| B | `worker/templates/**`, `worker/runtime/adapters/nunjucks/**`, `worker/app/viewmodels/**`, `worker/scripts/render-fixtures.mjs`, `worker/tests/{shims,templates}.test.ts`, `tests/parity/test_ts_templates.py`, the `contexts/` addition | `cd worker && npx vitest run tests/shims.test.ts tests/templates.test.ts`; `.venv/bin/python -m tests.parity.oracles.render_templates && .venv/bin/pytest tests/parity/oracles/test_oracles.py -q -k html`; `.venv/bin/pytest tests/parity/test_ts_templates.py -q` |
| C | `worker/runtime/{buildToken,assets,sw}.ts`, `worker/scripts/{build.mjs,run-node.sh}`, `worker/tests/{buildToken,assets,sw}.test.ts` | `cd worker && npx vitest run tests/{buildToken,assets,sw}.test.ts`; `npm run build && scripts/run-node.sh` then `curl -sf http://127.0.0.1:8790/static/css/index.css` and `/sw.js` |
| D | the operator's infra repo (`infra/prep/`), nothing in this repo | the operator's runbook (`infra/prep/DEPLOY-CELLD.md`); after E5, `/healthz` over HTTPS on the fleet |
