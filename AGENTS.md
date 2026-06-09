# prep — working notes for future Codex sessions

What this file is: the doc you read first when picking up work on
prep. Skim it top-to-bottom, then dive into code. README.md is for
humans; this is for the agent.

---

## What prep is

A self-hosted spaced-repetition flashcard tool. Web app, runs in
docker. Users describe a topic; Codex turns it into a deck; users
study on an SRS schedule (10m → 1d → 3d → 7d → 14d → 30d).
Multi-user via Tailscale identity. Installs as a PWA. AI features
(generation, grading, transforms) are opt-in via a `Codex setup-token`
the user pastes through the UI.

---

## Architecture

```
browser (PWA)
   ↓ tailscale serve --set-path=/prep ---->  prep container :8082
                                                 │
                                  goreman supervises 3 procs:
                                                 │
                                    ┌────────────┼─────────────┐
                                    ↓            ↓             ↓
                                temporal      uvicorn       go worker
                                start-dev   (FastAPI app)  (workflows)
                                  :7233        :8082             │
                                                                 ↓
                                                     agent container :9999
                                                          │
                                                     Codex CLI inside
                                                     CLAUDE_CODE_OAUTH_TOKEN
                                                     → anthropic
```

Two containers, one compose project:

- **prep** — FastAPI (uvicorn) + Temporal devserver + Go Temporal
  worker, all in one container, supervised by goreman. The
  user-facing surface.
- **agent** — Node + Codex CLI + a tiny Go HTTP wrapper. The
  worker calls `POST /run` here for every Codex invocation. Token
  is stored in a persistent volume so it survives restarts.

Both volumes are env-named (`${ENV_NAME}-data`, `${ENV_NAME}-agent-data`)
so staging and prod can run side-by-side on one host without colliding.

---

## Layout

The python source is organized DDD-style — one package per bounded
context, each with its own entities / repo / service / routes split.
Domain logic (pure, I/O-free) lives under `prep/domain/`;
infrastructure adapters under `prep/infrastructure/`.

```
prep/
├── app.py                   FastAPI() bootstrap + middleware + mount routers
├── db.py                    re-export facade over prep.infrastructure.db +
│                            per-table accessors not yet split into context repos
├── icons.py                 Jinja `icon('name')` global → inlined Phosphor Light SVG
├── chat_handoff.py          builds prefilled URLs for the "Discuss this card" popover
├── temporal_client.py       Python helpers: start_grading, start_transform,
│                            start_plan_generate, signals/queries
├── notify/                  bounded context: web push + scheduler
│   ├── push.py              VAPID bootstrap + _send_one + send_to_user fanout
│   │                        + subscribe (the I/O side; no scheduling)
│   ├── scheduler.py         periodic tick loop: per-user digest / when-ready
│   │                        evaluation, quiet-hours, dispatch into trivia.tick
│   ├── entities.py          NotificationPrefs, PushSubscription
│   ├── repo.py              NotifyPrefsRepo, PushSubsRepo
│   └── routes.py            /notify/* HTTP surface
├── decks/                   bounded context: deck + question lifecycle
│   ├── entities.py          Deck, DeckSummary, Question, DeckCard, NewQuestion
│   ├── repo.py              DeckRepo, QuestionRepo
│   ├── service.py           use cases (sync CRUD + temporal-orchestrated plan/transform)
│   └── routes.py            /decks/*, /deck/*, /question/*, /transform/*, /plan/*
├── study/                   bounded context: study sessions + reviews
│   ├── entities.py          StudySession, RecentSession, Review, CardState
│   ├── repo.py              SessionRepo, ReviewRepo (re-exports StaleVersionError)
│   ├── service.py           start_session, submit_sync_answer, advance, abandon,
│   │                        async start_grading + grading_landed
│   └── routes.py            /study/*, /session/*, /grading/*
├── agent/                   bounded context: AI integration
│   ├── status.py            probe + structured status() + cached is_available
│   └── routes.py            /settings/agent + connect/disconnect to agent-server
├── auth/                    bounded context: identity + per-user prefs
│   ├── identity.py          current_user FastAPI dependency, Tailscale headers
│   ├── repo.py              UserRepo (upsert, editor_input_mode)
│   └── routes.py            /settings/editor
├── domain/                  PURE — no I/O, no DB, no FastAPI imports
│   ├── srs.py               SRS state machine (LADDER_MINUTES, advance_step, Verdict)
│   └── grading.py           deterministic mcq/multi/idk grader
├── infrastructure/
│   └── db.py                sqlite connection factory + cursor() + init() + now()
├── web/                     cross-cutting HTTP layer
│   ├── templates.py         Jinja2Templates instance + context processors
│   ├── responses.py         redirect() helper (root_path-aware)
│   ├── errors.py            friendly error pages + json-aware exception handlers
│   ├── pwa.py               /manifest.json + /sw.js
│   └── index.py             GET / (cross-cuts decks + study)
└── dev/
    └── preview.py           /dev/preview/* template fixtures (gated by PREP_DEV=1;
                             never set in prod images)

tests/                       per-context test pyramid
├── conftest.py              tmp-path sqlite, TestClient, initialized_db fixtures
├── test_smoke.py            pre-refactor characterization tests (still green)
├── domain/                  pure unit tests (SRS, grading)
├── decks/                   entity + repo (real sqlite) + service (fake client) + routes
├── study/                   same shape

worker-go/                   Go Temporal worker
├── agent/agent.go           Client interface + ShellAgent + HTTPAgent
├── cmd/agent-server/        agent container binary: /run + /healthz + /connect + /disconnect
├── workflows/               GradeAnswer, Transform, PlanGenerate
├── activities/              GradeFreeText, ComputeTransform, ApplyTransform,
│                            PlanCards, GenerateCardFromBrief, InsertCard
└── shared/types.go          Workflow input/output schemas

docker/
├── Dockerfile.prep          multi-stage: golang:1.26 (worker+goreman), oven/bun:1.1.0
│                            (cm-bundle), python:3.11-slim runtime with uv-installed
│                            venv + temporal CLI baked
├── Dockerfile.agent         node:22-slim + npm-installed Codex + go-built agent-server
└── Procfile.docker          temporal | uvicorn | worker, all under goreman in the prep
                             container

docker-compose.yml           prep + agent services, env-driven volume + image names
.env.example                 per-deploy config template (PORT, ROOT_PATH, ENV_NAME, ...)
deploy/{staging,prod}.env    tracked deploy-shape env files for `make deploy-{stag,prod}`
.prod-version                single-line tag pinning what's running in prod
.dockerignore                keeps build context lean (.venv, .git, build outputs, secrets out)
```

**DDD invariants worth preserving as the codebase grows:**
- `prep/domain/` imports nothing from bounded contexts or infrastructure.
  Pure functions + value objects only.
- Bounded-context modules import from each other only via entities or
  via the public shape of another context's service. No reaching into
  another context's repo directly.
- Routes call services (or repos for trivial reads). They don't
  call temporal_client or sqlite directly.
- Repos return entities, not dicts. The conversion happens at the
  boundary; templates and HTTP responses see entity-shape data.
```

---

## Frontend architecture

**Philosophy.** Server-rendered HTML + progressive-enhancement JS.
Server is the source of truth, HTML is the API, JS is sprinkles. No
SPA framework, no JS bundler, no Tailwind. Pages POST forms; JS adds
polish. Most actions degrade to plain forms.

### UX rails (don't violate without a reason)

- **No layout shift on interaction.** A control's bounding box should
  not change when it's tapped. Buttons with two label states (e.g.
  "pin" / "pinned") need `min-width` sized to the longer label so the
  toggle doesn't reflow neighboring elements. Loading states swap
  icon-for-spinner of equal size, not text-for-text of unequal width.
  Inline content with growable elements (counters, chips that flip
  state) should reserve their final width up front. We've been
  burned by this twice with submit-pending.js textContent swaps —
  if it shifts neighbors when toggled, fix the chrome, not the text.
- **Every action must look responsive within ~50ms.** Tap → nothing
  → page eventually reloads is bad UX even when the round-trip is
  legitimately slow. Feedback options, in order of preference:
  (1) `data-submit-pending` on the form so the button gets `is-loading`
  immediately, (2) optimistic DOM update if the action is reversible,
  (3) a brief disabled state with a spinner. The 303-redirect-to-
  full-page-render flow is fine but ONLY when the button itself
  shows pending state during the round-trip.
- **Constant-size loading states.** When a button enters `is-loading`,
  its width must not change. The shared CSS pattern is: hide the
  current icon (`display: none`), render a `::before` spinner of the
  same size; keep the label as-is. Do NOT replace the label with
  "Working…" unless the button has `data-pending-label` AND the
  caller has accepted the width change (e.g. full-width primary
  CTAs where the row collapses anyway).

### CSS

Single entry stylesheet (`static/css/index.css`) declares native
`@layer` order and `@import`s the rest:

```
static/css/
├── index.css      — entry: @layer order + @import every other file
├── reset.css      — minimal modern reset
├── tokens.css     — :root design tokens (light + dark vars)
├── base.css       — html / body / a / .icon / .icon-inline
├── layout.css     — page chrome (.paper centered column)
└── components/    — one file per UI component (~28 files); kebab-case
                     names match the surface (buttons, deck-list,
                     study-card, transform, trivia-card, …). mobile.css
                     is imported LAST so its narrow-viewport overrides
                     win. spinners.css holds shared keyframes
                     (rise/stamp/pulse/blink) referenced by other
                     component files.
```

**Layer order**: `reset, tokens, base, layout, components,
utilities, overrides`. The pre-overhaul `legacy.css` is gone — every
rule moved into a component file under `components/`.

**Adding a new component**: create `components/<name>.css`, add an
`@import "./components/<name>.css" layer(components)` to `index.css`.
For a narrow-viewport tightening, append rules to `mobile.css`
instead of inlining a `@media` block in the component file —
`mobile.css` is imported last and wins by source order.

**Splitting a fat component file**: when a single file (e.g.
deck-page.css at 622 LOC) covers multiple distinct surfaces, split
when adding a sibling becomes easier than grep-locating in one. No
hard cap — readability wins.

**Inline `style="..."` attrs**: smell EXCEPT for CSS custom-prop
data-binding (e.g. `style="--progress: {{ pct }}%"`). Anything else
belongs in a class.

**Naming**: simple kebab-case component classes (`.deck-card`,
`.transform-panel`, `.session-card`). BEM (`__elem--mod`) is fine
inside a component file but not required globally — `@layer`
handles the specificity discipline BEM was invented for.

### JS

Native ES modules + importmap, no bundler. `templates/base.html`
declares an importmap aliasing `@/` → `/static/js/` and loads a
single bootstrap module:

```
static/js/
├── app.js                    — bootstrap; initializes always-on
│                                behaviors + lazy-imports per-feature
│                                modules when their data-* hooks are
│                                present on the page.
└── modules/
    ├── details-toggle.js     — iOS-26 pointerup-bound <details>
    │                            toggle + outside-click + Esc close.
    │                            Always on (registered in app.js).
    ├── dialog.js             — backdrop-click close for
    │                            <dialog data-dialog>.
    ├── submit-pending.js     — disable + label-swap on submit for
    │                            <form data-submit-pending>.
    └── poller.js             — workflow polling helper (interval +
                                 visibilitychange + cache-bust +
                                 error backoff). Lazy-loaded.
```

**Convention**: behaviors that always need to run app-wide register
in `app.js`. Behaviors driven by data-* attrs go through their
module's `attachDeclarative()` so adding the attribute to a template
wires the behavior — no per-page boilerplate. Per-page modules with
custom logic on top of a shared utility import the utility directly
in a `<script type="module">` block.

**Data-* hooks** (current set; document new ones here when added):

| Attribute              | Module               | Behavior                              |
| ---------------------- | -------------------- | ------------------------------------- |
| `<dialog data-dialog>` | `dialog.js`          | backdrop click closes                 |
| `<form data-submit-pending>` | `submit-pending.js` | disable + label-swap on submit |
| `[data-poll-url]`      | `poller.js`          | poll URL on interval, dispatch handler|
| `[data-details-body]`  | `details-toggle.js`  | mark a sibling popover body so the outside-click handler doesn't close the related details when the body is tapped (use when a `<details>` body must live OUTSIDE the `<details>` element for layout reasons — e.g. trivia card explore body) |

**Per-page inline `<script>` blocks**: still allowed when the page
has unique logic that doesn't generalize (e.g. card-preview filling,
delete-deck-confirm typed-name match). Don't extract just to extract.
Extract only when the same pattern shows up in 3+ templates.

### Templates

Jinja's macros are the right "component" primitive. No reach for
django-cotton / django-components — Jinja's macro story is fine.

```
templates/
├── base.html         — masthead + footer + importmap + module bootstrap
├── partials/         — _name.html → name.html, included verbatim
│                       with {% include "partials/name.html" %}
├── macros/           — parameterized "components" called as
│                       {{ ns.foo(args) }} after
│                       {% import "macros/<file>.html" as ns
│                          with context %}
├── trivia/           — bounded-context subfolder (mirrors prep/trivia/)
├── notify/           — bounded-context subfolder
└── *.html            — page templates (one per route)
```

**`with context` is required** when a macro references
`request.scope.get('root_path','')` or any other Jinja global —
imported macros are sandboxed by default, `with context` exposes
the caller's context. Macros that don't touch globals can skip it.

**Partial vs macro**: `{% include "partials/foo.html" %}` for static
chrome; `{% import "macros/foo.html" as ns with context %}` when the
component takes arguments. Macros are functions; partials aren't.

**Page extension**: every page extends `base.html` and overrides
`{% block title %}`, `{% block page_class %}`, `{% block main %}`.
Don't introduce new top-level blocks unless multiple pages need
them.

### PWA + service worker

`static/sw.js` handles `push` and `notificationclick` events only —
no fetch caching. App is on-tailnet with a fast server; an app-shell
caching layer would mainly create stale-content debugging headaches.
Add caching only when there's a concrete reason.

iOS gotchas (battle-tested in the codebase):
- iOS 26 PWA standalone swallows the synthesized `click` event on
  `<summary>` for the first ~5s after page load. Fix is in
  `details-toggle.js`: bind to `pointerup`, suppress the late
  compatibility click within 500ms.
- `<dialog>` backdrop-click-to-close is not native; wired by
  `dialog.js` via `data-dialog`.

---

## How AI work flows

Generation example (the plan-first flow at `/decks/new` action=plan):

1. FastAPI `/decks/new` POST creates deck row, then
   `temporal_client.start_plan_generate` kicks off `PlanGenerateWorkflow`
   on the worker.
2. Workflow calls activity `PlanCards` → worker's `Cfg.Agent.Run(prompt)`
   → over HTTP to the agent container → spawns `Codex -p <prompt>` with
   `CLAUDE_CODE_OAUTH_TOKEN` in env → returns stdout.
3. Workflow stores plan, query handler exposes it. UI polls
   `/plan/<wid>/status` and renders the brief outline.
4. User signals `feedback` (replan), `accept` (expand), or `reject`.
5. On accept: workflow `ExecuteActivity` for each `PlanItem` in
   parallel — N concurrent `Codex -p` calls — gathers results,
   writes via `InsertCard` activity (idempotency via
   `questions_idempotency` table).

The agent boundary (`worker-go/agent/agent.go`) is the seam. Two
implementations:
- **ShellAgent** — `exec.Command(Codex, args, prompt)`, used when
  `PREP_AGENT_BIN` is set. Native (no docker) dev or legacy.
- **HTTPAgent** — `POST <url>/run`, used when `PREP_AGENT_URL` is
  set. The container path. `FromEnv()` picks based on env.

---

## Schema migrations

`db.init()` runs on every app boot and is idempotent. Add a column?
Check `PRAGMA table_info(<table>)` first, then ALTER. Existing
examples: `editor_input_mode`, `notification_prefs`, `context_prompt`.

**FK CASCADE gotcha (fixed in v0.4.1).** If you ever rebuild a
table that's referenced by an FK, follow the SQLite-recommended
pattern: `PRAGMA foreign_keys=OFF` OUTSIDE any transaction, then
`BEGIN; ...rebuild...; PRAGMA foreign_key_check; COMMIT;`. A naive
`DROP TABLE decks` cascades through questions/cards/reviews and
wipes user data. Don't regress.

---

## Auth

Header-based via Tailscale Serve. The proxy injects
`Tailscale-User-Login: user@tailnet`. `current_user(request)` reads
that header, calls `db.upsert_user(...)`, returns the user dict.

For `make dev` and self-host single-user setups, `PREP_DEFAULT_USER`
in `.env` makes every header-less request that user. **Don't set it in
multi-user prod** — anyone hitting the URL becomes that user.

All user-owned tables (decks, questions, study_sessions, cards,
reviews, push_subscriptions) carry `user_id` and every db.py accessor
takes user_id first. IDOR via guessed IDs is blocked by the WHERE
clauses (cross-user lookups return None as if the row didn't exist).

---

## Common dev ops

| You change | What runs |
|---|---|
| `app.py`, `db.py`, any `*.py` | uvicorn `--reload` picks it up (<1s) |
| `templates/*.html` | jinja auto-reloads per request |
| `static/*.css`, `static/icons/*` | hard-refresh browser |
| `static/cm/` (CodeMirror source) | `cd static/cm && bun run build` |
| `worker-go/**/*.go` | `Ctrl-C` make dev, `make build`, `make dev` |
| `worker-go/cmd/agent-server/` | rebuild agent image: `docker compose build agent && docker compose up -d agent` (or run `go run ./cmd/agent-server` standalone for fast iteration) |
| Schema change | edit `db.init()`, restart `make dev` |
| New dep (python) | `mise exec -- uv add <pkg>` |
| New dep (go) | `cd worker-go && mise exec -- go get <mod>` |
| Lint / format | `make lint` (read-only) / `make format` (writes). Pre-commit hook runs the same checks against staged files. |
| New icon | `curl -o static/icons/<n>.svg https://raw.githubusercontent.com/phosphor-icons/core/main/assets/light/<n>-light.svg` |

To validate ad-hoc: `docker compose build && docker compose up -d`.
For the staging-vs-prod two-stack split, use `make deploy-stag` /
`make deploy-prod` (see below).

---

## Deploy model (single checkout, two stacks)

One checkout (`prep-app-staging/`, on `main`), two compose stacks
running on the same docker daemon:

- **stag** — image `prep:staging`, project `stag`, host port 8082,
  volumes `prep-data` + `prep-agent-data`. Tailscale serves at
  `/prep-staging/`. Built from current working tree on every
  `make deploy-stag`.
- **prod** — image `prep:<tag>`, project `prod`, host port 8081,
  volumes `prod-data` + `prod-agent-data`. Tailscale serves at
  `/prep/`. Built from `git worktree add --detach <tag>` against
  whatever tag is in `.prod-version`. The working tree never moves
  during a prod build.

**Source of truth for "what is prod"** = `.prod-version` (single
line, e.g., `v0.13.3`). Tracked in git so `git log .prod-version` is
the prod-deploy history.

**Per-stack config** lives in `deploy/staging.env` and
`deploy/prod.env` (tracked). A local `.env` (gitignored) layers on
top for per-machine overrides. `PREP_DEFAULT_USER` is deliberately
unset in both deploy env files — both stacks enforce real Tailscale
auth.

**Promote flow**:
```bash
# tag whatever's on main
git tag -a v0.X.Y -m "..."
git push origin --tags

# promote: writes .prod-version, commits, pushes, builds at the tag,
# brings up prod stack
make promote v=v0.X.Y
```

`make deploy-prod` (without promote) just redeploys whatever
`.prod-version` already says — idempotent. Use it after editing
`deploy/prod.env` or to recreate prod containers from the same tag.

**Important — wait for go-ahead before prod.** Default to
`make deploy-stag` during a session; wait for the user to say
"deploy prod" or equivalent before running `make promote` or
`make deploy-prod`.

Image tags are versioned (`prep:staging`, `prep:v0.13.3`) so all
historical prod images coexist in the daemon's cache; running
containers hold a reference to the image by ID, so a staging rebuild
can't displace prod's bytes.

Tailscale Serve direct mounts handle path routing (`tailscale serve
--set-path=/prep ...` and `--set-path=/prep-staging ...`) — no
reverse proxy required.

---

## Observability

prep emits Prometheus metrics + structured logs to the LGTM-stack
running under `~/Dropbox/workspace/macmini/observability/`.

**Metrics** (`prep/web/metrics.py`, exposed at `GET /metrics`):
- `prep_anyio_threadpool_borrowed` / `_capacity` (gauge). Sampled
  just-in-time on every scrape. **The leak/exhaustion canary** for
  threadpool exhaustion: sustained borrowed approx. capacity means
  sync handlers piling up on the threadpool (a known prod-down
  failure mode).
- `prep_claude_grade_duration_seconds{verdict}` — histogram. Labels:
  `right`, `wrong`, `unknown`, `fallback_unavailable`, `fallback_bad_json`.
  Buckets up to 30s so the 12s timeout tail is visible.
- `prep_http_request_duration_seconds{method, route, status}` —
  histogram. Route label is the FastAPI template form (`/deck/{name}`)
  not the raw URL — keeps cardinality bounded.

The `/metrics` endpoint sits on the same port as the FastAPI surface
(no separate process — single uvicorn = single registry). The HTTP
middleware in `prep.web.metrics` records request latency before any
router runs; `/metrics` itself is excluded so scrape calls don't
pollute the histogram. `claude_grade` calls `observe_claude_grade()`
directly from `prep.trivia.service`.

**Logs** flow to Loki automatically — Promtail in the obs-stack
auto-discovers every docker container's stdout/stderr. Query in
Grafana → Explore with `{compose_project="prod"}` (or `stag`).

The `prep` logger tree is configured in `prep/app.py` with a
StreamHandler at INFO (override via `PREP_LOG_LEVEL`); use
`logger = logging.getLogger(__name__)` in any module — info-level
messages reach stdout (and thus Loki) by default.

**Prometheus scrape config** lives in
`~/Dropbox/workspace/macmini/observability/prometheus/prometheus.yml`
under the `prep-prod` and `prep-staging` jobs (`host.docker.internal:8081`
and `:8082` respectively, `metrics_path: /metrics`). After editing,
`make reload-prom` from the obs/ dir picks up changes without a restart.

**Traces are NOT emitted yet.** Tempo/Jaeger isn't part of the
obs-stack today; logs + metrics carry the load. If a future debug
session needs request-level traces, OTel + Tempo is the canonical
upgrade — hold off until there's a concrete need.

---

## Gotchas worth knowing

**`tailscale serve --set-path=/prep` strips the prefix when
forwarding.** uvicorn must be launched with `--root-path $ROOT_PATH`
or static assets 404. Procfile.docker handles this; if you change
the run command, keep the flag.

**uvicorn `--no-access-log` is on in prod**, so per-request
diagnostics need `docker compose logs -f` for app errors only. Add
`--log-level debug` to Procfile.docker for verbose tracing.

**`Codex setup-token` output prefix**: `sk-ant-oat01-…`. The
agent-server's `/connect` validates that prefix. Other token shapes
(API keys, OAuth URLs) are rejected with a friendly error.

**Worker boots before temporal under goreman.** `dialTemporalWithRetry`
in `worker-go/main.go` retries dial up to 60s with exponential
backoff. If you change the worker startup, preserve that.

**Volume mounts SHADOW Dockerfile RUN mkdir**. e.g. the prep image
RUNs `mkdir /data/temporal` at build time, but compose mounts the
`prep-data` volume on top of `/data` and erases that mkdir. Procfile.docker's
temporal command does `sh -c 'mkdir -p /data/temporal && exec temporal …'`
to compensate. Same pattern if you add other dirs in /data.

**Closed `<dialog>` elements rendering inline at page bottom.** If
you set `display: flex` on a dialog selector unconditionally, you
override the UA's `dialog:not([open]) { display: none }`. Always
gate flex (or any non-default display) on `[open]`.

**Modal scroll trap on iOS.** `100vh` includes hidden URL bar;
modals hit `100dvh` and use `body:has(dialog[open]) { overflow:
hidden }` plus `overscroll-behavior: contain` to prevent
scroll-chaining.

**Anthropic prohibits embedding subscription OAuth in third-party
apps** (Feb 2026 policy). prep uses `Codex setup-token` (officially
blessed) instead — user generates the token on a machine they
control, pastes into the UI. Don't try to wrap `Codex auth login`.

**Importmap MUST appear in `<head>`, before any module script.**
The HTML spec requires the importmap to be parsed before the first
`<script type="module">` that uses one of its bare specifiers. If
the importmap is at the bottom of `<body>` (legacy layout) and a
page renders an inline module script higher in the body via
`{% block main %}`, the inline script's `import "@/..."` silently
dies at parse time — taking every behavior wired up in that block
with it (polling, click handlers, etc.). This pattern has caused a
production outage where htmx polling + refresh-link in transform.html
stopped working entirely. Fix: keep importmap in `<head>` always.

**Polling pattern is htmx, not JS modules.** `templates/transform.html`,
`plan.html`, `grading.html`, `trivia/generating.html` use
`hx-get="/<resource>/{wid}/fragment" hx-trigger="every 2s"` to poll
status fragments. Server controls polling lifecycle: a non-terminal
fragment includes `hx-trigger="every 2s"`, a terminal fragment omits
it → htmx auto-stops. No client state machine. The dead pattern (a
JS `startPoller` module + `setInterval` + `visibilitychange`) is gone
from the codebase. If you add a new "wait for backend, swap UI"
flow, follow the htmx pattern; don't reach for `setInterval`.

**Don't block HTTP routes on `await handle.result()`.** Temporal's
`handle.result()` long-polls for workflow completion. Calling it
from a request handler hangs the user's button-press until the
workflow fully winds down (seconds, sometimes longer). Especially
in apply/reject style routes: signal the workflow, then return the
status fragment immediately (htmx swaps it in). The workflow
exposes intermediate states (`applying`, `rejecting`) precisely so
the UI shows truth-of-state without lying or blocking. If you need
the result, query a stored row via the repo, not the live workflow.

**Worker capacity starves under panic-loops.** A workflow whose
definition changed in a non-deterministic way (e.g. you renamed a
timer ID, reordered activities) will panic on replay and the SDK
retries forever (attempt 500+, log spam). Each such workflow
consumes a worker slot every retry. Two such workflows can starve
the prep-generation task queue completely — new transforms sit in
`ActivityTaskScheduled` for minutes. **Before you change workflow
code that affects already-in-flight workflows: terminate them, or
cordon the deploy until they finish.** `temporal -n <ns> workflow
list` to see what's in flight; `temporal workflow terminate -w …`
to clean up.

**The agent's `/healthz` lies about Codex login state.** It
returns `{"ok":true,"logged_in":true}` if a token *file* exists on
disk, regardless of whether the token actually works. To probe real
auth, you have to run `Codex -p "test"` inside the agent
container. Plan to fix /healthz to do a real auth probe; for now,
don't trust its `logged_in` field.

---

## Testing + promote gates

**3-layer pyramid (in order of fastness, run by `make ci`):**

1. **`make lint`** — `ruff format --check`, `ruff check`, `go vet`,
   `gofmt -l`. <2s, no infra.
2. **`make test`** — `pytest -x` against in-process FastAPI with
   mocked temporal/Codex. ~10s. The 396-test characterization +
   route + service + repo + entity suite. **Catches**: shape
   regressions, IDOR, contract drift between route + service. Does
   NOT catch: anything requiring a real worker, real Codex, real
   browser JS execution.
3. **`make e2e`** — `pytest tests/e2e/` against deployed staging.
   Two flavors live side-by-side under `tests/e2e/`:
   - **httpx tests** (test_smoke.py, test_ai_flows.py): full HTTP
     round-trip — create-deck → study a card → grade → drive a
     transform/plan to terminal. **Catches**: route-template-
     temporal-Codex integration, IDOR, redirect shape, htmx-trigger
     leaks (server-side polling lifecycle).
   - **browser tests** (test_browser_smoke.py, marked `slow` +
     `browser`): drive Chromium via Playwright at iPhone-15-Pro
     viewport. **Catches**: inline `<script type="module">` parse +
     execute (the importmap-ordering bug class), htmx polling
     actually firing client-side, in-place fragment swaps
     (no navigation on accept/reject), `HX-Redirect` followed by the
     browser. Without these, the server returning the right HTML is
     a green light even when every page's JS is dead.

**`make promote v=v0.X.Y`** chains: `deploy-stag-from-tag` →
`make lint test e2e` → write `.prod-version` + commit + push →
`make deploy-prod`. Any step failing aborts before mutating prod.
The lint+test+e2e tail is also `make ci`.

**Browser test prerequisites.** Playwright + chromium binary live
in the dev venv (`uv sync --group dev` installs the python package;
the chromium binary needs an explicit `uv run playwright install
chromium` after install — it lives under `~/Library/Caches/ms-
playwright/`). `make e2e` warns if either is missing. In CI / on
the mac mini box both are already present. Browser tests can be
skipped during fast iteration via `pytest -m "not browser"
tests/e2e/`. Total wall time: 6 browser tests in ~36s on a warm
mac mini against staging.

**Browser-test fixture gotcha.** The `page` fixture in
`tests/e2e/conftest.py` injects the `Tailscale-User-Login` header
via `ctx.route()` rather than `extra_http_headers`. The latter
applies to every request, including cross-origin asset fetches
(Google Fonts etc.), which trip CORS preflight rejections because
the upstream doesn't whitelist the header in
`Access-Control-Allow-Headers`. Those preflight failures show up
as `console error: Failed to load resource: net::ERR_FAILED` and
trigger false positives in the inline-module-script test. Route-
based injection scopes the header to the prep app's origin; don't
revert that without a reason.

**TDD invariant.** New routes get tests in the same commit. Tests
go in the matching `tests/<bounded-context>/test_routes.py` (or
`test_service.py`). E2e gets one test per user-visible flow in
`tests/e2e/test_smoke.py`. If you add a status-bearing workflow,
add an e2e that drives it to terminal — don't ship blind.

---

## What's intentionally NOT here

- ANTHROPIC_API_KEY support. We use the Codex subscription path on
  purpose. The agent-server inherits whatever auth `Codex setup-token`
  produced; users opt in once.
- A test suite. Exercise via UI. Adding one is a known gap.
- Cloud SaaS / multi-tenant auth. prep is single-tenant
  per-deployment.
- Mobile native apps. PWA covers it.

---

## Versioning

Semver via git tags. Pre-1.0 we're permissive about minor-vs-patch
boundaries. Tag from `prep-app-staging/` (where `main` lives), then
checkout that tag in `prep-app/` (the prod checkout) and `docker
compose build && up -d`.

Current version visible via `git describe --tags` in either checkout.
