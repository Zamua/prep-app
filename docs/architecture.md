# Architecture

A guided tour of how prep is put together. Aimed at someone who has
already seen the [README](../README.md), got the worker running
locally, and now wants to know *why* the code is shaped the way it is.

If you are an AI agent picking up work on the codebase, read
[`CLAUDE.md`](../CLAUDE.md) instead. It is terser and skewed toward
"what is true right now".

---

## Top-level shape

prep is one TypeScript Worker running on **celld**, a Cloudflare
Workers runtime. There is no application server, no separate database
process, and no job queue. State lives in per-cell SQLite; durable work
runs on cell alarms.

```
                    browser (PWA)
                          │
                          ▼
   ┌─────────────────────────────────────────────────────┐
   │  entry worker (runtime/worker.ts)                   │
   │    static assets, /manifest.json, /sw.js            │
   │    landing, privacy, offline shell, error pages     │
   │    /api/instant/generate, /webhooks/clerk, /metrics  │
   │    identity: Clerk session, anon cookie, or PAT      │
   └───────────────┬─────────────────────────────────────┘
                   │ identity asserted in x-prep-* headers
                   ▼
   ┌───────────────────────┐        ┌──────────────────┐
   │ UserCell (per user)   │◀──────▶│ JobCell (per job)│
   │  SQLite: decks, cards │ status │  step ledger     │
   │  renders every signed │  write │  alarm loop      │
   │  in page              │        │  LLM + write     │
   │  per-user alarm       │        │  steps           │
   └──────┬────────────────┘        └──────────────────┘
          │
          ├──▶ DirectoryCell ("global"): enumeration, merges, reaper
          └──▶ InstantLimiterCell ("global"): instant-generation windows
```

The entry worker is a translation layer. It verifies who the request
is, strips any inbound copy of the identity headers, sets its own, and
forwards to the cell that identity names. A request can only reach the
cell its verified identity names, so per-user isolation is structural
rather than a `WHERE user_id = ?` the code has to remember.

---

## The four cell classes

Class names sit in the storage key path, so the taxonomy is a one-way
door. There are four, and adding a fifth is a decision, not a
refactor.

| class | keyed by | holds |
| --- | --- | --- |
| `UserCell` | user id (Clerk `sub`, or `anon:<hex>`) | one SQLite per user: decks, questions, cards (FSRS state), reviews, study sessions and answers, trivia sessions and queue, the notifications log, push subscriptions, BYOK credentials, API token hashes, the four idempotency ledgers, prefs, and this user's job status rows |
| `DirectoryCell` | `"global"` | enumeration data only, written at create / merge / delete: user id, `is_anonymous`, `created_at`, the `account_merges` audit, merge markers and tombstones. It also owns the anonymous-retention sweep |
| `InstantLimiterCell` | `"global"` | the instant-generation ledger and both breakers (per-IP buckets, global minute and day windows) |
| `JobCell` | job id | one durable job: its step ledger and its human gate |

**There is no scheduler cell and no per-request write to any global
cell.** `last_seen_at` is bumped on every identified request, so it
lives in the `UserCell`; in a global cell it would be a single-writer
hot spot on the whole request path.

The 18-table shared schema the app used to carry collapses into one
SQLite per user with no `user_id` columns at all. `DirectoryCell` keeps
the small amount of cross-user data that genuinely has to be
enumerable.

---

## Layers, enforced

```
worker/
  domain/     pure: FSRS, grading, markdown, trivia state, merge policy,
              instant card hygiene, limiter arithmetic, row caps, job
              graph + ledger + schedule algebra, the anon cookie format
  app/        use cases and PORTS: DeckRepo, SessionRepo, JobStatusRepo,
              AgentPort, WebPush, Clock, WorkflowRunner, Renderer,
              IdentityProvider; view-model DTOs (what a page needs,
              computed here, never in a template)
  runtime/    the entry worker, the four cells, and ADAPTERS: SqlStorage
              repositories, the Clerk verifier, the anon cookie signer,
              the agents, web push over WebCrypto, the nunjucks renderer,
              the alarm-ledger runner; and compose.ts, the composition
              root
  templates/  the nunjucks templates
  tests/      vitest, mirroring the three layers
```

The dependency rule is **runtime -> app -> domain, and nothing imports
upward**. It is enforced by `worker/tests/layering.test.ts`, not by
convention, because a rule is only worth having if breaking it is
noisy. That test fails when:

1. `domain/` imports anything from `app/`, `runtime/`, `cloudflare:` or
   `node:`. Pure functions and value objects only. A domain function
   that needs the time takes it as a parameter.
2. `app/` imports anything outside `domain/` and `app/`, or contains
   `fetch(`, `new Response`, `.sql.exec`, `DurableObject` or
   `nunjucks`. Use cases call ports; adapters own the I/O.
3. Anything under `runtime/` other than `compose.ts` imports from
   `runtime/adapters/`. Cells and the router receive adapters through
   ports from the composition root. Naming an adapter anywhere else is
   the drift the ports exist to prevent.
4. Anything other than the nunjucks adapter imports nunjucks or the
   compiled templates.

`runtime/compose.ts` is the composition root and the only place
adapters meet ports. Cross-cutting concerns are wrappers applied there
and by the router, never inside a handler: the anonymous-cookie
refresh and clear on the response path, `no-cache` on HTML, request
timing.

---

## Rendering

The UI is server-rendered HTML plus progressive-enhancement JS. The
renderer is **nunjucks**, precompiled at build time by
`worker/scripts/build.mjs` into `build/templates.js`, so no template
source is parsed at request time.

**Where rendering runs matters.** Several page contexts read the
database per render, so every signed-in page renders **inside the
`UserCell`**: one activation per page, synchronous SQLite reads, one
`pageContext` built by the use case. The entry worker only verifies
identity and forwards. Unauthenticated pages (landing, privacy, the
offline shell, the reauth shell, error pages) render in the entry
worker, which has no database to read.

View models are computed in `app/viewmodels/`. A template never does a
lookup, a group-by, or arithmetic that a use case could have done.

The client JS under `static/js/` is a build input, not a served source
tree that happens to also be shipped: `build.mjs` copies it into
`dist/assets/static/` and bakes the precache manifest the service
worker enumerates.

---

## Durable work: jobs on alarms

There are four job kinds, defined as data in `app/jobs/graph.ts`:

| kind | steps |
| --- | --- |
| `PlanGenerate` | `plan` (LLM) -> `gate` (human) -> `expand` (LLM, batched) -> `insert` (write, per item) |
| `Transform` | `compute` (LLM) -> `gate` (human) -> `apply` (write) |
| `TriviaGenerate` | `generate` (LLM) -> `insert` (write) |
| `GradeAnswer` | `grade` (LLM) -> `record` (write) |

A graph is a table, so a workflow's shape is reviewable without reading
control flow. Each node names its kind, its retry policy, its fanout
mode, its status string, and what happens on error.

**One `JobCell` per job, driven by its own alarm.** Every decision is
taken from the ledger rows, never from in-memory state, so an eviction,
a node restart and a duplicate alarm all reach the same one. Two rules
the shape rests on:

- A caller-originated RPC (`start`, `signal`, `terminate`) never calls
  back into the owner's cell. The owner is mid-request when it calls,
  and a cell serves one request at a time. Everything that touches the
  owner happens on the alarm instead.
- The alarm is derived from the rows at the end of every RPC and in the
  constructor, never held, so a rolled-back RPC still converges.

**The status direction pays for the poll.** A `JobCell` writes into its
owner's `UserCell` and never the reverse. The progress fragment polls
and the badge poll read only `UserCell` rows, so a 300s LLM step blocks
its own `JobCell` and nothing else. The one `UserCell -> JobCell` hop is
the gate signal (accept / reject / feedback / apply), which happens on
a click.

Progress travels with the status write, rendered by the job's partial,
so a page never has to query a job to draw it.

`app/jobs/index.ts` is the only file that knows all four workflows
exist. The runner imports the registry, never a handler.

---

## Periodic work

Scheduled work is **per-user alarms**, not a fan-out over users.

Each `UserCell` computes its own next wake from its own state (digest
hour in its timezone, the when-ready debounce against its next due
card, each trivia deck's backed-off refill interval, quiet hours) and
arms `storage.setAlarm`. The wake is re-derived on every prefs or deck
write and on activation, from persisted state, so a duplicate fire is a
no-op.

`app/notify/wake.ts` separates the reading from the doing on purpose:
`nextWakeAt` re-derives the wake after any write, and `runWake` reads
the same rows through the same function, so what the alarm is armed for
and what it does when it fires cannot drift apart.

An alarm handler never calls the LLM. The trivia refill dispatches a
`TriviaGenerate` job per due deck and returns.

The one remaining walk is the anonymous-retention sweep. It lives in
`DirectoryCell` because there is one directory, and its alarm is
re-derived from the sweep's own row on every activation, so an eviction
resumes where it stopped.

---

## Identity

Precedence, stated once in `app/auth/resolve.ts` so no call site has to
remember it:

**signed-in > dormant session > anonymous cookie > visitor.**

- **Signed in.** A Clerk session cookie, verified against the JWKS with
  the authorized-party check. Deploy-side Clerk configuration is public
  and lives in the wrangler files; the secret key and webhook secret
  arrive as runtime vars.
- **Dormant session.** A returning user on a PWA cold launch has an
  expired session token and durable evidence of one. Falling through to
  an anonymous cookie left on that browser would serve them their old
  guest account and break every recovery path keyed on "no user", so a
  dormant session gets the reauth shell instead.
- **Anonymous cookie.** `prep_anon`, HMAC-SHA256 over an HKDF-derived
  key. An anonymous visitor who generates a deck becomes a real user
  row with a real `UserCell`. Anonymous accounts are anonymous, not
  ephemeral. See [ANONYMOUS-ACCOUNTS.md](ANONYMOUS-ACCOUNTS.md).
- **Visitor.** The landing page.
- **PAT.** A bearer token for `/api/v1/*` and `/mcp`, matched against a
  SHA-256 hash the owner's cell stores.

When a signed-in request arrives carrying an anonymous cookie, the
**merge saga** in `app/auth/mergeSaga.ts` folds the anonymous account
into the signed-in one. It is a saga because it spans three cells and
has to be resumable: markers in the `DirectoryCell`, rows copied
between two `UserCell`s, tombstone at the end.

---

## AI: BYOK, API keys only

Every AI call is a `fetch` from a cell. There is no SDK, no subprocess,
and no sidecar.

Two funding tiers, decided in `app/agent/funding.ts` (policy over rows,
so it is app-layer and names no adapter) and turned into an adapter by
`runtime/adapters/agents/select.ts`:

1. **BYOK**: the user's own API key for Anthropic, OpenAI, or
   OpenRouter, stored AES-256-GCM encrypted in their cell. Precedence
   when several are held and none is active: Anthropic, OpenRouter,
   OpenAI. OpenRouter additionally supports an OAuth PKCE sign-in that
   mints a key on the user's own account, so it is the one provider
   that needs no copy-paste.
2. **Shared free tier**: an OpenAI-compatible endpoint the deploy
   configures by env. Capped per generation. Optional; a deploy that
   configures nothing simply refuses AI calls with one reason.

**BYOK is API keys only.** There is no Claude-subscription provider and
no deploy-wide subscription token. A Claude Code OAuth credential is
rejected by the Messages API, and the one sanctioned path for it
bundles and spawns a large executable per call, which is not a harness
this app hosts.

If a user holds BYOK rows but none of them yields a usable key, the
call refuses rather than falling through to the shared tier: silently
spending a shared credential a user opted out of is worse than an
error. The key is decrypted in the isolate that will use it and never
held past the call, so revoking a credential stops the next step.

---

## Storage

One SQLite database per cell, through `SqlStorage`. Repositories are
adapters under `runtime/adapters/sql/` and return entities, not rows.

A `UserCell` holds:

```
profile                 the user: display name, email, prefs (retention,
                        editor mode, notification prefs, tz), is_anonymous,
                        last_seen_at, row caps
decks                   name, context_prompt, created_at
questions               type (mcq|multi|code|short), topic, prompt,
                        choices, answer, rubric, skeleton, language,
                        suspended
cards                   1-to-1 with questions; FSRS state (stability,
                        difficulty, phase, next_due, last_review)
reviews                 append-only audit log of every grade
study_sessions          status, state, current question, optimistic
study_session_answers   version guard, device label
trivia_sessions         the trivia surface's own session + queue
trivia_queue
notifications_log
push_subscriptions      one row per browser per user
byok_credentials        AES-256-GCM ciphertext
api_tokens              SHA-256 hashes
active_workflows        the badge read model
job_progress            what a JobCell writes back
*_idempotency           grading, questions, steps, offline sync
```

Migrations run in `runtime/adapters/sql/migrate.ts` on cell activation
under `blockConcurrencyWhile`, guarded and idempotent.

---

## SRS

`domain/fsrs/` owns the rules: pure functions over the FSRS-6 state
model (stability, difficulty, phase). No I/O.

- **stability** (days): how long until recall probability falls to
  ~90%. Grows on success, shrinks on lapse.
- **difficulty** (1 to 10): learned from the verdict history.
- **phase**: Learning (new or recently lapsed) vs Review (graduated).

Both grading paths schedule through the same function: the deterministic
grader in `domain/grading/` for mcq / multi / idk, and the
`GradeAnswer` job for code and short answers.

The split between `reviews` (immutable history) and `cards` (mutable
current state) is what makes "rights / attempts" a `COUNT(*)` over the
audit log rather than a counter that can drift.

---

## Build, run, test

```bash
cd worker
npm install
npm run build        # precompile templates, bake icons + service worker,
                     # bundle domain twins, lay out dist/assets
npm run typecheck    # tsc over the worker and its tests
npx vitest run       # the suite
scripts/run-node.sh  # build, deploy, and start one local celld node
```

`build/` and `dist/` are generated and never committed. `build.mjs`
exposes each step as a function so `tests/build.test.ts` can run one
against a scratch tree.

Deploy contracts are the three wrangler files
(`wrangler.dev.jsonc`, `wrangler.staging.jsonc`, `wrangler.prod.jsonc`).
They carry **public values only**: the durable-object bindings, the
asset directory, the Clerk publishable configuration, and the
timeout ceilings. Every secret arrives at runtime as a `CELLD_VAR_*`
and never enters a committed file.

---

## Where to start when...

- **Adding a route.** Decide first whether it needs a user. If it does,
  add it to `runtime/cells/routes/` and it renders in the `UserCell`.
  If it does not, it belongs in the entry worker under
  `runtime/routes/`.
- **Adding a domain rule.** It goes in `domain/`, with a test, and it
  takes the clock as a parameter. The layering test will tell you
  immediately if it reached for I/O.
- **Adding a workflow.** Add its graph to `app/jobs/graph.ts`, its step
  handlers under `app/jobs/`, and register them in `app/jobs/index.ts`.
  The runner needs no change: a graph is data.
- **Adding a stored field.** Update the entity, the repository's row
  mapping, and the schema migration. Repos return entities, so the
  round-trip test catches a half-done change.
- **Adding an adapter.** It goes under `runtime/adapters/` behind a port
  declared in `app/ports.ts`, and it is wired in `runtime/compose.ts`.
  If you find yourself importing it anywhere else, the layering test
  will say so.

---

## Feature specs

Longer design documents for the features whose behavior is not obvious
from the code:

- [ANONYMOUS-ACCOUNTS.md](ANONYMOUS-ACCOUNTS.md): server-side anonymous
  identity, the cookie lifecycle, and the merge.
- [OFFLINE.md](OFFLINE.md): the offline study surface and the sync
  contract.
- [INSTANT-START.md](INSTANT-START.md): the anonymous first-run flow and
  its rate limiter.
- [AI-PROVIDERS.md](AI-PROVIDERS.md): the provider-neutral agent layer,
  the shared tier, and BYOK.
