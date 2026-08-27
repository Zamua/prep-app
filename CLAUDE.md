# prep: working notes for future Claude sessions

What this file is: the doc you read first when picking up work on prep.
Skim it top to bottom, then dive into code. README.md is for humans;
this is for the agent. [`docs/architecture.md`](docs/architecture.md) is
the longer prose tour of the same ground.

---

## What prep is

A spaced-repetition flashcard app. Users describe a topic, an LLM turns
it into a deck, and FSRS schedules the reviews. Installs as a PWA and
studies offline. AI is opt-in and always spends the user's own API key.

It is **one TypeScript Worker on celld**, a self-hostable runtime for
the Cloudflare Workers API. There is no application server process, no
separate database, and no job queue.

---

## Architecture

```
                    browser (PWA)
                          │
                          ▼
   ┌─────────────────────────────────────────────────────┐
   │  entry worker (runtime/worker.ts)                   │
   │    static assets, /manifest.json, /sw.js            │
   │    landing, privacy, offline shell, error pages     │
   │    /api/instant/generate, /webhooks/clerk, /metrics │
   │    identity: Clerk session, anon cookie, or PAT     │
   └───────────────┬─────────────────────────────────────┘
                   │ identity asserted in x-prep-* headers
                   ▼
   ┌───────────────────────┐        ┌──────────────────┐
   │ UserCell (per user)   │◀──────▶│ JobCell (per job)│
   │  SQLite + every page  │ status │  step ledger     │
   │  render + per-user    │  write │  alarm loop      │
   │  alarm                │        │                  │
   └──────┬────────────────┘        └──────────────────┘
          ├──▶ DirectoryCell ("global"): enumeration, merges, reaper
          └──▶ InstantLimiterCell ("global"): instant-generation windows
```

The entry worker is a translation layer. It resolves identity, strips
any inbound copy of the `x-prep-*` headers, sets its own, and forwards.
A request can only reach the cell its verified identity names, so
per-user isolation is structural rather than a `WHERE user_id = ?` every
query has to remember.

### The four cell classes

Class names sit in the storage key path. The taxonomy is a **one-way
door**; a fifth class is a decision, not a refactor.

| class | keyed by | holds |
| --- | --- | --- |
| `UserCell` | user id (Clerk `sub`, or `anon:<hex>`) | one SQLite per user: decks, questions, cards, reviews, sessions, trivia, notifications, push subs, BYOK credentials, PAT hashes, four idempotency ledgers, prefs, job status rows |
| `DirectoryCell` | `"global"` | enumeration only: user id, `is_anonymous`, `created_at`, the merge audit, merge markers, tombstones. Owns the anonymous-retention sweep |
| `InstantLimiterCell` | `"global"` | the instant-generation ledger and both breakers |
| `JobCell` | job id | one durable job: its step ledger and its human gate |

**No scheduler cell, and no per-request write to any global cell.**
`last_seen_at` is bumped on every identified request, so it lives in the
`UserCell`; in a global cell it would be a single-writer hot spot on the
whole request path.

---

## Layout

```
worker/
├── domain/                PURE. No I/O, no framework, no clock of its own
│   ├── fsrs/              FSRS-6 scheduler + fuzz
│   ├── grading/           deterministic mcq/multi/idk grader, py-repr helpers
│   ├── jobs/              graph algebra, ledger, schedule, refusal, ids
│   ├── markdown/          the renderer (block, inline, links, tables, url)
│   ├── instant/           card hygiene, limiter arithmetic, ip parsing
│   ├── notify/wake.ts     when a user's next alarm should land
│   ├── anonCookie.ts merge.ts limits.ts trivia.ts pat.ts py.ts …
│   └── index.ts
├── app/                   use cases and PORTS. Policy, not plumbing
│   ├── ports.ts           every port in one file
│   ├── pageContext.ts     what a page needs, built by the use case
│   ├── viewmodels/        DTOs derived per template
│   ├── auth/              resolve.ts (precedence), mergeSaga.ts, reaper.ts
│   ├── agent/funding.ts   which credential funds a call, and whether any does
│   ├── jobs/              graph.ts (the four workflows as data) + step handlers
│   ├── decks/ study/ trivia/ notify/ offline/ settings/ dashboard/ instant/
│   ├── api/               v1.ts, mcp.ts, csv.ts, deckIo.ts, tools.ts
│   └── metrics.ts errors.ts http.ts entities.ts
├── runtime/               the worker, the cells, and the ADAPTERS
│   ├── worker.ts          the entry worker
│   ├── compose.ts         THE COMPOSITION ROOT. Only file that names adapters
│   ├── cells/             UserCell, DirectoryCell, InstantLimiterCell, JobCell
│   │   ├── router.ts      the cell-side route table + identity gates
│   │   ├── routes/        pages.ts, api.ts, jobs.ts, adapt.ts
│   │   └── seed/          parity seed profiles
│   ├── adapters/
│   │   ├── sql/           one repo per aggregate + schema.ts + migrate.ts
│   │   ├── agents/        anthropic, openaiCompat, byok, freeTier, select
│   │   ├── nunjucks/      the renderer, its shims, the icon global
│   │   └── clerk, anonCookie, pat, svix, byokCrypto, hkdf, webpush,
│   │       alarmLedgerRunner, clock, random, apkg, zip, …
│   ├── routes/            instant, metrics, openapi, legal, migrate
│   ├── env.ts             the whole env contract, typed
│   └── assets.ts sw.ts storage.ts buildToken.ts webhooks.ts
├── templates/             nunjucks templates, precompiled at build time
├── tests/                 vitest, mirroring the three layers
├── scripts/               build.mjs, build-domain.mjs, build-pages.mjs,
│                          run-node.sh, fsrs-oracle.mjs
└── wrangler.{dev,staging,prod}.jsonc    deploy contracts, public values only

static/                    BUILD INPUT for the worker, not a served tree
├── css/                   index.css entry + @layer + components/
├── js/                    the client modules (study, dashboard, offline, …)
├── icons/                 Phosphor Light SVGs, baked into build/icons.js
├── sw.js                  templated by the /sw.js route
└── cm/                    CodeMirror bundle source
```

### Layering, enforced

**runtime -> app -> domain, and nothing imports upward.**
`worker/tests/layering.test.ts` fails when:

1. `domain/` imports from `app/`, `runtime/`, `cloudflare:` or `node:`.
2. `app/` imports outside `domain/` + `app/`, or contains `fetch(`,
   `new Response`, `.sql.exec`, `DurableObject` or `nunjucks`.
3. Anything under `runtime/` other than `compose.ts` imports from
   `runtime/adapters/`.
4. Anything but the nunjucks adapter imports nunjucks or the compiled
   templates.

Cross-cutting concerns are wrappers at the composition root and in the
router: anon-cookie refresh/clear on the response path, `no-cache` on
HTML, request timing. A handler never touches them.

---

## Frontend architecture

**Philosophy.** Server-rendered HTML plus progressive-enhancement JS.
Server is the source of truth, HTML is the API, JS is sprinkles. No SPA
framework, no bundler, no Tailwind. Pages POST forms; JS adds polish.

**Where rendering runs.** Several page contexts read the database per
render, so every signed-in page renders **inside the `UserCell`**: one
activation, synchronous SQLite reads, one `pageContext` from the use
case. Unauthenticated pages (landing, privacy, offline shell, reauth
shell, errors) render in the entry worker, which has no database.

**The exception: the two surfaces that also run offline.** Each has to
render with no server (from the IndexedDB snapshot), so a
server-rendered version could not be the only one. Each is a set of
client components behind a port with two adapters, driven by hosts:

| surface | components | port | hosts | server renders |
| --- | --- | --- | --- | --- |
| study loop | `static/js/study/` | `CardSource` (`LocalSource` over IndexedDB + the JS grader/scheduler, `ServerSource` over `worker/app/study/api.ts`) | `study/online-host.js`, `offline/offline-app.js` | `study_shell.html`, `offline.html` |
| dashboard | `static/js/dashboard/` | `DeckSource` (`LocalSource` in `dashboard/local-source.js`, `ServerSource` in `dashboard/source.js` over `worker/app/dashboard/`) | `dashboard/online-host.js`, `offline/offline-app.js`, `dashboard/local-host.js` | `index.html`, `offline.html`, `landing.html` |

Consequences, all accepted:

- **Both surfaces require JS when signed in.** The one place the
  progressive-enhancement rule is deliberately broken. Re-adding a
  server-rendered deck list is a design change, not a fix: a second
  implementation of it is exactly what these components prevent.
- **A failed mount has to be visible.** The client-rendered region ships
  a fallback note as real markup (NOT `<noscript>`: a module that never
  loads fires nothing and scripting is on), which the host clears by
  replacing the region's children. Without it, a broken import chain
  reads as "you have no decks".
- **The data needs no round trip; the screen still waits for the module
  chain.** The dashboard shell embeds its first payload as JSON
  (`#dashboard-overview`), so nothing fetches before first paint. Two
  rules keep the window small: `LocalSource` lives in
  `dashboard/local-source.js` so the signed-in page never pulls the
  offline stack, and `index.html` declares the chain in
  `{% block head_preload %}`. Adding an import under
  `dashboard/components.js`, `source.js` or `online-host.js` means
  adding a `modulepreload` for it.
- **The landing page decides before it paints.** A visitor the server
  cannot identify, on a device that still holds a snapshot, gets the
  dashboard from `dashboard/local-host.js` instead of the splash.
  IndexedDB answers too late, so `store.js` mirrors "a snapshot is here"
  into `localStorage` (`prep:offline_snapshot`); a classic inline script
  in the landing `<head>` reads it and stamps `data-local-decks` on the
  root, and `landing.css` picks the region. A first-time visitor runs
  one `getItem` and gets the normal page.
  - The flag states what the stores HOLD, not that a sync ran. A flag
    written for an empty store paints the fallback note where the splash
    belongs.
  - The host's module chain is preloaded from the head script's `CHAIN`,
    not from markup: a `modulepreload` in `landing.html` would charge
    every visitor for a page almost none of them get.
  - Signing out leaves the snapshot in place by design; the wipe is the
    exit, not sign-out. Every destructive path goes through
    `static/js/offline/wipe.js`, which flushes the outbox first and
    wipes only when the queues came back empty. See
    [`docs/OFFLINE.md`](docs/OFFLINE.md).

### UX rails (do not violate without a reason)

- **No layout shift on interaction.** A control's bounding box must not
  change when tapped. Two-state buttons need `min-width` sized to the
  longer label. Loading states swap icon-for-spinner of equal size, not
  text-for-text of unequal width.
- **Every action must look responsive within ~50ms.** In order of
  preference: `data-submit-pending` on the form, an optimistic DOM
  update if the action is reversible, then a brief disabled state with a
  spinner. A redirect-and-full-render flow is fine only when the button
  itself shows pending state during the round trip.
- **Constant-size loading states.** Hide the current icon
  (`display: none`), render a `::before` spinner of the same size, keep
  the label. Do not swap the label for "Working..." unless the button
  has `data-pending-label` and the caller accepted the width change.

### CSS

`static/css/index.css` is the single entry: it declares the native
`@layer` order and `@import`s everything else.

```
static/css/
├── index.css      entry: @layer order + @import every other file
├── reset.css      minimal modern reset
├── tokens.css     :root design tokens (light + dark)
├── base.css       html / body / a / .icon
├── layout.css     page chrome (.paper centered column)
└── components/    one file per UI surface, kebab-case. mobile.css is
                   imported LAST so narrow-viewport overrides win.
                   spinners.css holds the shared keyframes.
```

Layer order: `reset, tokens, base, layout, components, utilities,
overrides`.

- **Adding a component**: create `components/<name>.css` and add
  `@import "./components/<name>.css" layer(components)` to `index.css`.
- **Narrow-viewport tightening** goes in `mobile.css`, not a `@media`
  block inside the component file.
- **Inline `style="..."`** is a smell EXCEPT for custom-prop data
  binding (`style="--progress: {{ pct }}%"`).
- **Naming**: kebab-case component classes. BEM inside a component file
  is fine but not required; `@layer` handles the specificity discipline
  BEM was invented for.

### JS

Native ES modules plus an importmap, no bundler. `templates/base.html`
declares an importmap aliasing `@/` to a build-versioned
`/static/js/v<token>/` and loads one bootstrap module, `app.js`.

**Convention**: app-wide behaviors register in `app.js`. Behaviors
driven by `data-*` attributes go through their module's
`attachDeclarative()`, so adding the attribute to a template wires the
behavior with no per-page boilerplate. Per-page inline
`<script type="module">` blocks are still allowed for logic that does
not generalize; extract only when the same pattern appears in three or
more templates.

Document a new `data-*` hook here when you add one.

### Templates

nunjucks (the JavaScript port of Jinja2), precompiled by
`scripts/build.mjs` into `build/templates.js`. Nothing is parsed at
request time.

```
worker/templates/
├── base.html      masthead + footer + importmap + bootstrap
├── partials/      included verbatim: {% include "partials/name.html" %}
├── macros/        parameterized components: {% import ... as ns with context %}
├── trivia/ notify/  per-surface subfolders
└── *.html         page templates
```

- **`with context` is required** when a macro references a global such
  as `root`. Imported macros are sandboxed by default.
- **Partial vs macro**: `include` for static chrome, `import` when the
  component takes arguments. Macros are functions; partials are not.
- **Page extension**: every page extends `base.html` and overrides
  `{% block title %}`, `{% block page_class %}`, `{% block main %}`.
  Do not add a top-level block unless several pages need it.
- The nunjucks shims (Python `%` formatting, slices, `tojson`,
  banker's rounding, `items()`) live in `runtime/adapters/nunjucks/`.
  A construct that silently evaluates to false or undefined in nunjucks
  but worked in Jinja is the failure mode to watch for.

### PWA + service worker

`static/sw.js` has two jobs: push (`push` + `notificationclick`), and
offline (precache the `/offline` shell and its styles, modules and
icons at install; serve the shell as a navigation fallback when the
network fails or hangs; serve precached subresources cache-first).
Nothing else is intercepted, so the online app behaves byte-identically
to a service-worker-less page.

The `/sw.js` route substitutes two placeholders before serving: the
deterministic build token and the JSON precache manifest. **Those
placeholder spellings must appear only at their definition sites** in
`sw.js`; substitution is a global string replace, so writing one out
anywhere else (a comment included) embeds a second copy of the
manifest.

iOS gotchas, battle-tested here:

- iOS 26 PWA standalone swallows the synthesized `click` on `<summary>`
  for ~5s after page load. `details-toggle.js` binds `pointerup` and
  suppresses the late compatibility click within 500ms.
- `<dialog>` backdrop-click-to-close is not native; `dialog.js` wires it
  via `data-dialog`.

---

## How AI work flows

Four job kinds, defined as **data** in `app/jobs/graph.ts`:

| kind | steps |
| --- | --- |
| `PlanGenerate` | `plan` (llm) -> `gate` (human) -> `expand` (llm, batch 4) -> `insert` (write, per item) |
| `Transform` | `compute` (llm) -> `gate` (human) -> `apply` (write) |
| `TriviaGenerate` | `generate` (llm) -> `insert` (write, per item) |
| `GradeAnswer` | `grade` (llm) -> `record` (write) |

A graph names each node's kind, retry policy, fanout mode, status string
and error behavior, so a workflow's shape is reviewable as a table
rather than as control flow. `app/jobs/index.ts` is the only file that
knows all four exist; the runner imports the registry, never a handler.

**One `JobCell` per job, driven by its own alarm.** Every decision comes
from the ledger rows, so an eviction, a node restart and a duplicate
alarm all reach the same one. Two rules the shape rests on:

- A caller-originated RPC (`start`, `signal`, `terminate`) never calls
  back into the owner's cell. The owner is mid-request when it calls,
  and a cell serves one request at a time. Everything that touches the
  owner happens on the alarm.
- The alarm is derived from the rows at the end of every RPC and in the
  constructor, never held, so a rolled-back RPC still converges.

**The status direction pays for the poll.** A `JobCell` writes into its
owner's `UserCell` and never the reverse. The 2s progress fragment and
the 5s badge read only `UserCell` rows, so a 300s LLM step blocks its
own `JobCell` and nothing else. The one `UserCell -> JobCell` hop is the
gate signal (accept / reject / feedback / apply), which happens on a
click. Progress travels with the status write, already rendered by the
job's partial.

**No retry on an LLM step.** Re-running a long prompt hides the real
failure for another long prompt, so the error reaches the user. Write
steps do retry, and they are idempotent through the per-cell
idempotency ledgers.

### Periodic work

Scheduled work is **per-user alarms**, not a fan-out over users. Each
`UserCell` computes its own next wake from its own state (digest hour in
its tz, the when-ready debounce against its next due card, each trivia
deck's backed-off refill interval, quiet hours) and arms
`storage.setAlarm`. It is re-derived on every prefs or deck write and on
activation, from persisted state, so a duplicate fire is a no-op.

`app/notify/wake.ts` keeps the reading and the doing separate:
`nextWakeAt` re-derives the wake after a write, and `runWake` reads the
same rows through the same function, so what the alarm is armed for and
what it does when it fires cannot drift.

**An alarm handler never calls the LLM.** The trivia refill dispatches a
`TriviaGenerate` job per due deck and returns.

The one remaining walk is the anonymous-retention sweep, in
`DirectoryCell`, whose alarm is re-derived from the sweep's own row on
every activation so an eviction resumes where it stopped.

---

## AI providers

Every AI call is a `fetch` from a cell. No SDK, no subprocess, no
sidecar.

`app/agent/funding.ts` decides which credential funds a call (policy
over rows, so it names no adapter); `runtime/adapters/agents/select.ts`
turns that answer into an adapter.

1. **BYOK**: the user's own Anthropic, OpenAI, or OpenRouter key,
   AES-256-GCM encrypted in their cell. Precedence when several are held
   and none is active: Anthropic, OpenRouter, OpenAI. OpenRouter also
   supports OAuth PKCE sign-in, which mints a key on the user's own
   account with no copy-paste.
2. **Shared free tier**: one OpenAI-compatible endpoint configured by
   env (`PREP_FREE_INFERENCE_*`), capped per generation. Optional.

**BYOK is API keys only. Do not add a Claude-subscription provider.**
A Claude Code OAuth token is rejected by the Messages API, and the one
sanctioned path for it bundles and spawns a large executable per call.

If a user holds BYOK rows but none yields a usable key, the call
**refuses** rather than falling through to the shared tier: silently
spending a credential the user opted out of is worse than an error. The
key is decrypted in the isolate that will use it and never held past the
call, so a revoked credential stops the next step.

---

## Auth

Precedence, stated once in `app/auth/resolve.ts`:

**signed-in > dormant session > anonymous cookie > visitor.**

- **Clerk** when its five vars are set; otherwise `NoIdentityProvider`
  and the deploy is anonymous-only with no sign-in page. Under parity
  mode a `FakeIdentityProvider` reads a trusted header instead.
- **The dormant step is load-bearing.** A returning user on a PWA cold
  launch has an expired session token and durable evidence of one.
  Falling through to a `prep_anon` cookie left on that browser would
  serve them their old guest account and break every recovery path keyed
  on "no user", so they get the reauth shell.
- **`prep_anon`**: HMAC-SHA256 over an HKDF-derived key. An anonymous
  visitor who generates a deck becomes a real user row with a real
  `UserCell`. Anonymous accounts are anonymous, not ephemeral.
- **PAT**: a bearer token for `/api/v1/*` and `/mcp`, matched against a
  SHA-256 hash in the owner's cell.

A signed-in request carrying an anonymous cookie triggers the **merge
saga** (`app/auth/mergeSaga.ts`). It spans three cells and has to be
resumable: markers in the `DirectoryCell`, rows copied between two
`UserCell`s, tombstone at the end.

Secrets never come from a wrangler file. They arrive at runtime as
`CELLD_VAR_*`; `runtime/env.ts` is the typed contract for all of them.

---

## Storage and migrations

One SQLite per cell, behind repositories under `runtime/adapters/sql/`.
Repos return entities, not rows.

Migrations run in `runtime/adapters/sql/migrate.ts` on cell activation
inside `blockConcurrencyWhile`, guarded and idempotent. There is a
`schema_version` per cell class. Adding a column means adding a guarded
step there plus the row mapping in the repo; the round-trip test catches
a half-done change.

---

## Observability

`GET /metrics` serves Prometheus text exposition with three histogram
families: `prep_ai_grade_duration_seconds`,
`prep_instant_generate_duration_seconds`, and
`prep_http_request_duration_seconds`. Names, labels, buckets and help
text are fixed: two targets in one scrape job that disagree on a
family's HELP is an inconsistency Prometheus reports.

**The registry is module-level, which on this runtime means per
isolate.** The counters belong to whichever isolate answered the scrape
and go when it is recycled. Nothing is per cell and nothing survives an
eviction. Do not write a query that assumes otherwise.

Scrape configuration and dashboards are operator-side and are not in
this repo.

---

## Dev ops

`make help` lists every target. The ones that matter:

```bash
make setup       # mise install + npm install + uv sync + git hooks
make build       # templates, icons, service worker, domain twins, dist/assets
make typecheck   # tsc over the worker and its tests
make test        # vitest
make ci          # lint + typecheck + test + the migration tool's suite
make dev         # build, deploy and start a local celld node on :8791
make dev-stop
make llm-stub    # the canned LLM a local node calls for AI flows
```

`worker/build/` and `worker/dist/` are generated and never committed.
Each step in `build.mjs` is a function so `tests/build.test.ts` can run
one against a scratch tree.

**Python in this tree is not the application.** It is the migration tool
(`migrate/`) and the browser and pixel harness (`tests/`), both driven by
their own make targets (`test-migrate`, `e2e`, `parity`). New
application code is TypeScript under `worker/`.

The pre-commit hook gates staged TypeScript on typecheck plus the whole
vitest suite. It runs in seconds; do not reach for `--no-verify` to get
around a red suite.

Deploy contracts are the three wrangler files. They carry **public
values only**: durable-object bindings, the assets directory, Clerk's
publishable configuration, and the timeout ceilings.
`CELLD_FETCH_TIMEOUT_S` in a wrangler file must match the node's own
setting; the worker takes the smaller of it and `PREP_JOB_LLM_TIMEOUT_S`
minus headroom so an LLM step gets its full budget.

---

## Gotchas worth knowing

**Parity mode is a local and staging-only switch.** `PREP_PARITY_MODE=1`
enables a fake identity provider that trusts a header, a pinned clock, a
seed endpoint and the probe job graphs. The composition root refuses it
on prod. Never set it on a deploy that serves real users.

**Importmap MUST appear in `<head>`, before any module script.** The
spec requires the importmap to be parsed before the first
`<script type="module">` that uses a bare specifier from it. An
importmap at the bottom of `<body>` silently kills every `import "@/..."`
in an inline module higher in the page, taking the behaviors wired there
with it. This has caused an outage. Keep it in `<head>`.

**Polling is htmx, not a JS state machine.** The progress partials
(`transform_progress`, `plan_progress`, `trivia_generating_progress`,
`workflow_badge`) carry `hx-get` plus `hx-trigger="every Ns"`. The
server controls the lifecycle: a non-terminal fragment includes the
trigger, a terminal fragment omits it and htmx stops. Do not reach for
`setInterval` in a new wait-for-backend flow.

**Closed `<dialog>` rendering inline at the page bottom.** Setting
`display: flex` on a dialog selector unconditionally overrides the UA's
`dialog:not([open]) { display: none }`. Gate any non-default display on
`[open]`.

**Modal scroll trap on iOS.** `100vh` includes the hidden URL bar. Use
`100dvh`, plus `body:has(dialog[open]) { overflow: hidden }` and
`overscroll-behavior: contain` to stop scroll chaining.

**A tuple or a Python literal in a template evaluates silently wrong.**
`x in ('a','b')` is false in nunjucks and `True` / `False` / `None`
resolve to undefined. Use `['a','b']` and `true` / `false` / `null`. The
shims cover the rest; a missing shim shows up as a blank branch, not an
error.

**`app/` may not contain `fetch(`, `new Response`, `.sql.exec`,
`DurableObject` or `nunjucks`.** The layering test greps for those
strings, so even a comment mentioning one fails it.

**A cell serves one request at a time.** Anything that would call back
into the caller's own cell deadlocks. That constraint is why job work
happens on alarms and why the status direction is one-way.

---

## What is intentionally NOT here

- **Streaming AI responses.** Every flow is one-shot; the polling UX
  covers the wait.
- **Per-token usage tracking.** Providers meter per account, so a
  per-token rollup models the wrong thing.
- **A deploy-wide AI credential.** Only a user's own key, or the
  optional shared free tier a deploy explicitly configures.
- **Operator and deploy tooling.** This repo is application code. Compose
  files, cluster manifests, deploy targets and secrets live in a private
  repo by design, and no host, IP, path or credential belongs in a file
  here.
- **Native mobile apps.** The PWA covers it.

---

## Versioning

Semver via git tags, cut from `main`. Pre-1.0 the minor/patch boundary is
permissive. `git describe --tags` shows the current version.
