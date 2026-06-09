# Architecture

A guided tour of how prep is put together. Aimed at someone who's
already seen the [README](../README.md), got it running locally,
and now wants to know *why* the code is shaped the way it is.

If you're an AI agent picking up work on the codebase, you probably
want [`CLAUDE.md`](../CLAUDE.md) instead — it's terser and skewed
toward "what's true right now."

---

## Top-level shape

```
              browser (PWA)
                   │
                   ▼
   ┌──────────────────────────────────────────────┐
   │              prep container                  │
   │  ┌────────────────────────────────────────┐  │
   │  │  goreman supervises 3:                 │  │
   │  │   temporal devserver (gRPC :7233)      │  │
   │  │   uvicorn / FastAPI (HTTP :8082)       │  │
   │  │   Go worker (Temporal task queue)      │  │
   │  └────────────────────────────────────────┘  │
   │                       │                      │
   │                       │ POST /api/agent/run  │
   │                       │ (X-Internal-Token)   │
   │                       ▼                      │
   │  ┌────────────────────────────────────────┐  │
   │  │  prep.agent.sdk_adapter (in-process):  │  │
   │  │   claude-agent-sdk → anthropic         │  │
   │  │   auth: CLAUDE_CODE_OAUTH_TOKEN +      │  │
   │  │         per-user BYOK rows (encrypted) │  │
   │  └────────────────────────────────────────┘  │
   └──────────────────────────────────────────────┘
```

**Single container, one compose project (`docker-compose.yml`).**
prep owns the HTTP surface, the SQLite DB, durable workflow state,
AND the in-process AI adapter. Bind the host port (default 8082)
and you have the app.

The Go worker invokes AI work by POSTing `/api/agent/run` against
its own host (`http://localhost:8082`), gated by an internal-token
header. That route resolves the per-user adapter via the selector
(BYOK → deploy-wide subscription → Noop) and runs one
`claude-agent-sdk` query, returning `{stdout}` — the same wire
shape the retired sidecar used, so the worker code didn't change.

The retired sidecar (`worker-go/cmd/agent-server/`) and its
`prep-agent-data` volume were removed during the SDK migration.

The Temporal devserver runs *inside* the prep container (not a
separate service). The Go worker connects to it on `127.0.0.1:7233`.
This keeps the deploy a single docker-compose file with no external
dependencies, at the cost of co-locating the durable-execution
runtime with the app.

---

## Python package layout (DDD)

The python source is organized by **bounded context**, not by
technical layer. Each context owns its own entities, repository,
service, and HTTP routes:

```
prep/
├── app.py                FastAPI() bootstrap; mounts the per-context routers
├── domain/               PURE — no I/O, no DB, no FastAPI imports
│   ├── srs.py            SRS state machine (FSRS-6) + Verdict + schedule_review
│   └── grading.py        deterministic mcq/multi/idk grader
├── infrastructure/
│   └── db.py             sqlite connection factory + cursor() + init() + now()
├── decks/                bounded context: deck + question lifecycle
│   ├── entities.py       Deck, Question, DeckCard, NewQuestion, QuestionType
│   ├── repo.py           DeckRepo, QuestionRepo
│   ├── service.py        use cases (CRUD + temporal-orchestrated plan/transform)
│   └── routes.py         /decks/*, /deck/*, /question/*, /transform/*, /plan/*
├── study/                bounded context: study sessions + reviews + grading
│   ├── entities.py       StudySession, RecentSession, Review, CardState
│   ├── repo.py           SessionRepo, ReviewRepo
│   ├── service.py        start_session, submit_sync_answer, advance, abandon, …
│   └── routes.py         /study/*, /session/*, /grading/*
├── notify/               bounded context: web push + scheduler
│   ├── entities.py       NotificationPrefs, PushSubscription
│   ├── repo.py           NotifyPrefsRepo, PushSubsRepo
│   └── routes.py         /notify/*
├── agent/                bounded context: AI integration
│   ├── port.py           AgentPort Protocol + AgentResult + exception types
│   ├── sdk_adapter.py    in-process claude-agent-sdk adapter (the prod impl)
│   ├── anthropic_api.py  per-user BYOK adapter — direct Anthropic Messages API
│   ├── openai_api.py     per-user BYOK adapter — OpenAI Chat Completions API
│   ├── openrouter.py     per-user BYOK adapter — OpenRouter (multi-vendor)
│   ├── selector.py       per-user agent_for_user() → BYOK > subscription > Noop
│   ├── status.py         probe + structured status() + cached is_available
│   └── routes.py         /settings/agent + /settings/agent/byok/* + /api/agent/run
├── auth/                 bounded context: identity + per-user prefs
│   ├── identity.py       Tailscale header parsing → current_user dependency
│   ├── repo.py           UserRepo
│   └── routes.py         /settings/editor
├── web/                  cross-cutting HTTP layer
│   ├── templates.py      Jinja2Templates instance + context_processors
│   ├── responses.py      redirect() helper (root_path-aware)
│   ├── errors.py         friendly error pages + json-aware exception handlers
│   ├── pwa.py            /manifest.json + /sw.js
│   └── index.py          GET / (cross-cuts decks + study)
└── dev/
    └── preview.py        /dev/preview/* template-fixture routes
                          (only registered when PREP_DEV=1)
```

### Layering rules

These are the boundary invariants worth preserving as the codebase
grows. A few minutes thinking about which side of the line a new
module belongs on saves a lot of refactoring later.

1. **`prep/domain/` imports nothing else from the project.** Pure
   stdlib + pydantic. If a domain function needs to know the time,
   take it as a parameter — don't reach for `datetime.now()`.
2. **Bounded-context modules cross only via entities or service
   surface.** `prep.study.routes` imports `DeckRepo` to look up a
   deck name, but it never imports `prep.decks.repo._row_to_question`.
3. **Routes call services (or repos for trivial reads).** Routes
   don't call `temporal_client` or `prep.infrastructure.db` directly.
   The exception is bare reads like "find the deck name for this id"
   where wrapping in a service function is just ceremony.
4. **Repos return entities, not raw dicts.** Decoding from sqlite
   rows happens inside the repo via `_row_to_X` helpers; templates
   and HTTP responses see entity-shape data.
5. **`temporal_client.py` and `chat_handoff.py` live at `prep/`
   root** because they're shared adapters used across multiple
   contexts, not owned by any one. If a third use shows up that
   fits into a context cleanly, they can move.

### Test pyramid

Per bounded context. Run with `make test`:

```
tests/
├── conftest.py           fixtures: tmp-path sqlite, env vars, TestClient,
│                         initialized_db (fresh schema + upserted user)
├── test_smoke.py         5 characterization tests pinning the v0.13.x behavior
├── domain/               pure unit tests (no I/O)
│   ├── test_srs.py       FSRS schedule_review + ladder→FSRS seed migration
│   └── test_grading.py   mcq/multi/idk paths
├── decks/                full pyramid: entities → repo → service → routes
│   ├── test_entities.py  pydantic validation
│   ├── test_repo.py      integration against real (in-mem) sqlite
│   ├── test_service.py   sync use cases via real sqlite + async via FakeTemporalClient
│   └── test_routes.py    HTTP via FastAPI TestClient
└── study/                same shape
```

A FakeTemporalClient class in `tests/decks/test_service.py` records
every call, so the workflow-orchestration paths get tested without
spinning up a real Temporal server. Synchronous repo operations test
against a real `:memory:` sqlite file scoped to the test (faster +
more accurate than mocking).

The pre-commit hook runs `pytest -x --tb=short -q` on commits
touching python — local test failures block the commit before they
land in the working tree.

---

## How an AI workflow flows

Take the plan-first deck-generation flow as a concrete example.
This is the most complex path the app takes; everything else is a
simpler subset.

**1. User clicks "Plan & generate" on `/decks/new`:**

```
POST /decks/new (action=plan, name="go-channels", context_prompt="...")
  └── prep.decks.routes.deck_new_create
        └── service.create_deck(repo, ...)            → INSERT INTO decks
              repo.create → prep.db.create_deck
        └── service.start_plan_generation(client, ...)
              client.start_plan_generate              → Temporal client.start_workflow
                                                         (workflow type "PlanGenerate")
              returns workflow_id ("plan-go-channels-<rand>")
        └── responses.redirect → /plan/<wid>
```

**2. The Go worker picks up the PlanGenerate workflow:**

```
worker-go/workflows/plan.go::PlanGenerateWorkflow
  ├── activities.PlanCards               (single LLM call: "give me a deck outline")
  │     └── client.Run(prompt) → POST agent:9999/run → claude -p ...
  ├── workflow.SetQueryHandler("getPlanProgress") — exposes progress to /plan/<wid>/status
  ├── workflow.GetSignalChannel("feedback")     ┐
  ├── workflow.GetSignalChannel("accept")       ├── poll for one of these
  └── workflow.GetSignalChannel("reject")       ┘
        ↓ on accept signal
        ├── for each PlanItem: ExecuteActivity(GenerateCardFromBrief) (parallel)
        ├── for each result:   ExecuteActivity(InsertCard) (idempotent via questions_idempotency)
        └── workflow returns
```

**3. Browser polls `/plan/<wid>/status`:**

```
GET /plan/<wid>/status
  └── prep.decks.routes.plan_status
        └── service.get_plan_progress(client, wid)
              client.get_plan_progress → Temporal QueryHandle.query("getPlanProgress")
        └── JSONResponse({...})
```

**4. User clicks "accept":**

```
POST /plan/<wid>/accept
  └── prep.decks.routes.plan_accept
        └── service.accept_plan(client, wid)
              client.signal_plan_accept → Temporal SignalHandle.signal("accept")
        └── responses.redirect → /plan/<wid>
```

The same shape applies to:
- `/question/<qid>/improve` (card-scope transform — auto-applies, no signal loop)
- `/deck/<name>/transform` (deck-scope transform — same apply/reject signals)
- `/session/<sid>/submit` for code/short answers (GradeAnswer workflow)

The seam is `worker-go/agent/agent.go` — a `Client` interface with
an `HTTPAgent` implementation that POSTs `http://localhost:8082/api/agent/run`
(prep's own host) with an `X-Internal-Token` header. The route then
hands the request off to the in-process `claude-agent-sdk` adapter,
so there's no second container in the loop.

---

## SRS state machine

`prep/domain/srs.py` owns the rules — pure functions over the FSRS-6
state model (stability, difficulty, phase). No I/O.

State per card:
- **stability (float, days)**: how long until recall probability falls
  to ~90%. Grows with successful reviews, shrinks on lapses.
- **difficulty (float, 1–10)**: how hard the card is, learned over
  time from your verdict history.
- **phase**: Learning (new / recently lapsed) vs Review (graduated).

```
schedule_review(card_state, verdict, now) →
    CardState(stability', difficulty', phase', next_due)
```

A historical ladder-based migration shim survives in
`seed_state_from_ladder_step()` for pre-FSRS cards in old decks —
maps step→stability so they don't all become "new" on upgrade.

Insert path (`prep.db.add_question`): create the question row + a
matching `cards` row in the Learning phase so the card lands due
immediately.

Review path (`prep.db.record_review`, used by both the sync grade
in `prep.study.service.submit_sync_answer` and the Go worker's
GradeAnswer activity):
1. Verify the question belongs to the user (defense in depth — the
   route should already have checked).
2. Read the current FSRS state.
3. Call `schedule_review(state, verdict, now)`.
4. `INSERT INTO reviews` (audit log) + `UPDATE cards SET stability,
   difficulty, phase, next_due, last_review`.
5. Return `CardState(...)` so the route can render "next review in
   N days" without a re-query.

**Caveat: the Go worker still uses the legacy ladder.**
`worker-go/activities/grading.go` writes `cards.step` + `next_due`
from a hardcoded `srsLadderMinutes` array. The Python path migrated
to FSRS; the Go path didn't. Hybrid behavior in practice: sync
self-grade uses FSRS; AI-graded free-text answers use the ladder.
Migrating the Go path is tracked separately.

The audit-log split between `reviews` (immutable history) and `cards`
(mutable current state) means we can compute "rights / attempts"
from `reviews` aggregations — those columns on `DeckCard` come from
`SELECT COUNT(*)` subqueries in `db.list_questions`.

---

## Schema overview

Five user-owned tables, all keyed by `user_id` (= Tailscale login):

```
users                 1 row per Tailscale identity (login email is PK)
  ├── tailscale_login (PK)
  ├── display_name, profile_pic_url, created_at, last_seen_at
  ├── notification_prefs    (JSON blob — mode/digest_hour/threshold/quiet hours/...)
  └── editor_input_mode     (vanilla|vim|emacs)

decks
  ├── id (PK), user_id (FK), name, created_at
  ├── context_prompt        (free-form description; what the AI sees on transforms)
  └── UNIQUE (user_id, name)

questions
  ├── id (PK), user_id (FK), deck_id (FK)
  ├── type (mcq|multi|code|short), topic, prompt, choices (JSON), answer
  ├── rubric, skeleton, language, suspended
  └── created_at

cards                  1-to-1 with questions; mutable SRS state
  ├── question_id (PK + FK)
  ├── step (0..5)
  ├── next_due, last_review

reviews                append-only audit log of every grade
  ├── id (PK), question_id (FK)
  ├── ts, result (right|wrong), user_answer, grader_notes

study_sessions         cross-device study attempts
  ├── id (PK, hex string), user_id (FK), deck_id (FK)
  ├── status (active|completed|abandoned)
  ├── state (awaiting-answer|grading|showing-result)
  ├── current_question_id, current_draft, current_grading_workflow_id
  ├── last_answered_qid, last_answered_verdict (JSON), last_answered_state (JSON)
  ├── version            (optimistic-concurrency guard; client must echo back)
  └── device_label

study_session_answers  (session_id, question_id) join — which cards has this
                       session already seen, so the next-card picker doesn't
                       double-serve them.

push_subscriptions     1 row per browser/device per user
  ├── endpoint (PK), user_id (FK)
  └── p256dh, auth, created_at, last_seen_at
```

`db.init()` is idempotent — runs on every app boot, creates tables
that don't exist, and walks a series of guarded `ALTER TABLE` /
table-rebuild blocks for each historical migration step. Adding a
new column? Append another `PRAGMA table_info` check + `ALTER` to
`init()`.

**FK CASCADE gotcha (fixed in v0.4.1):** if you ever rebuild a
table that's referenced by an FK, follow the SQLite-recommended
pattern. `PRAGMA foreign_keys = OFF` *outside* any transaction, then
`BEGIN; …rebuild…; PRAGMA foreign_key_check; COMMIT;`. A naive
`DROP TABLE decks` cascades through questions/cards/reviews and wipes
user data. The `decks` rebuild block in `prep/infrastructure/db.py`
shows the correct pattern.

---

## Auth model

prep ships with exactly one auth path: **Tailscale Serve identity
headers**. There is no password / OAuth / magic-link flow. This is
deliberate — prep is built for personal-tailnet hosting, where
"who's allowed in" is already answered by your tailnet membership.

`prep.auth.identity.current_user` is the dependency every protected
route uses:

```python
def _resolve_login(request: Request) -> str | None:
    hdr = request.headers.get("tailscale-user-login")
    if hdr:
        return hdr.strip()                    # tailscale headers always win
    fallback = os.environ.get("PREP_DEFAULT_USER")
    return fallback or None                   # empty/unset → 401

def current_user(request: Request) -> dict:
    login = _resolve_login(request)
    if not login:
        raise HTTPException(401, ...)
    user = db.upsert_user(login, ...)         # idempotent; refreshes last_seen_at
    request.state.user = user                 # surfaces to context_processor
    return user
```

`PREP_DEFAULT_USER` is the development-time bypass. The Makefile's
`make dev` target sets it to `dev@example.com` so contributors can
study without a tailnet. Deploy targets explicitly clear it (`make
deploy-stag` and `make deploy-prod` prefix the docker compose
invocation with `PREP_DEFAULT_USER=`) so prod stacks always require
real Tailscale identity.

Defense in depth: every user-owned table has a `user_id` column,
every accessor takes `user_id` first, and every WHERE clause filters
on it. Even if a route forgets the ownership check, an IDOR via
guessed IDs still returns "not found" because the cross-user query
filters them out.

---

## Deploy shape

Two compose stacks, one checkout, idempotent promote:

```
prep-app-staging/
├── main branch                          ← source of truth
├── make deploy-stag                     ← builds current main as
│                                          prep:staging on :8082
│                                          (ENV_NAME=prep, ROOT_PATH=/prep-staging)
├── git tag -a v0.X.Y && git push --tags ← cut a release
└── make promote v=v0.X.Y                ← writes .prod-version,
                                            commits, pushes,
                                            git worktree at v0.X.Y,
                                            builds prep:v0.X.Y on :8081
                                            (ENV_NAME=prod, ROOT_PATH=/prep)
```

Both stacks run on the same docker daemon. The `git worktree add
--detach <tag>` for prod is the trick that lets one checkout build
two different commits without disturbing the working tree.

`.prod-version` is the source of truth for "what's running in
prod" — `git log .prod-version` is the prod-deploy history.

`deploy/staging.env` and `deploy/prod.env` ship in the repo as
**defaults for the author's two-stack tailscale setup** (ports
8081/8082, paths /prep + /prep-staging). They're not secrets and
not personal — anyone using a similar shape gets a working starting
point. Different setup? Edit them, or layer overrides via a local
`.env` (which is gitignored).

`PREP_DEV=1` (set by `make dev`, deliberately cleared by the deploy
targets) gates the `/dev/preview/*` template-fixture routes so they
never ship in prod images.

---

## Where to start when…

- **Adding a new route**: pick the bounded context it belongs to,
  add the handler in that context's `routes.py`. If it touches more
  than one context (deck list + recent sessions, say), it goes in
  `prep/web/`.
- **Adding a new entity field**: update the pydantic model in
  `entities.py`, the `_row_to_X` helper in `repo.py`, the schema
  migration in `prep/infrastructure/db.py`, and the relevant
  template if it should render. Run `make test` — the entity tests
  catch the round-trip break.
- **Adding a new workflow**: define the workflow + activities in
  `worker-go/workflows/`, the type schemas in
  `worker-go/shared/types.go`, the python-side client wrapper in
  `prep/temporal_client.py`, the use case in the relevant context's
  `service.py`, and the route in `routes.py`. Tests with
  `FakeTemporalClient` verify the orchestration without booting
  Temporal.
- **Changing the SRS rules**: edit `prep/domain/srs.py`. The 24
  domain tests under `tests/domain/test_srs.py` will tell you fast
  if you broke the ladder.
