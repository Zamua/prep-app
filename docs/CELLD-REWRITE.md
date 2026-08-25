# prep on celld: the rewrite plan

Status: PLAN, nothing built. Revised once after an adversarial review
(41 findings, all folded in). Decided by the operator on 2026-08-25:

1. No Temporal. Durable work runs on celld Workflows.
2. No Python. The whole service is TypeScript, native on celld.
3. The acceptance bar is pixel parity: the rewrite renders the same
   pixels as the current app across the user-visible flows on staging,
   with a diff that rounds to zero.
4. The software principles hold: DDD, explicit layering, ports and
   adapters. Layering is enforced by a test, not by convention.

This document is the scope, the target architecture, the parity gate,
the phase plan, the spikes that gate the one-way doors, the risks, and
the decisions the operator owns. Section 10 is the inventory it rests
on, measured on 2026-08-25.

---

## 1. What is being rewritten

The current app: 24.7k lines of Python across 19 route modules (139
routes), 49 Jinja templates, 7.0k lines of first-party JS, 52 CSS files,
an 18-table SQLite schema, a 3.2k-line Go Temporal worker running 4
workflows, two asyncio loops carrying 4 jobs. 1,297 tests; 23 Playwright
e2e files pinning ~30 user-visible flows.

### 1.1 Carries over unchanged (the parity anchor)

- All CSS, all icons, all first-party JS under `static/js/` (study loop,
  dashboard, offline sync, instant start, the 14 modules), vendored
  htmx, the CodeMirror bundle, the service worker's behavior and its
  precache contract.
- Every JSON contract the JS depends on: `/api/study/*`,
  `/api/dashboard/*`, `/api/offline/{snapshot,sync}` (including
  `previous_ids`), `/api/instant/generate`, `/notify/*`,
  `/api/active-workflows-badge`, `/api/v1/decks*`, `/mcp`.
- Every URL including the legacy aliases (`/deck/{name}/edit-with-claude`
  redirect, `/decks/{name}/next`, `POST /cards`), every htmx fragment
  shape, every `HX-Redirect`, every redirect target, every `Set-Cookie`.
- The wire formats of stored secrets: the anonymous cookie (HMAC-SHA256
  over the same HKDF-derived key, same 180d/30d/60s lifecycle) and BYOK
  ciphertexts (AES-256-GCM, same master key), so cookies and credentials
  survive the cutover.
- The curated OpenAPI surface (`/openapi.json`, `/docs`, `/redoc`) for
  `/api/v1/*` and `/mcp`: hand-written in TS, checked as a contract
  golden.

### 1.2 Rewritten in TypeScript

Every route handler; every repository; the domain (FSRS, grading,
markdown, trivia state, merge policy, instant hygiene, limiter
arithmetic, anonymous row caps); auth (Clerk sessions with the
`__session` cookie and `azp`, the `__client_uat` dormant-session read
and the reauth shell with its fallback cookie, the anonymous cookie's
mint / refresh / clear middleware, forget-device, PAT, webhooks, the
merge saga); the AI adapters; push; the 4 workflows; the 4 jobs; the MCP
server (17 tools); CSV, `.prepdeck`, Anki import/export; the PWA routes;
the debug endpoints (`/_debug/auth`, `/debug/session`), kept because
they are the documented Clerk diagnostics.

### 1.3 Dropped, and one decision the operator owns

Dropped: the Tailscale identity provider and `PREP_DEFAULT_USER` as a
production path (a fake identity provider stays, for local e2e; see
5.1), the deploy-wide `claude-agent-sdk` subscription token (gated off
on every Clerk deploy already), the Prometheus threadpool gauges (no
threadpool exists), the Go worker + Temporal devserver + goreman + the
container.

**Decision 7.4: the per-user `claude_subscription` BYOK provider.** It
is live in Clerk mode today, listed first in the settings UI, and it
runs the Python `claude-agent-sdk`, which spawns a Claude Code
subprocess. A V8 cell cannot do that. Options: drop it and notify the
users holding such a row (their ciphertext survives, useless), or keep
it behind a sidecar, which is not "full native celld". The plan assumes
drop-and-notify until the operator says otherwise.

---

## 2. Target architecture

### 2.1 One Worker, four permanent cell classes

celld rejects `renamed_classes` and class names sit in the storage key
path: the taxonomy is a one-way door and is committed only after the
phase 0b spikes (5.1) pass.

| class | keyed by | holds |
| --- | --- | --- |
| `UserCell` | the user id (Clerk `sub`, or `anon:<hex>`) | everything the schema scopes by user today: decks, questions, cards (FSRS state), reviews, study sessions and answers, trivia sessions and queue, notifications log, push subscriptions, BYOK credentials, API token hashes, the three idempotency ledgers (offline sync, grading, questions), prefs (retention, editor mode, notification prefs, tz), **`last_seen_at`**, the anonymous row caps, and this user's job status rows (the badge read model) |
| `DirectoryCell` | `"global"` | enumeration data only, written at create / merge / delete: user id, `is_anonymous`, `created_at`; the `account_merges` audit (the source of `previous_ids`); merge markers and tombstones (2.6) |
| `InstantLimiterCell` | `"global"` | the instant-generation ledger and both breakers (per-IP buckets, global minute and day windows). One writer is fine at this volume; sharding by IP bucket is a later split that needs no rename |
| `JobCell` | job id | one durable job: the step ledger and the human gate (2.4) |

There is no scheduler cell and no per-request write to any global cell:

- `last_seen_at` is bumped on every identified request today, which in a
  global cell would be a single-writer hot spot on the entire request
  path. It lives in the `UserCell`.
- Scheduled work is **per-user alarms**, not a fan-out. Each `UserCell`
  computes its own next wake from its own state (digest hour in its tz,
  the when-ready debounce against its next due card, each trivia deck's
  backed-off refill interval, quiet hours) and arms `storage.setAlarm`;
  re-armed on every prefs or deck write and on activation, derived from
  persisted state so a duplicate fire is a no-op. A fleet fan-out on a
  300s period would re-activate every user cell on the same period the
  fleet evicts them (`CELLD_IDLE_EVICT_S=300`) and defeat the economics
  the port rests on.
- The trivia refill never calls the LLM from an alarm handler; it
  dispatches a `JobCell` (TriviaGenerate) per due deck.
- The anonymous reaper is the one walk that remains: daily, over
  anonymous ids in the `DirectoryCell`, batch of 50, each candidate asked
  for its own `last_seen_at`.

Per-user isolation is structural: a request can only reach the cell its
verified identity names. The 18-table schema collapses into one SQLite
per user with no `user_id` columns.

### 2.2 Layers, enforced

```
worker/
  domain/     pure: fsrs, grading, markdown, trivia state, merge policy,
              instant card hygiene, limiter arithmetic, row caps, srs ladder
  app/        use cases and PORTS: DeckRepo, UserPrefsRepo, SessionRepo,
              JobStatusRepo, AgentPort, PushSender, Clock, WorkflowRunner,
              Renderer, IdentityProvider; view-model DTOs (what a page needs,
              computed here, never in a template)
  runtime/    the Worker router, the cells, and ADAPTERS: SqlStorage repos,
              Clerk verifier, fake identity provider, anon cookie, agents,
              web-push over WebCrypto, nunjucks renderer, workflow runners;
              the composition root, where cross-cutting wrappers are applied
  templates/  the 49 templates, ported
tests/
  layering.test.ts   fails if domain imports app/runtime/cloudflare:/node:,
                     or if app contains fetch(, new Response, .sql.exec,
                     DurableObject
```

Cross-cutting concerns stay wrappers at the composition root, as they
are middleware today: the anonymous cookie refresh / clear on the
response path, `no-cache` on HTML, request timing. A route handler
never touches them.

**Where rendering runs.** Three of the nine template context processors
read the database per render (`agent_available`, `notif_unseen_count`,
and `deck_display`, a closure doing a SELECT from inside the template).
Identified pages therefore render **inside the `UserCell`**: one
activation per page, synchronous reads, one `pageContext` built by the
use case (with `deck_display` pre-resolved to a map). The router only
verifies identity and forwards. Unauthenticated pages (landing, privacy,
the offline shell, error pages, the reauth shell) render in the router.

### 2.3 Server-rendered HTML, on celld, first of its kind here

Both TS apps on the fleet are SPAs over static assets. prep is
server-rendered Jinja + htmx and the pixel bar rules out changing that:
the Worker renders HTML with **nunjucks**, the JavaScript port of
Jinja2, precompiled at build time. `extends`, `block`, `include`,
`import ... with context`, macros, `call`, `set` blocks, whitespace
control, custom filters and globals all exist. The templates port
near-verbatim, with a shim that was measured against the templates and
probed against nunjucks 3, not assumed:

| construct | sites | port |
| --- | --- | --- |
| `request.scope.get('root_path','')` | 100 | one `root` global |
| other `.get(k)` / `.get(k, default)` | 8 | property access; a `get(obj, k, default)` global where a default is used |
| Python `%` formatting | 6 | `format` filter (renders `NaN` otherwise) |
| slices `x[:n]`, `x[5:10]` | 20 | `slice` filter (parse error otherwise) |
| `x in ('a','b')` tuples | 10 | `x in ['a','b']` (**evaluates to false silently** in nunjucks; every terminal-state branch of the three progress partials depends on it) |
| `True` / `False` / `None` | 11 | `true` / `false` / `null` (**resolve to undefined silently**) |
| `dict.update({...})` group-by in a template | 4 | moved into the use case's view model, where it belongs |
| `.items()`, `.split()`, `.replace()`, `'sep'.join(list)` | 12 | `items()` global, native methods, `replace` and `join` filters (JS `.replace` replaces the first occurrence only) |
| `namespace()` | 1 | rewrite the one site |
| `|tojson` | 3 | a `tojson` filter reproducing markupsafe's `htmlsafe_json_dumps` (escapes `<`, `>`, `&`, `'`). `|dump` is wrong twice over: under autoescape it breaks the dashboard payload, and `|dump|safe` turns a deck name containing `</script>` into stored XSS |
| `|round` | 1 | banker's rounding (Python) not `Math.round`; user-visible in `deck.html` |
| floats reaching text (`50.0` vs `50`) | few | a `pyfloat` formatter where a float reaches text |
| `selectattr` / `rejectattr` counting | 3 | view-model fields |
| custom filters `markdown`, `wakes_in` (`relative_time` is dead) | 2 | registered filters |
| global `icon(name, class_=)` (91 uses, 38 SVGs) | 1 | registered global |
| 9 context processors | 9 | one `pageContext` from the use case (2.2) |

Byte-exact HTML is **not** the gate: markupsafe escapes `"` as `&#34;`,
nunjucks as `&quot;`; Jinja strips one trailing newline per template,
nunjucks keeps it; `tojson` separators differ. The gate for this layer
is **DOM equivalence**: parse both outputs, compare the element tree,
attribute **sets** (order-insensitive, values entity-decoded), text
nodes entity-decoded, and the order of `<script>` and `<link>` elements.
The attribute-set comparison is what protects the 34 JS modules, which
key on `data-*` hooks and ids that no pixel can see.

Static assets ship through wrangler `assets` with `run_worker_first`
for every non-asset route and for `/static/{js,css}/v*`, because the
worker owns the versioned-path alias rule (any accepted token, including
the legacy all-digit stamps, resolves to the current tree so pages
cached by old service workers keep loading).

### 2.4 Durable work: a port with two adapters

The four workflows keep their names and step structure:

| workflow | steps | human gate |
| --- | --- | --- |
| PlanGenerate | plan (LLM) -> wait for accept / reject / feedback (feedback re-plans; the timer is not refreshed) -> expand in batches of 4 (LLM) -> insert | up to 24h |
| Transform | compute (LLM) -> wait for apply / reject (deck and reorganize scope; card scope auto-applies) -> apply | up to 1h |
| TriviaGenerate | generate (LLM) -> insert per card | none |
| GradeAnswer | grade (LLM) -> record | none |

celld shipped Workflows upstream on 2026-08-22 (`step.do` with retries,
`sleep`, `waitForEvent` / `sendEvent`, `create` / `get` / `status` /
`terminate`; a step ledger in the cell's SQLite, every wait an alarm)
targeting 0.3.1. **There is no public 0.3.1 artifact yet.** The plan
does not wait on it:

- `app/ports/WorkflowRunner`: `start(kind, input)`, `signal(id, event)`,
  `status(id)`, `terminate(id)`.
- Adapter 1, **`AlarmLedgerRunner`**, ships first: a `JobCell` per job
  holds a step ledger in its SQLite; every LLM step is an idempotent
  row; the human gate is a persisted deadline; the alarm is derived from
  persisted state and re-armed on every activation (boardtogether's
  `ensureRoundAlarm`, proven to fire on evicted cells and to survive
  node restart). Retries are rows.
- Adapter 2, **`CelldWorkflowsRunner`**, when 0.3.1 has an artifact.
  Use cases do not change.

Status flows **one direction only: `JobCell` -> `UserCell`**, as an
idempotent status write (keyed by job id + transition) on every
transition. The 5s badge poll and the progress partials read the
`UserCell`'s job status rows and never touch a `JobCell`. Results land
in the owning `UserCell` by DO-to-DO RPC with the same idempotency keys
the Go worker uses today (`<job>-insert-N`, `<job>-expand-N`, the grading
key).

Instant anonymous generation stays one synchronous call with no ledger;
its real budget is 60s plus 15s adapter headroom, 75s, which is one of
the spikes (5.1). Its three-cell sequence is ordered: reserve in the
limiter, insert the deck in the new `UserCell`, register in the
directory, set the cookie, then settle the limiter row; a crash between
steps leaves a reserved slot that expires, never an orphan user.

### 2.5 The adapters

| concern | today | on celld | parity requirement |
| --- | --- | --- | --- |
| Clerk session | `clerk-backend-api` | kcal's `runtime/clerk.ts` **extended**: it verifies a Bearer JWT (RS256 over WebCrypto, single-flight JWKS, `iss/exp/nbf/sub`) and nothing else; prep adds the `__session` cookie source, `azp` against per-environment authorized parties, the `__client_uat` dormant read, and the reauth shell | same accept / reject decisions; reauth with and without the fallback cookie |
| Clerk webhooks | svix | svix signature over WebCrypto | `user.created/updated/deleted` only |
| anonymous account | HMAC-SHA256 cookie, HKDF from the master key | identical bytes via WebCrypto; refresh / clear as a response wrapper | existing cookies keep working across cutover |
| BYOK secrets | AES-256-GCM, `base64(nonce||ct||tag)` | identical via WebCrypto | existing rows decrypt with the same master key |
| PAT | global `token_hash` index | subject embedded: `prep_pat_<b64u(sub)>.<b64u(secret)>`, hash in the owner's cell (kcal). The user id is not a secret; it is in every deep link already | existing tokens reissued; users told through Clerk's email |
| AI | OpenAI-compat, Anthropic, free tier | the same three as `fetch` adapters behind `AgentPort` | same prompts, same parsers, the 5-card cap |
| push | `pywebpush` + `py_vapid` | a WebCrypto web-push adapter (VAPID JWT + aes128gcm) | the PEM VAPID key converted to JWK once at migration |
| markdown | mistune, with a 200-line client twin | ONE implementation: the client `markdown.js` promoted to the shared renderer, its 9 documented divergences closed against the 60 fixtures | the 60 fixtures pass server-side; the client keeps passing them |
| FSRS | `py-fsrs` 6.3.2 | `ts-fsrs` (FSRS-6) behind `domain/fsrs`, patched or vendored until fixture-equal | 4.2; risk 1; decision 7.5 |
| grading | `domain/grading.py` | direct port; the `sorted()` list repr in the multi-choice feedback reproduced exactly | the existing `grader.js` fixtures |
| Anki | Python sqlite + zip | `fflate` + `sql.js`, WASM loaded through the module-import path (spike 5.1) | same first-field / cloze-skip / flatten rules; same minimal `collection.anki21` |
| MCP | hand-rolled JSON-RPC | hand-rolled stateless JSON-RPC (kcal), 17 tools | same tool names and schemas |
| identity for local e2e | Tailscale headers | a fake identity provider, never enabled by a deploy file | the local offline suites keep running |
| metrics | Prometheus process gauges + histograms | a request histogram per isolate at `/metrics`; the threadpool gauges are gone with the threadpool | documented reduction |

### 2.6 The merge, as a saga

Today the merge is one `BEGIN IMMEDIATE`. On celld it spans three cells,
so it is written as a saga with a marker, in this order:

1. `DirectoryCell` records `merging(anon -> target)`. From this moment
   the identity resolver treats the anonymous id as the target.
2. The anonymous `UserCell` exports; the target imports, idempotent by
   row id, applying the policy (reassign decks / questions / sessions /
   log / job rows; drop-on-conflict for the offline idempotency ledger;
   delete push subscriptions, BYOK rows, tokens; carry `desired_retention`
   and `editor_input_mode` if the target has none; de-collide deck slugs
   with numbered suffixes then random bytes; enforce the target's caps).
3. `DirectoryCell` writes the audit row (`previous_ids` comes from here)
   and marks the merge done.
4. The anonymous cell writes its own **tombstone** and `deleteAll`s the
   rest. Every anonymous-cell activation checks its tombstone before
   serving, so a stale cookie naming a merged or reaped id cannot
   resurrect an empty account, and no per-request directory read is
   needed. The reaper uses the same tombstone.

---

## 3. The repository, the fleet, and secrets

The rewrite lives **in the same repo**, as kcal's did: a top-level
`worker/` beside the Python tree. Python stays the reference and the
golden generator until cutover, then is deleted in one commit.

Deploy targets live in `infra/prep/` (never in the app repo): a
`celld/{staging,prod}.yaml` pair, buckets `prep-cells-staging` and
`prep-cells`, one least-privilege MinIO user each, the digest-pinned
celld image, `CELLD_IDLE_EVICT_S`, the internal-listener NetworkPolicy,
a nightly `mc mirror` backup like kcal's. Prod is pinned by a
`.celld-version` naming the bundle tag, mirroring `.vps-version`; merge
and tag before the first deploy, as always.

`wrangler.jsonc` is per environment and **public-values only** (Clerk
issuer, publishable key, JWKS), with the house deny-list rule: a value
that is a secret or an internal address never enters it.

**Secrets have no house path into a cell yet.** prep needs six:
`PREP_KEY_ENCRYPTION_SECRET`, `PREP_ANON_COOKIE_SECRET`,
`CLERK_SECRET_KEY` (account deletion calls Clerk), the webhook secret,
the free-tier API key, the VAPID private key. celld's accepted config has
no secrets key, and no fleet app carries an application secret today.
Spike 5.1 answers whether celld exposes process env or a deploy-time
secrets file; if neither, the design is a sealed `vars` file generated
at deploy from a k8s Secret and never committed. Phases 3 and 6 cannot
start without the answer.

---

## 4. The parity gate

Three tiers, cheapest first, all in CI on every PR; the third also runs
against staging before any promotion.

### 4.1 DOM goldens (equivalence)

Every template rendered from a fixture context on the Python side is a
golden; the TS renderer must produce a DOM-equivalent document (2.3).
Fixtures cover every branch of the three progress partials, terminal
and non-terminal, and a user-controlled string containing `</script>`
inside the dashboard payload.

### 4.2 Domain oracles (exact, from the Python implementation)

Corpora extracted from Python before it is touched:

- FSRS: several thousand `(state, rating, elapsed, retention) -> next
  state` transitions covering fresh cards, ladder seeding, every step
  bucket, retention clamping. **Extracted with fuzzing disabled**: prod
  runs py-fsrs with `enable_fuzzing=True` (its default), which
  randomizes review intervals of 2.5 days and up through Python's global
  `random`, so an exact due-timestamp oracle does not exist for the live
  configuration. Stability and difficulty must agree to 1e-9; due
  timestamps exactly on the fuzz-off branch. Whether the TS scheduler
  keeps fuzz is decision 7.5.
- Grading: the `grader.js` fixtures plus every branch of
  `validate_regex_update`.
- Markdown: the 60 cases, server-side, `js_expected` retired.
- Merge: an anonymous account with rows in every scoped table, both
  idempotency ledgers, prefs to carry, and slug collisions, merged into a
  target at its caps; rows, audit row, `previous_ids` compared.
- Offline sync: recorded pairs for every status, the 422s, the
  idempotency replay.
- Contracts: recorded JSON for every API listed in 1.1, the `Set-Cookie`
  headers of the anonymous lifecycle, the MCP tool list and one call per
  tool, `openapi.json`.

### 4.3 Pixel goldens (the operator's bar)

One Chromium build, one context spec, one clock, one set of font bytes,
one canned LLM, both sides. Anything less cannot round to zero.

Context: 393x852, DPR 3, iOS UA, `is_mobile`, `has_touch`,
`reduced_motion: reduce` (the stylesheet zeroes every animation and
transition under it), `color_scheme` in {light, dark}, `timezone_id`
fixed and the parity user's tz fixed to match, `service_workers: block`
except on the offline flows.

Pins, each a known diff source:

- **Fonts, pinned to the same bytes on both sides before any golden is
  captured.** Google serves per-UA subsetted, differently hinted builds
  of Fraunces and JetBrains Mono; different bytes shift outlines by
  sub-pixels and the block rule fails on every text run. The harness
  routes the font URLs to checked-in files at capture and at compare
  time; every shot waits on `document.fonts.ready`.
- **A canned LLM.** An OpenAI-compatible stub with fixed responses keyed
  by prompt hash, reachable from both staging deployments through the
  free-tier base URL, plus a BYOK key for the parity user pointing at
  it, with a **hold** mode that never answers until told, so
  "in progress" screens are a state and not a race. Phase 0
  infrastructure.
- Clock: `PREP_FAKE_NOW` honored by both servers, `page.clock.install`
  on the client; fixtures carry absolute timestamps. On the Python side
  this is a refactor, not a flag: 28 direct `datetime.now()` /
  `time.time()` calls across 16 files route through one clock seam.
- ClerkJS (major-version floating from Clerk's host) and the redoc
  script: pinned or blocked identically in both runs.
- Polling screens are captured after the first `htmx:afterSwap`, on
  both sides.
- The landing placeholder: seeded. Build token: identical for the run.

Comparison: per-channel tolerance 2/255, failing pixels at most 0.02%
of the area, and zero failing pixels in any 8x8 block. Scattered
anti-aliasing passes; a shifted glyph fails. Diff masks are pytest
artifacts rendered into the existing test report.

Screens: enumerated from the template list, not from memory. Every
page template, every partial in each of its states (the nine transform
diff-card states, the plan re-planning round, the improve dialog, the
PWA nudge, the duration sheet), the three error pages, the reauth shell
with and without the fallback cookie, the sign-out choice and device
wipe dialogs, the trivia deep-link mode, question new / edit with the
editor, the deck chooser and the three importers with their error
states, export, split, reorganize. **About 70 screens x 2 schemes.**

Goldens are captured from the Python app on staging after the pins
land and **before the rewrite touches anything else**, which is why the
harness is phase 0.

The parity host (`celld.staging.prepcards.app`, the hostthis pattern)
is registered as a Clerk authorized party on the staging instance and
shares the cookie domain, or no signed-in flow can be captured there.

---

## 5. Phases

Each phase runs as the usual dispatch: a spec agent, implementation
agents split by bounded context, a test agent, an adversarial review,
then the gates. A phase is done when its gates are green on staging.

### 5.1 Phase 0b, the runtime spikes (before the taxonomy is committed)

Each has a pass / fail criterion and a fallback. All are cheap; none can
be skipped, because the taxonomy and the secrets design depend on them.

1. **Secrets delivery**: does a cell see process env, or does `celld
   deploy` accept a secrets file? Fail -> the sealed-`vars`-at-deploy
   design (3).
2. **Request deadline**: a 75s outbound `fetch` from a cell inside one
   request completes. Fail -> instant generation becomes a `JobCell`
   with a polling page.
3. **`sql.js` in a cell**: WASM through the module-import path (celld
   resolves a `.wasm` sidecar; runtime compilation from bytes is
   forbidden). Fail -> Anki import/export moves to a tiny Go sidecar
   (kcal's catalogue shape) and is named as the exception.
4. **Cell deletion**: `deleteAll()` plus tombstone; does the bucket
   reclaim the objects? Fail -> tombstone-only, with a documented
   garbage cost.
5. **nunjucks precompiled bundle**: 4,954 template lines with custom
   filters; size and cold-activation cost inside the 128 MB heap.
6. **DO-to-DO RPC from a `JobCell` into a `UserCell`** with an
   idempotent write, under a node kill.

### 5.2 Phase 0, the gate itself

Python changes, the only ones in the plan: the clock seam refactor,
the seeded placeholder, the free-tier base URL pointable at the stub,
the font routing. Then: the pixel harness, the canned LLM with hold
mode, the goldens captured from the current app on staging, the domain
oracle corpora (FSRS with fuzz off), the DOM-equivalence differ.
Deliverable: a red-capable gate, proven by perturbing one pixel, one
FSRS parameter, and one `data-*` attribute.

### 5.3 Phase 1, the skeleton

`worker/` layout, wrangler files per environment, the layering test,
the four cell classes declared (reviewed hardest: the one-way door),
the fake identity provider (so fixture pages render signed-in without
Clerk), the nunjucks renderer with the full shim, all 49 templates
ported with view-model DTOs replacing template-side computation, static
assets and the SW contract including the alias rule, the
`infra/prep/celld/` fleet on staging behind the parity host. Gates: DOM
goldens equivalent for every template; pixel gate green for every
screen renderable from fixtures through the fake provider.

### 5.4 Phase 2, the domain

FSRS, grading, markdown unification, trivia state, instant hygiene,
limiter arithmetic, row caps, merge policy. Pure TS, vitest, no I/O.
Gate: every oracle corpus passes. Runs in parallel with phase 1.

### 5.5 Phase 3, the user's data

`UserCell` and its repositories; rendering inside the cell; auth
(Clerk with cookie, `azp`, dormant read, reauth; anonymous lifecycle
wrapper; forget-device; PAT; webhooks); `DirectoryCell`; the merge saga
with tombstones; dashboard, deck page, study API and sessions, trivia
play, settings, notify prefs and push, offline snapshot (with
`previous_ids`) and sync, API v1, MCP, OpenAPI. Gates: contract goldens
including `Set-Cookie`; pixel gate on every read and study flow.

### 5.6 Phase 4, durable work

`WorkflowRunner` with `AlarmLedgerRunner`; the four workflows; the
`JobCell -> UserCell` status writes; instant generation with
`InstantLimiterCell` in the stated order; the AI adapters; per-user
alarms for digest, when-ready, trivia refill (dispatching jobs), quiet
hours; the daily reaper. Gates: pixel gate on generation, grading,
transform, plan and trivia flows against the canned LLM; kill-the-node
mid-plan and mid-gate tests (boardtogether's `restart.test.ts` shape).
Can start against phase 2 before phase 3 finishes.

### 5.7 Phase 5, the long tail

CSV, `.prepdeck`, Anki; legal pages; `/metrics`; the debug endpoints;
the e2e suite re-pointed: the Clerk-staging files carry over as they
are, the local offline suites run against a local celld node through
the fake provider with the harness able to kill the node. Gate: the
full e2e suite green against the staging fleet.

### 5.8 Phase 6, migration and cutover

The exporter (per user from SQLite, **plus** the global tables:
`account_merges` into the directory, the limiter ledger into the limiter
cell or an explicit reset, `last_seen_at` per user) and the idempotent
importer into `UserCell`s through an internal endpoint, rehearsed on
staging against a prod snapshot; per-user count and FSRS-state
verification; PAT reissue notice through Clerk email; VAPID key
conversion; the cutover runbook (hostthis's shape: migrate with prod
up, a short announced window, the old system never mutated, rollback
is an ingress flip **and loses the window's writes**); promotion; the
Python tree, the Go worker and the container deleted.

**When 0.3.1 ships**, at any point after phase 4: `CelldWorkflowsRunner`
behind the same port and tests; the ledger cell stays as the status
read model.

Sizing, honestly: 15-20k lines of TypeScript including tests (kcal's
worker was 2.5k for a far smaller surface, boardtogether 2.9k). The
critical path is 0b -> 0 -> 1 -> 3 -> 6; phases 2 and 4 overlap it.

---

## 6. Risks, and what closes each

1. **FSRS parity.** `ts-fsrs` implements FSRS-6 but is not a verified
   twin of `py-fsrs`, and prod is fuzzed. Closed by the fuzz-off corpus
   and by patching or vendoring until fixture-equal; decision 7.5
   settles fuzz. If parity cannot be reached on the stored state
   fields, the fallback is migrating every card's due date with the
   divergence documented, surfaced to the operator with numbers.
2. **celld Workflows not yet obtainable.** Closed by the port and the
   alarm-ledger adapter.
3. **First server-rendered app on the fleet.** Closed by the DOM gate
   in phase 1 before any data path exists, rendering inside the cell,
   and precompiled templates.
4. **The two silent nunjucks classes** (tuple `in`, capitalized
   literals). Closed by the shim and by fixtures that exercise every
   progress-partial branch; a parse error would be caught, silence
   would not.
5. **The cell taxonomy is permanent.** Closed by the spikes preceding
   it and an adversarial review of the four classes before the first
   deploy.
6. **Secrets delivery unknown.** Spike 1; phases 3 and 6 wait on it.
7. **MinIO conditional writes.** Upstream says the community edition
   lacks what celld needs; the fleet runs one replica per app, where a
   two-owner race cannot occur. prep inherits that posture; scaling any
   fleet to two nodes is a separate decision.
8. **75s synchronous LLM call inside a request.** Spike 2.
9. **`sql.js` in the isolate.** Spike 3.
10. **PAT format change.** Announced through Clerk email; the one path
    that cannot be byte-compatible.
11. **Rollback loses the cutover window's writes.** The window is short
    and announced; users are told.
12. **Observability shrinks.** No threadpool gauges; per-isolate
    histograms reset on eviction. Logs flow as before.

---

## 7. Decisions the operator owns before phase 1

1. The four-class taxonomy (2.1). It cannot be renamed later.
2. In-repo `worker/` over a new repository (3).
3. PAT reissue and its notice channel (2.5).
4. The per-user `claude_subscription` BYOK provider: drop and notify, or
   a sidecar (1.3).
5. FSRS fuzz: keep it in TS (`ts-fsrs` fuzz, verified only
   distributionally) or turn it off (a scheduling change for every
   user, not pixel-visible, behavior-visible).
6. The debug endpoints: keep (assumed) or drop.

---

## 8. Not in scope

Any new feature. Any visual change. A SPA. Multi-node celld fleets.
Cloudflare hosting (the target is the self-hosted fleet). Keeping the
Python app alive after cutover.

---

## 9. What this plan is not yet

It is not a spec for any phase. Each phase opens with its own spec
agent, and the spikes in 5.1 may change 2.1 and 3 before phase 1 starts.

---

## 10. Inventory the plan rests on (measured 2026-08-25)

Routes: 139 decorators across 19 modules (85 optional-identity, 23
signed-in, 7 PAT, 2 internal token, the rest open), plus the curated
OpenAPI surface; 4 htmx polling fragments (badge 5s, plan 2s, transform
2s, trivia 1s); no SSE or WebSockets. Tables: 18. Templates: 49 (32
pages, 4 macro files, 11 partials, base, offline, error). First-party
JS: 34 modules, 7.0k lines. Workflows: 4 on one task queue; only
PlanGenerate (24h) and Transform (1h) hold a human gate. Background:
two asyncio loops carrying 4 jobs (notify digest and when-ready, trivia
refill, anonymous reaper, workflow reconciler). Tests: 1,297 functions
in 110 files; 23 e2e files. Integrations: Clerk (sessions, webhooks),
three AI adapter families plus the free tier, web push, MCP (17 tools),
Anki. Context processors: 9, three of which read the database. The
data set is small enough that migration is measured in seconds.
