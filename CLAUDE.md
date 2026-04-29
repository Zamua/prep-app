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
                                                     agent container :9999
                                                          │
                                                     claude CLI inside
                                                     CLAUDE_CODE_OAUTH_TOKEN
                                                     → anthropic
```

Two containers, one compose project:

- **prep** — FastAPI (uvicorn) + Temporal devserver + Go Temporal
  worker, all in one container, supervised by goreman. The
  user-facing surface.
- **agent** — Node + Claude Code CLI + a tiny Go HTTP wrapper. The
  worker calls `POST /run` here for every claude invocation. Token
  is stored in a persistent volume so it survives restarts.

Both volumes are env-named (`${ENV_NAME}-data`, `${ENV_NAME}-agent-data`)
so staging and prod can run side-by-side on one host without colliding.

---

## Layout

```
app.py                FastAPI routes + startup probe + jinja context_processors
db.py                 sqlite schema (idempotent migrations in init()), accessors
agent.py              status() probe — reports HTTP agent /healthz or shell-bin existence
grader.py             deterministic grade() for mcq/multi/idk only (fast sync path)
notify.py             VAPID web push + asyncio-scheduled per-user digest/when-ready
chat_handoff.py       builds prefilled URLs for the "Discuss this card" popover
icons.py              Jinja `icon('name')` global → inlined Phosphor Light SVG
dev_preview.py        /dev/preview/<template>/<fixture> routes for UI sweeps
temporal_client.py    Python helpers: start_grading, start_transform, start_plan_generate, signals/queries
templates/            Jinja2. base.html sets data-editor-mode + masthead chip via context_processor
static/               style.css (single file, CSS vars, light/dark), icons/, pwa/, cm-bundle.js
worker-go/
  agent/agent.go      Client interface + ShellAgent + HTTPAgent + FromEnv()
  cmd/agent-server/   The agent container's binary: /run + /healthz + /connect + /disconnect
  workflows/          GradeAnswer, Transform, PlanGenerate
  activities/         Side effects: GradeFreeText, ComputeTransform, ApplyTransform,
                      PlanCards, GenerateCardFromBrief, InsertCard
  shared/types.go     Workflow input/output schemas
docker/
  Dockerfile.prep     multi-stage: golang:1.26 (worker+goreman), oven/bun:1.1.0 (cm-bundle),
                      python:3.11-slim runtime with uv-installed venv + temporal CLI baked
  Dockerfile.agent    node:22-slim + npm-installed claude-code + go-built agent-server
  Procfile.docker     temporal | uvicorn | worker, all under goreman in the prep container
docker-compose.yml    prep + agent services, env-driven volume names
.env.example          template for per-deploy config (PORT, ROOT_PATH, ENV_NAME, etc.)
ui-tools/capture.py   playwright screenshot harness — drives Chromium at iPhone-15-Pro-Max
                      viewport over /dev/preview/* + live pages, dumps PNGs to ui-screenshots/.
                      Run before/after a UI change to eyeball regressions:
                      `cd ui-tools && uv run capture.py --tag before-foo` (then `--tag after-foo`).
.dockerignore         keeps build context lean (.venv, .git, build outputs, secrets out).
```

---

## How AI work flows

Generation example (the plan-first flow at `/decks/new` action=plan):

1. FastAPI `/decks/new` POST creates deck row, then
   `temporal_client.start_plan_generate` kicks off `PlanGenerateWorkflow`
   on the worker.
2. Workflow calls activity `PlanCards` → worker's `Cfg.Agent.Run(prompt)`
   → over HTTP to the agent container → spawns `claude -p <prompt>` with
   `CLAUDE_CODE_OAUTH_TOKEN` in env → returns stdout.
3. Workflow stores plan, query handler exposes it. UI polls
   `/plan/<wid>/status` and renders the brief outline.
4. User signals `feedback` (replan), `accept` (expand), or `reject`.
5. On accept: workflow `ExecuteActivity` for each `PlanItem` in
   parallel — N concurrent `claude -p` calls — gathers results,
   writes via `InsertCard` activity (idempotency via
   `questions_idempotency` table).

The agent boundary (`worker-go/agent/agent.go`) is the seam. Two
implementations:
- **ShellAgent** — `exec.Command(claude, args, prompt)`, used when
  `PREP_AGENT_BIN` is set. Native (no docker) dev or legacy.
- **HTTPAgent** — `POST <url>/run`, used when `PREP_AGENT_URL` is
  set. The container path. `FromEnv()` picks based on env.

---

## Schema migrations

`db.init()` runs on every app boot and is idempotent. Add a column?
Check `PRAGMA table_info(<table>)` first, then ALTER. Existing
examples: `editor_input_mode`, `notification_prefs`, `context_prompt`.

**FK CASCADE gotcha (the v0.4.1 incident).** If you ever rebuild a
table that's referenced by an FK, follow the SQLite-recommended
pattern: `PRAGMA foreign_keys=OFF` OUTSIDE any transaction, then
`BEGIN; ...rebuild...; PRAGMA foreign_key_check; COMMIT;`. v0.3.0
shipped a naive `DROP TABLE decks` and cascaded through
questions/cards/reviews — wiped a real prod DB. v0.4.1 fixed it.
Don't regress.

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

## Gotchas worth knowing

**`tailscale serve --set-path=/prep` strips the prefix when
forwarding.** uvicorn must be launched with `--root-path $ROOT_PATH`
or static assets 404. Procfile.docker handles this; if you change
the run command, keep the flag.

**uvicorn `--no-access-log` is on in prod**, so per-request
diagnostics need `docker compose logs -f` for app errors only. Add
`--log-level debug` to Procfile.docker for verbose tracing.

**`claude setup-token` output prefix**: `sk-ant-oat01-…`. The
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
apps** (Feb 2026 policy). prep uses `claude setup-token` (officially
blessed) instead — user generates the token on a machine they
control, pastes into the UI. Don't try to wrap `claude auth login`.

---

## What's intentionally NOT here

- ANTHROPIC_API_KEY support. We use the Claude subscription path on
  purpose. The agent-server inherits whatever auth `claude setup-token`
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
