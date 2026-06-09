# prep — working notes for future Claude sessions

What this file is: the doc you read first when picking up work on
prep. Skim it top-to-bottom, then dive into code. README.md is for
humans; this is for the agent.

---

## What prep is

A self-hosted spaced-repetition flashcard tool. Web app, runs in
docker. Users describe a topic; Claude turns it into a deck; users
study on an SRS schedule (10m → 1d → 3d → 7d → 14d → 30d).
Multi-user via Tailscale identity. Installs as a PWA. AI features
(generation, grading, transforms) are opt-in via a `claude setup-token`
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
                                                       POST /api/agent/run
                                                       (X-Internal-Token)
                                                                 │
                                                                 ↓
                                                  prep.agent.sdk_adapter
                                                  (claude-agent-sdk in-process)
                                                  CLAUDE_CODE_OAUTH_TOKEN
                                                       → anthropic
```

**Single container, one compose project.** Everything runs inside
`prep`: FastAPI (uvicorn) + Temporal devserver + Go worker, all under
goreman. AI calls go through `prep/agent/`'s SDK adapter (the
Python `claude-agent-sdk` package) — no separate agent container,
no `claude` CLI binary, no HTTP hop to a sidecar.

The Go worker still needs to invoke AI work, but rather than calling
out to a sidecar container it POSTs `/api/agent/run` against its own
prep host (`http://localhost:8082/api/agent/run`). The route is gated
by an `X-Internal-Token` header (shared secret in `PREP_INTERNAL_TOKEN`,
fail-closed if unset) so the endpoint can't be hit from outside the
container. Token + secret are unique-per-deploy; never overlap stag
and prod.

OAuth token lives at `/data/claude-oauth-token` (0600) inside the
single `${ENV_NAME}-data` volume — written by the `/settings/agent/connect`
form when the user pastes a `claude setup-token` output, loaded into
`CLAUDE_CODE_OAUTH_TOKEN` on app boot. Deleting the file (or hitting
`/settings/agent/disconnect`) wipes auth in-place.

**History:** the architecture used to be two containers (prep + an
`agent` sidecar running Node + Claude Code CLI + a Go HTTP wrapper at
port 9999), but a migration to the in-process `claude-agent-sdk`
collapsed it. The sidecar binary at `worker-go/cmd/agent-server/` is
retired.

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
│   ├── port.py              AgentPort Protocol + AgentResult dataclass +
│   │                        AgentUnavailable / AgentBudgetExhausted exceptions
│   ├── sdk_adapter.py       AgentPort impl via `claude-agent-sdk` (in-process,
│   │                        no CLI binary). Late-imports the SDK.
│   ├── fake.py              FakeAgent — test double; record calls, return canned text
│   ├── token_store.py       atomic 0600 write/read of /data/claude-oauth-token
│   ├── status.py            probe + structured status() + cached is_available
│   └── routes.py            /settings/agent + connect/disconnect + /api/agent/run
│                            (worker-callable, X-Internal-Token gated)
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
├── agent/agent.go           Client interface + HTTPAgent (POSTs prep's
│                            /api/agent/run with X-Internal-Token)
├── workflows/               GradeAnswer, Transform, PlanGenerate
├── activities/              GradeFreeText, ComputeTransform, ApplyTransform,
│                            PlanCards, GenerateCardFromBrief, InsertCard
└── shared/types.go          Workflow input/output schemas

docker/
├── Dockerfile.prep          multi-stage: golang:1.26 (worker+goreman), oven/bun:1.1.0
│                            (cm-bundle), python:3.11-slim runtime with uv-installed
│                            venv (incl. claude-agent-sdk) + temporal CLI baked
└── Procfile.docker          temporal | uvicorn | worker, all under goreman in the prep
                             container

docker-compose.yml           single `prep` service, env-driven volume + image names
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
   → POST `http://localhost:8082/api/agent/run` with `X-Internal-Token`
   header → prep's FastAPI route hands off to `prep.agent.get_agent()`
   (the `ClaudeAgentSdkAdapter`) → in-process `claude-agent-sdk` call →
   returns text from `AssistantMessage` chunks + cost/usage from the
   final `ResultMessage`.
3. Workflow stores plan, query handler exposes it. UI polls
   `/plan/<wid>/status` and renders the brief outline.
4. User signals `feedback` (replan), `accept` (expand), or `reject`.
5. On accept: workflow `ExecuteActivity` for each `PlanItem` in
   parallel — N concurrent SDK calls through `/api/agent/run` —
   gathers results, writes via `InsertCard` activity (idempotency
   via `questions_idempotency` table).

**Two seams worth knowing:**

- **`AgentPort` (prep/agent/port.py)** — the Python-side abstraction.
  `ClaudeAgentSdkAdapter` is the production impl; `FakeAgent` is the
  test double. `get_agent()` returns the singleton; `set_agent()`
  swaps for tests. Errors surface as `AgentUnavailable` (generic
  failure → 502) or `AgentBudgetExhausted` (the user blew their
  monthly credit pool → 429 + `kind: budget_exhausted` so the UI can
  show a specific message).
- **Worker's `HTTPAgent` (worker-go/agent/agent.go)** — the Go-side
  HTTP client. Configured via `PREP_AGENT_URL` (host) +
  `PREP_INTERNAL_TOKEN` (shared secret). It POSTs the same wire
  format the old sidecar's `/run` accepted (`{prompt, session_id?,
  resume_id?}` → `{stdout}`), so the worker stayed unchanged across
  the SDK migration apart from a one-line env-var flip.

**Auth model:** the user runs `claude setup-token` on a machine they
control, pastes the resulting `sk-ant-oat01-…` token into
`/settings/agent/connect`. Prep writes it to
`/data/claude-oauth-token` (0600) and stamps `CLAUDE_CODE_OAUTH_TOKEN`
into the live process env so the SDK adapter can use it without a
restart. The token authenticates against the user's Claude
subscription credit pool (Max 20x = ~$200/mo, post Anthropic's
2026-06-15 SDK-credit-eligibility change), not a separate API-key
billing account.

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
| `prep/agent/sdk_adapter.py` | uvicorn `--reload` (in-process; no separate container to rebuild) |
| Schema change | edit `db.init()`, restart `make dev` |
| New dep (python) | `mise exec -- uv add <pkg>` |
| New dep (go) | `cd worker-go && mise exec -- go get <mod>` |
| Lint / format | `make lint` (read-only) / `make format` (writes). Pre-commit hook runs the same checks against staged files. |
| New icon | `curl -o static/icons/<n>.svg https://raw.githubusercontent.com/phosphor-icons/core/main/assets/light/<n>-light.svg` |

To validate ad-hoc: `docker compose build && docker compose up -d`.
For the staging-vs-prod two-stack split, use the operator's
`make -C infra/prep deploy-devel` / `deploy-prod` (see below).

---

## Deploy model (single checkout, THREE deploys)

**Operator targets live in a separate private repo.** The
`make deploy-devel`, `make deploy-prod`, `make deploy-vps`,
`make promote`, `make promote-vps`, `make logs-*`, `make down-*`
commands referenced throughout this section are defined in
`infra/prep/Makefile` (operator's private infra repo) and invoked
via `make -C ~/Dropbox/workspace/macmini/infra/prep <target>`. The
shapes / pin files / flow described below are unchanged; only the
location of the Makefile moved.

One checkout (on `main`), three deploys built from it. Two stacks
share a local docker daemon (stag + prod-tailnet, both tailnet-only);
the third is the public-internet deploy of prepcards.app on a remote
host.

- **stag** (local, tailnet) — image `prep:staging`, project `stag`,
  host port 8082, single `prep-data` volume. Tailscale serves at
  `/prep-staging/`. Tailscale auth. Built from current working tree
  on every `make deploy-stag`. No pin file (always the latest commit).
- **prod-tailnet** (local, tailnet) — image `prep:<tag>`, project
  `prod`, host port 8081, single `prod-data` volume. Tailscale serves
  at `/prep/`. Tailscale auth. **Single-user** (operator's own /prep
  instance). Built from `git worktree add --detach <tag>` against the
  tag in `.prod-version`.
- **prepcards.app** (remote host, public internet) — image `prep:vps`,
  compose project `prep`. Reverse proxy at the remote host terminates
  TLS and forwards to the container on :8082. **Clerk auth,
  multi-user.** Built from the tag in `.vps-version` via
  `make deploy-vps` (SSHes to the host, `git fetch --tags && git
  checkout <tag> && docker compose build && up -d`). Operator-side
  overlay (compose.yml + .env with Clerk + token secrets) lives
  OUTSIDE this repo: the Makefile points at it via
  `OPS_DEPLOY_DIR ?= <operator-managed-path>`. Pattern documented in
  the shared infra repo's `APP-PATTERN.md`.

**Two pin files (DON'T conflate them):**
- `.prod-version` → local /prep (tailnet, single-user)
- `.vps-version` → prepcards.app (public, multi-user)

**Per-deploy config** lives in `deploy/staging.env` + `deploy/prod.env`
(tracked, used by the local stacks) and an operator-managed `.env`
at `$OPS_DEPLOY_DIR/.env` on the VPS (NOT in git, holds Clerk keys +
PREP_AUTH_MODE=clerk + secrets). A local `.env` (gitignored) layers
on top of the local stacks for per-machine overrides.
`PREP_DEFAULT_USER` is deliberately unset in all three deploys: every
request must authenticate.

**Promote flow** (tag once, promote per-target):
```bash
# tag whatever's on main
git tag -a v0.X.Y -m "..."
git push origin --tags

# promote to local /prep (tailnet, single-user): writes .prod-version,
# commits, pushes, builds at the tag, brings up local prod stack.
make promote v=v0.X.Y

# promote to prepcards.app (public, multi-user): writes .vps-version,
# commits, pushes, SSH'd build + up on the VPS.
make promote-vps v=v0.X.Y
```

`make deploy-prod` / `make deploy-vps` (without `v=`) redeploy whatever
the respective pin already says (idempotent). Use after editing the
deploy env file or to recreate containers from the same tag.

**"Promote to prepcards.app" = `make promote-vps`, not `make promote`.**
The two are NOT interchangeable. `make promote` only updates the
tailnet /prep instance; `make promote-vps` updates the public
multi-user deploy. Past failure mode: confusing the two and believing
the public deploy got a security-relevant change when only the
tailnet instance did. When in doubt about which deploy the user
means, check both pin files (`cat .prod-version .vps-version`) and
the running images.

**Important: wait for go-ahead before any prod deploy.** Default to
`make deploy-stag` during a session; wait for the user to say "deploy
prod" / "promote" / equivalent before running `make promote`,
`make deploy-prod`, `make promote-vps`, or `make deploy-vps`. For
security-relevant changes, prefer rolling out to prepcards.app FIRST
(highest stakes, multi-user), confirm visually, then promote to the
tailnet /prep too.

Image tags are versioned (`prep:staging`, `prep:v0.13.3`) so all
historical prod images coexist in the daemon's cache; running
containers hold a reference to the image by ID, so a staging rebuild
can't displace prod's bytes.

Tailscale Serve direct mounts handle path routing (`tailscale serve
--set-path=/prep ...` and `--set-path=/prep-staging ...`) — no
reverse proxy required.

---

## Observability

prep emits Prometheus metrics + structured logs to an operator-side
LGTM-stack (Loki + Grafana + Tempo + Mimir) running on the same
docker daemon.

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

**Prometheus scrape config** is operator-side (lives in the obs
stack's checkout, not this repo). The jobs target
`host.docker.internal:8081` (prod) and `:8082` (staging) on
`metrics_path: /metrics`.

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

**`claude setup-token` output prefix**: `sk-ant-oat01-…`. The
`/settings/agent/connect` route validates that prefix. Other token
shapes (API keys, OAuth URLs) are rejected with a friendly error.

**`/api/agent/run` is fail-closed without `PREP_INTERNAL_TOKEN`.**
The route's `_require_internal_token` dependency raises 503 if the
env var is unset, and 401 if the request's `X-Internal-Token` header
doesn't match. Both stag and prod set the var via `deploy/<env>.env`;
local dev (`make dev`) needs it too if you want the worker → prep
path to work. NB stag and prod MUST use distinct values — never
share secrets across environments.

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
apps** (Feb 2026 policy). prep uses `claude setup-token` (officially
blessed) instead — user generates the token on a machine they
control, pastes into the UI. Don't try to wrap `claude auth login`.

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
it (htmx auto-stops). No client state machine. The dead pattern (a
JS `startPoller` module + `setInterval` + `visibilitychange`) was
removed. If you add a new "wait for backend, swap UI" flow, follow
the htmx pattern; don't reach for `setInterval`.

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

**`prep.agent.status()` is file-presence-only.** The probe checks
whether `CLAUDE_CODE_OAUTH_TOKEN` is in env OR the token file exists
at `/data/claude-oauth-token` — NOT whether the token actually
authenticates. To probe real auth, call the SDK once. Cheap
end-to-end check from inside the container:
`docker exec stag-prep-1 .venv/bin/python -c "import asyncio; from prep.agent import get_agent; print(asyncio.run(get_agent().run('hi')).text[:80])"`.

**`/api/agent/run` is best-effort idempotent.** Each call makes a
fresh SDK request — there's no de-dup. If the worker retries a
failed activity, you'll pay credits twice. Workflow code that
generates content uses an `idempotency_key` table to dedupe at the
write side, not the agent-call side. Don't add free retries to AI
activities without the dedupe shield.

---

## Testing + promote gates

**3-layer pyramid (in order of fastness, run by `make ci`):**

1. **`make lint`** — `ruff format --check`, `ruff check`, `go vet`,
   `gofmt -l`. <2s, no infra.
2. **`make test`** — `pytest -x` against in-process FastAPI with
   mocked temporal/claude. ~10s. The 396-test characterization +
   route + service + repo + entity suite. **Catches**: shape
   regressions, IDOR, contract drift between route + service. Does
   NOT catch: anything requiring a real worker, real claude, real
   browser JS execution.
3. **`make e2e`** — `pytest tests/e2e/` against deployed staging.
   Two flavors live side-by-side under `tests/e2e/`:
   - **httpx tests** (test_smoke.py, test_ai_flows.py): full HTTP
     round-trip — create-deck → study a card → grade → drive a
     transform/plan to terminal. **Catches**: route-template-
     temporal-claude integration, IDOR, redirect shape, htmx-trigger
     leaks (server-side polling lifecycle).
   - **browser tests** (test_browser_smoke.py, marked `slow` +
     `browser`): drive Chromium via Playwright at iPhone-15-Pro
     viewport. **Catches**: inline `<script type="module">` parse +
     execute (the importmap-ordering bug class), htmx polling
     actually firing client-side, in-place fragment swaps (no
     navigation on accept/reject), `HX-Redirect` followed by the
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

- ANTHROPIC_API_KEY support. We use the Claude subscription path on
  purpose — the SDK adapter authenticates via
  `CLAUDE_CODE_OAUTH_TOKEN` (output of `claude setup-token`) only,
  which draws from the user's Max-plan credit pool.
- Per-token usage tracking. We had a `agent_usage` table briefly,
  but Anthropic meters per-account, not per-token, so the rollup
  modeled the wrong thing — dropped. Now we just handle
  `AgentBudgetExhausted` gracefully (429 + UI message) and wait for
  Anthropic to expose a real per-token usage API.
- A separate AI sidecar. The SDK migration pulled the agent
  in-process; the old `worker-go/cmd/agent-server/` binary +
  `Dockerfile.agent` are gone.
- Cloud SaaS / multi-tenant auth. prep is single-tenant
  per-deployment.
- Mobile native apps. PWA covers it.

---

## Versioning

Semver via git tags. Pre-1.0 we're permissive about minor-vs-patch
boundaries. Tag from `main`, then use `make promote v=<tag>` (local
tailnet /prep) or `make promote-vps v=<tag>` (prepcards.app) to build
+ deploy at the tag.

Current version visible via `git describe --tags`.
