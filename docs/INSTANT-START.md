# Instant start

Design spec for the anonymous first-run flow: a new visitor lands,
types what they want to learn, gets an AI-generated deck stored
locally on their device, and studies it immediately. No account, no
auth wall, no configuration. An account becomes the thing you get
LATER, to keep the deck across devices, instead of the thing that
gates the first card.

Companions: [AI-PROVIDERS.md](AI-PROVIDERS.md) (the free inference
tier this flow spends) and [OFFLINE.md](OFFLINE.md) (the local-first
storage, study surface, and sync machinery this flow reuses).
Read both; this spec deliberately adds as little new machinery as
possible on top of them.

---

## 1. Goal, baseline, target

### The measured baseline

A first-run walkthrough on a real phone (iOS Safari, fresh visitor,
public deploy) measured roughly **nine minutes and about ten
decisions** between landing and the first studyable card. The gates,
in order:

1. The landing page promises "describe what you want to learn" but
   contains no input. The only action is a sign-in link.
2. Auth wall on the first tap: a hosted sign-in page on a foreign
   domain with its own branding, defaulting to sign-IN for a visitor
   who has no account, then email + password + OTP round trip.
   Roughly four minutes before the app shows anything of its own.
3. A "pick a kind" five-way fork including jargon (SRS) and options
   impossible for a new user (restore a backup).
4. A name-the-deck-before-describing-it form.
5. A "Plan & generate" vs "Create empty" fork.
6. A plan-approval interstitial with the accept button below the
   fold.

Gates 3 through 6 are signed-in flow frictions and are explicitly
**out of scope here** (section 7, follow-ups). This spec removes
gates 1 and 2 for new visitors by making the landing page the
product.

### Target

- **Time to first card under 60 seconds.** Type a topic, tap
  Generate, study. Generation itself is the long pole. Free-tier
  generations are capped at 5 cards (product decision: a fast
  first deck with bounded spend beats a slow full one): measured
  generation-shaped calls against the configured free tier ran
  11.5s and 15.5s for 5 cards (~260 output tokens), versus 33 to
  44 seconds for 12-card probes, which is what drove the cap.
  Client copy says "usually 10 to 20 seconds". Everything around
  the generation must be near-zero.
- **One decision.** The topic is the only thing the visitor chooses.
  No kind picker, no name field, no plan approval, no account.
- **The account ask arrives after value, not before.** A quiet
  disclosure that the deck is device-local, and a non-blocking nudge
  after the first study session. Never a modal wall.

### Non-goals (v1)

- Changing any signed-in flow. The dashboard, /decks/new, the plan
  flow, and trivia are untouched.
- Anonymous trivia decks. The instant deck is an SRS deck of short
  cards; the existing offline study surface covers exactly that.
- Anonymous BYOK, model choice, or card-count choice.
- CAPTCHA. Abuse control in v1 is rate limiting plus a circuit
  breaker (section 3.2); the escalation path is specced, not built.
- Multi-deck anonymous accounts. One guest deck per device at a
  time (a second generation before sign-up replaces after confirm,
  section 2).

---

## 2. UX flows

### The anonymous landing

`GET /` for an unauthenticated visitor keeps the same URL and
branching logic (`prep/web/index.py:161-208`); only the template
content changes. The hero section of `templates/landing.html:16-30`
becomes the product:

- Eyebrow: `prep`. Headline: **"What do you want to learn today?"**
- A single textarea (placeholder: a concrete example topic), a
  primary **Generate my deck** button, and one quiet disclosure line
  under the form (section 3.5).
- The existing marketing sections (walkthrough, benefits, CTA band,
  `templates/landing.html:35-163`) move below the fold, demoted, not
  deleted. The masthead sign-in chip stays
  (`templates/landing.html:10-12`).
- The hero renders only when the deploy's free tier is configured
  (`free_tier_configured()`, `prep/agent/selector.py:236-240`,
  passed into the template context by the index route) AND the auth
  provider exposes a sign-in URL (`get_provider().urls().sign_in`
  is not None; only clerk mode does). Outside clerk mode there is
  no self-serve sign-up for the nudge CTA to link, and on
  tailscale-mode deploys unauthenticated requests are a dev-only
  shape anyway. A deploy failing either condition renders today's
  landing unchanged.

Signed-in users never see any of this: the authenticated branch of
`index()` (`prep/web/index.py:209-280`) is untouched, and so is the
reauth-shell branch for dormant sessions
(`prep/web/index.py:184-195`).

### The generation loop (client)

States of the hero form, driven by
`static/js/modules/instant-start.js` (new):

1. **Idle.** Button enabled once the module loads. A
   `<noscript>` line replaces the form promise: "Instant decks need
   JavaScript. Sign in to build decks instead."
2. **Generating.** On submit: button flips to `is-loading` (equal
   size spinner, no layout shift, per the UX rails in CLAUDE.md), a
   status line cycles honest copy ("Writing your cards. Usually 10
   to 20 seconds."). One in-flight request max; the fetch carries
   `AbortSignal.timeout(75000)`, slightly above the server's cap.
3. **Ready.** On 200: cards are written to IndexedDB (section 3.3),
   the form swaps to a ready panel: "Your deck: {display_name}, N
   cards", the first three prompts as a preview, and a primary
   **Start studying** button that navigates to `<root>/offline`.
4. **Error.** Inline line under the form, input preserved:
   - `rate_limited` scope `minute`: "One deck a minute. Try again
     shortly."
   - `rate_limited` scope `day`: "You've reached today's limit.
     Create a free account to keep going." (links sign-in; failed
     attempts that spent upstream tokens count toward the limit,
     section 3.2, so the copy must not promise three successes)
   - `busy`: "The free AI is busy right now. Try again in a few
     minutes."
   - `generation_failed` / network / timeout: "That didn't work.
     Try again."
   - IndexedDB write failure (rare): the ready panel still renders
     the cards read-only from memory, with "Couldn't save on this
     device. Create an account to keep this deck." replacing Start
     studying.

### Returning anonymous visitor

On module init, `instant-start.js` reads IndexedDB (same check shape
as `static/js/modules/offline-link.js`). When `meta.guest` exists
AND `meta.owner` is absent, a **Continue studying** strip renders
above the input: deck name, card count, due count, linking to
`<root>/offline`. The owner-absent condition is load-bearing:
owner-present devices never write `meta.guest` (section 3.3), and
the strip must never render from guest metadata on a device whose
cards sync through the normal owner flush. Generating again with a
guest deck present asks one confirm ("Replace your current deck
({name})? It's only stored on this device.") before overwriting; v1
keeps one guest deck per device. The replace also deletes
`outbox_reviews` rows referencing the replaced cards: an orphan
review whose card no longer exists would otherwise surface as a
reject at adoption time. On an owner-present device there
is no guest deck to replace: generation appends authored cards to
that owner's local state with no confirm (section 2 edge table).

### The guest study surface

Study happens at `/offline`, the existing client-rendered app
(decision: redirect, not a landing-hosted island; the offline app
already implements the queue, card views, grading, and the ladder,
and reusing it wholesale is the least new code). The shell and app
gain a **guest mode** presentation branch so it reads as the app,
not as a degraded fallback. Guest mode is a client-side derivation:
`meta.owner` absent AND guest data present (section 3.3); the same
cached shell bytes serve both modes.

Guest-mode differences (all in `offline-app.js`; mostly copy, but
the owner-null guards and the reconnect suppression below are real
logic changes and are specced as such):

- Masthead brand renders "Prep" without the "offline" suffix
  (`templates/offline.html:37-44` stays static; the JS rewrites the
  brand word in guest mode).
- Overview prelude: "Your deck" / the deck's display name, "N cards
  are due right now." No "Studying as ..." line (there is no owner).
  This is an owner-null GUARD, not just copy: `renderOverview`
  dereferences `state.owner.display_name` (`offline-app.js`)
  and would throw on the null owner the new boot gate admits.
- The overview footer's snapshot-stamp line reads
  `state.owner.snapshot_at` (`renderOverview`'s footer) and throws
  the same way; in guest mode that line is replaced by the
  local-only disclosure line (the account nudge, below). These two
  are the complete set of owner dereferences on the guest render
  path; any new one added later must carry the same guard.
- The deck list renders the one guest deck from `meta.guest` with
  live counts (the snapshot `decks` store is empty for guests).
- The verdict screen's "offline schedule" qualifier (the
  `scheduleNote` the shell passes to `verdictView` in
  `static/js/study/components.js`) is dropped in guest mode; the
  ladder IS the schedule for a guest.
- The reconnect path is suppressed. `syncOnReconnect`
  (`offline-app.js`) runs on every shell boot; for a guest with any
  queued review its flush would 401 and raise the "Back online. Open
  prep to finish syncing." banner (same function) on every
  online guest session, a nag directly against "feels like the app,
  not the fallback". In guest mode the shell skips the reconnect
  flush and its banner entirely: a guest has no account to sync
  with until adoption.
- Study mechanics are byte-identical: queue ordering, grading,
  ladder scheduling, verdict screens all run the existing code
  paths (section 3.3).

### The account nudge

Two surfaces, both quiet, both guest-mode only:

1. **Persistent line** in the overview footer (with the other footer
   lines in `renderOverview`, `offline-app.js`): "This deck is stored only on
   this device. Create an account to keep it across your devices."
   with a Create-account link. Always present, never animated,
   never blocking.
2. **Post-session banner**: when a guest study session reaches the
   caught-up screen (`renderCaughtUp` in `offline-app.js`, view built
   by `caughtUpView` in `static/js/study/components.js`), a
   banner card renders above it: "Nice work. This deck lives only
   on this device so far. Create a free account to keep it and
   study anywhere." Primary CTA links the sign-in URL; a "Not now"
   dismiss stamps `meta.guest.nudge_dismissed_at` and suppresses the
   banner (the footer line stays). A guest whose data is owner-absent
   `local_cards` only (offline authoring, no generated deck) has no
   `meta.guest` record to stamp; the dismiss persists as
   `meta.guest_nudge {dismissed_at}` instead, same suppression. The
   banner never interrupts a card, never repeats within a page load,
   and never renders as a modal.

The sign-in URL reaches the shell via the `/offline` route context
(`prep/web/pwa.py:152-172` gains `sign_in_url` from
`get_provider().urls()`, rendered as a data attribute; the SW-cached
copy carries it, and it is deploy-stable).

### Sign-up and adoption

The visitor taps a nudge CTA, completes the identity provider's
flow, and lands back on `/` as an authenticated user. On that first
authenticated page load the sync module detects adoptable guest data
and shows the **one-time adoption confirm** (section 3.4): "Add your
deck to this account?" with the deck name and counts. Accept moves
the deck and every recorded review into the account through the
existing sync engine; the next render shows the deck on the real
dashboard with FSRS state reflecting the anonymous study. Decline
(explicit "Discard it") wipes the guest data. Dismissing decides
nothing and re-prompts on the next load.

A visitor who already HAS an account follows the same path: sign in
from the masthead chip, adoption confirm on the next load, deck
lands next to their existing decks.

### Edge states

| Scenario | Behavior |
| --- | --- |
| Free tier not configured on the deploy | Landing renders today's marketing hero; no input, no endpoint promise. |
| Generation busy (breaker or per-minute cap tripped, or upstream contention) | Inline busy copy; input preserved; no retry storm (button stays manual). |
| Generation failed (unparseable output, upstream 5xx, timeout) | Inline error, input preserved. These failures spent upstream tokens, so they COUNT toward the daily windows and the global breaker (section 3.2); only refusals that never reached the upstream are free. |
| JS disabled | `<noscript>` line; sign-in path unaffected. |
| Returning anonymous visitor with a guest deck | Continue-studying strip on the landing; `/offline` works directly; studying offline-after-first-visit works via the existing SW precache (nothing new to cache: the landing module lives in `static/js/modules/`, already precached wholesale, `prep/web/pwa.py:96-104`). |
| Anonymous visitor who already has an account | Signs in via the masthead chip; adoption confirm if guest data exists. |
| Device already has an owner snapshot (a signed-out returning user, or a borrowed device) | The landing input still works; generated cards land in `local_cards` exactly like offline-authored cards and belong to that owner's device state. `meta.guest` is NOT written (section 3.3), so no Continue-studying strip renders and no stale guest metadata outlives the flush. The generated cards join `allStudyCards` (`offline-app.js`) and interleave with the owner's due cards in one queue; accepted v1 behavior, identical to authoring cards offline on that device. When the SAME owner signs back in, the existing silent flush applies (OFFLINE.md M4 semantics: the device is theirs, this is authoring). When a DIFFERENT user signs in, the existing owner-mismatch guard refuses and the existing conflict dialog runs (`sync.js:62-74`). The adoption gate applies only to owner-ABSENT devices. |
| Second generation before sign-up | One confirm, then replace (v1: one guest deck per device). |
| IndexedDB unavailable | Cards render read-only from memory; account CTA replaces Start studying. |
| Guest deck on a plain Safari tab | Subject to the 7-day script-storage cap like all offline data (OFFLINE.md section 3); the local-only disclosure is the honest mitigation, and adoption is the durable exit. |

---

## 3. Architecture

### 3.1 The anonymous generation endpoint

A new bounded context `prep/instant/` (`routes.py`, `service.py`,
`repo.py`), following the per-context layout.

**`POST /api/instant/generate`** - unauthenticated, JSON in/out.

Request: `{"topic": "<free text>"}`. Topic is stripped, control
characters removed, required non-empty, capped at 500 characters
(422 beyond; the client mirrors the cap with `maxlength`).

Response 200:

```json
{
  "display_name": "Postgres MVCC",
  "cards": [
    {"prompt": "...", "answer": "...", "answer_regex": "...|null"}
  ]
}
```

Errors, all JSON with a `kind` the client branches on:

| Status | kind | When |
| --- | --- | --- |
| 422 | `invalid_topic` | empty / over-cap topic |
| 429 | `rate_limited` (+ `scope`: `minute` or `day`, + `retry_after_s`) | per-IP window exceeded |
| 429 | `busy` | global breaker or global per-minute cap tripped, concurrency cap hit, or upstream `AgentBusy` |
| 502 | `generation_failed` | adapter `AgentUnavailable`, timeout, or unparseable output |
| 503 | `not_configured` | free tier absent on this deploy |

**Free-tier only, by construction.** The service resolves its
adapter through `free_tier_agent()`
(`prep/agent/selector.py:165-233`) directly, NEVER through
`agent_for_user()`. This is load-bearing, not stylistic:
`agent_for_user(None)` consults the deploy-wide subscription token
on non-clerk deploys (`prep/agent/selector.py:369-372`), and an
anonymous internet endpoint must never be able to spend a BYOK key
or the operator's subscription pool under any deploy shape. The
adapter is resolved per request (the factory is cheap and
never-raising by contract); `None` means 503. A module-level test
seam (`set_instant_agent_factory`, same pattern as
`selector.set_user_agent_factory`,
`prep/agent/selector.py:70-81`) lets tests inject `FakeAgent`.

**Output-capped, by construction.** `free_tier_agent()` builds its
adapter with the transform-sized `_FREE_TIER_MAX_TOKENS = 32768`
(`prep/agent/selector.py:162`), and `AgentPort.run()` exposes no
per-call max_tokens, so reusing that adapter unchanged would let an
adversarial topic pull 16x the output this endpoint budgets for on
every call. `free_tier_agent()` therefore gains an optional
`max_output_tokens` parameter (default `None` preserves 32768 for
every existing caller), and the instant service passes
`PREP_INSTANT_MAX_OUTPUT_TOKENS` (default 1024, roughly 4x an
honest 5-card response, ~260 output tokens measured, so real decks
never truncate). A response
cut off at the cap fails parsing or the 3-card floor and returns
`generation_failed`; that outcome still counts as spend
(section 3.2). The abuse arithmetic in 3.2 depends on this cap
being ENFORCED per call, never assumed.

**Synchronous single call, no plan step, no Temporal.** One prompt,
one adapter call in the async route handler (non-blocking; the
free-tier adapter is httpx-async). This is deliberately simpler
than the signed-in plan flow: no workflow, no polling UI, no worker
involvement. The SERVICE owns the 60s deadline (`asyncio.wait_for`)
and hands the adapter a 75s transport backstop: the shared adapter
maps its own transport timeout to `AgentBusy`, which section 3.2
would misclassify as a free refusal, so the service deadline must
fire first and classify the stall as spend
(`generation_failed`). A timeout the adapter still surfaces
arrives as `AgentTimeout` (an `AgentBusy` subclass meaning "the
request WAS issued") and also counts as spend.

**The prompt** reuses the trivia generation shape, which already
produces exactly what the offline grader consumes. A new template in
`prep/instant/service.py`, derived from `_GEN_PROMPT_TEMPLATE`
(`prep/trivia/service.py:51-133`): "exactly 5" q/a/r items (the
free-tier card cap, section 1), the same
answer-length constraints, the REGEX GUIDANCE block with its
regex-semantics rules intact but its grader-fallback lines
rewritten (the trivia block tells the model "the grader has a
separate path" for paraphrase / typo-tolerant matching and "the
grader has fallbacks", `prep/trivia/service.py:115-119`; the
offline grader has neither, a null regex means reveal +
self-verdict, so the instant prompt states exactly that and pushes
the model to emit a regex whenever the answer shape allows one,
because a verbatim copy would bias it toward omitting regexes
exactly where they matter most), the same
"return ONLY valid JSON" instruction, minus the existing-questions
block and minus the explanation field (SRS short cards have no
explanation surface in the offline app; smaller output also means a
faster, cheaper call). No per-user context of any kind enters the
prompt: topic text only.

**Output parsing reuses the existing tolerant parser.**
`_parse_qa_pairs` (`prep/trivia/service.py:142-161`) is promoted to
a shared pure module (`prep/domain/qa_extract.py`), with
`prep/trivia/service.py` importing it from there (no behavior
change, pinned by the existing trivia tests). The instant service
then applies the same per-item hygiene `generate_batch` does
(`prep/trivia/service.py:184-215`): skip non-dict items, require
non-empty q and a, validate each regex with
`grading.validate_regex_update(r, expected_literal=a)`
(`prep/domain/grading.py`) and drop to null on failure. Server-side
caps on the response: at most 16 cards (truncate), prompt at most
2000 chars, answer at most 500 (over-cap items skipped); fewer than
3 surviving cards means degenerate output and returns
`generation_failed`. Prompt-injection blast radius note: the model
output never touches server state; it is parsed, validated, and
returned to the requester's own device, so an adversarial topic can
only produce a bad deck for its own author.

`display_name` is derived server-side from the topic: whitespace
collapsed, capped at 60 chars.

**Metrics**: `prep_instant_generate_duration_seconds{outcome}`
histogram (outcomes: `ok`, `rate_limited`, `busy`, `failed_spent`,
`failed_free`, `invalid`), same registration pattern as the grade
histogram in `prep/web/metrics.py:72-86`. This is the operator's
abuse and saturation dial; the spend/free split is what makes a
forced-failure campaign (section 3.2 outcome classes) visible as a
`failed_spent` spike instead of hiding inside a generic failure
count.

### 3.2 Abuse control

This endpoint spends the deploy's shared free-tier key on
unauthenticated internet traffic; abuse control is the make-or-break
of the feature. Layers, outermost first:

**Client IP resolution, proxy-aware and verified.** The app runs
behind an ingress that terminates TLS, and uvicorn currently runs
with `--proxy-headers --forwarded-allow-ips "*"`
(`docker/Procfile.docker`, app line). With every peer trusted,
uvicorn's forwarded-header handling resolves `request.client.host`
from the LEFTMOST `X-Forwarded-For` entry, which the client itself
can supply; `request.client.host` is therefore SPOOFABLE under the
current config and must not be the limiter key as-is. A helper
`client_ip(request)` in `prep/instant/routes.py`:

- Reads the header named by `PREP_CLIENT_IP_HEADER` (default
  `x-real-ip`, the ingress-set header on both public deploys).
- `PREP_CLIENT_IP_HEADER=x-forwarded-for-last` selects the LAST
  `X-Forwarded-For` entry (the one appended by the trusted ingress)
  for ingress setups that append rather than overwrite.
- A missing header, or a value that does not parse as an IP
  address, fails CLOSED: the request is keyed to a single shared
  sentinel bucket subject to the same per-IP windows. It NEVER
  falls back to `request.client.host`: per the uvicorn analysis
  above, that value is client-supplied under the container's
  always-on `--proxy-headers --forwarded-allow-ips "*"`
  (`docker/Procfile.docker`), so no trustworthy socket-peer value
  is reachable through it in ANY deploy shape, proxied or not; a
  no-proxy deploy where clients connect directly is equally
  spoofable because the client sends its own `X-Forwarded-For`.
  Dev without an ingress lands in the sentinel bucket, a tight
  shared allowance that is fine for manual poking; tests inject
  the header.
- The derived address is normalized before keying: IPv4 keys on
  the exact address, IPv6 keys on its /64 prefix. One host
  trivially owns a /64, so exact-address v6 keys would let a
  caller rotate past both per-IP windows for free, exhaust the
  global breaker in minutes (a feature-wide DoS for the day), and
  drain upstream spend with zero limiter friction. The stored
  `ip` value is this bucket string.

**Verification is a REQUIRED staging gate before the landing ships**
(milestone M4 gate, section 6): from an external host, curl the
staging endpoint with forged `X-Real-Ip` and `X-Forwarded-For`
headers and confirm from the request log line (the service logs the
derived IP for every generation at INFO) that the derived IP is the
caller's real address, not the forgery. If the ingress turns out
not to overwrite `X-Real-Ip`, flip the env to
`x-forwarded-for-last` and re-verify. Do not ship the landing on an
unverified header.

**Per-IP sliding windows, sqlite-backed.** New table (created
idempotently in `db.init()`, same pattern as existing migrations):

```sql
CREATE TABLE IF NOT EXISTS instant_generations (
  id         INTEGER PRIMARY KEY,
  ip         TEXT NOT NULL,   -- limiter bucket: exact IPv4, or IPv6 /64 prefix
  created_at TEXT NOT NULL,
  outcome    TEXT NOT NULL DEFAULT 'pending',
             -- pending|ok|failed_spent|failed_free (classes below)
  cards      INTEGER,
  topic_chars INTEGER
);
CREATE INDEX ... ON instant_generations (ip, created_at);
CREATE INDEX ... ON instant_generations (created_at);
```

**Outcome classes decide what counts.** Four `outcome` values:
`pending` (reserved, call in flight), `ok`, `failed_spent` (the
upstream call was ISSUED and then timed out, returned 5xx, or
produced unparseable, truncated, or degenerate output), and
`failed_free` (refused with NO upstream spend: semaphore
contention after reservation, or an upstream 429 / `AgentBusy`).
The split is the load-bearing rule of this section: the topic is
attacker-controlled text inside the prompt, so failures are
FORCEABLE ("reply with a poem, no JSON" reliably fails parsing
while spending full tokens). Any outcome that spent upstream
tokens must count against every quota-protecting window; only
genuinely no-spend refusals are free. Exempting failures from the
daily windows would hand an attacker unlimited spend: force a
failure, repeat every 60s per bucket, and nothing but the burst
window ever accumulates.

Limits (env-tunable, defaults):

| Limit | Default | Window | Counts |
| --- | --- | --- | --- |
| per-IP burst | 1 | 60s | every row (all outcomes) |
| global per-minute (`PREP_INSTANT_GLOBAL_PER_MINUTE`) | 4 | 60s | pending + ok + failed_spent |
| per-IP daily (`PREP_INSTANT_PER_IP_PER_DAY`) | 3 | 24h | pending + ok + failed_spent |
| global daily breaker (`PREP_INSTANT_GLOBAL_PER_DAY`) | 200 | 24h | pending + ok + failed_spent |

The burst window counts everything, so a hammering client is
throttled regardless of upstream health. Spend failures counting
toward the visitor's daily allowance is a deliberate tradeoff: a
legitimate visitor whose three attempts all die on upstream 5xx
loses the day's allowance through no fault of their own, which is
the price of failing closed; the error copy (section 2) promises a
limit, not three successes.

The global daily breaker protects the free tier's shared daily
token quota for the whole deploy (AI-PROVIDERS.md section 2): 200
generations at the ENFORCED 1,024-token output cap (section 3.1)
is at most ~205k output tokens/day, a bounded fraction of the
quota that leaves the signed-in flows their headroom. That
arithmetic holds only because the cap is enforced per call and
spend failures count toward the breaker. The global per-minute cap
rations the OTHER shared budget, the per-minute output-token rate:
the semaphore bounds concurrency, not calls per minute, and short
generations could otherwise stack enough calls in one minute to
push signed-in grading into its string-match fallback for the
duration of a burst. Four spends per minute at the 1,024 cap
bounds instant's worst-case share of the minute budget. Breaker or
per-minute trips return `busy`, indistinguishable on the wire from
upstream contention, which is honest: from the visitor's seat both
mean "the shared capacity is spent, try later".

**Check-and-reserve is one transaction.** The limiter check and the
`pending` row INSERT happen inside a single sqlite transaction in
`prep/instant/repo.py`, so concurrent requests cannot both pass a
count they jointly exceed (the reserved row IS what the next
request's count sees; the gate and its subject come from one read).
The row's `outcome` resolves to `ok` / `failed_spent` /
`failed_free` when the request completes. Rows older than 7 days
are pruned opportunistically on insert.

**Concurrency cap.** A module-level `asyncio.Semaphore(2)` around
the upstream call; contention beyond it returns `busy` immediately
rather than queueing (the reserved row resolves `failed_free`: no
spend happened). Two concurrent 60s generations bound the
endpoint's own resource use; the global per-minute cap above is
what bounds the token rate.

**Prompt length cap** (500 chars, section 3.1) bounds the input
tokens an anonymous caller can spend per request.

**Escalation path (specced, not built).** If the metrics show
sustained abuse (breaker tripping daily, single-subnet farming):
first tighten the env knobs (they deploy without code changes) and,
for v6 farming across a wider allocation, the IPv6 bucket from /64
to /56 (one line in `client_ip`); second, add an env-gated CAPTCHA
challenge (e.g. a Turnstile-class
token verified server-side) on the generate call only, keeping the
zero-friction path the default when the gate is off; third, require
sign-in for generation on the affected deploy by unsetting the
instant-start env entirely (the landing degrades to today's hero
automatically). None of these change the endpoint contract.

### 3.3 Client storage model

Everything lands in the EXISTING `prep-offline` IndexedDB database
(`static/js/offline/store.js:28-29`), no schema version bump: IDB
rows are schemaless per record, both new fields ride on existing
stores, and `store.js` object stores are unchanged.

**What a generated deck writes** (all under `withLock`,
`store.js:42-46`, same interleaving discipline as every other
multi-store write):

- `meta.guest` (new meta record), written ONLY when `meta.owner`
  is absent: `{deck_client_id, display_name, topic, created_at,
  nudge_dismissed_at?}`. The guest deck's identity and the nudge
  bookkeeping. There is deliberately NO `meta.owner` write: owner
  absence is the adoption signal (section 3.4). The owner-absent
  condition on the write is equally deliberate: on an owner-present
  device the generated cards are ordinary authored `local_cards`
  rows and nothing else. Writing `meta.guest` there would be junk
  from the moment it lands: only adoption Accept deletes it as part
  of the state machine, and the state-3 silent flush never does.
  An owner-present `meta.guest` (an Accept interrupted between the
  owner stamp and the deletion is the one path that produces one)
  is inert debris, since every guest surface is owner-absent-gated;
  the successful-refresh path sweeps it opportunistically so it
  cannot linger as a stale Continue-studying strip or
  replace-confirm target.
- `meta.guest_nudge` (new meta record) `{dismissed_at}`: the
  post-session nudge dismissal for a guest with no `meta.guest`
  record (owner-absent `local_cards` only). Written by the banner's
  "Not now" in that state; generation never writes it.
- One `local_cards` row per card:

```
{client_id: uuid(), deck_id: null, deck_name: <display_name>,
 type: "short", prompt, answer, answer_regex: <string|null>,
 created_at: toISOString(), local_step: 0, local_next_due: null}
```

Two fields are new on `local_cards` rows relative to OFFLINE.md M4
authoring (`LocalSource.author`, `static/js/study/source.js`):
`type: "short"` +
`answer_regex`, and `deck_name`. Both are additive; existing
authored rows without them behave exactly as today.

**Study works through the existing modules unchanged:**

- The queue already merges `local_cards` into the study pool
  (`allStudyCards`, `offline-app.js`); `local_next_due:
  null` means due immediately (`scheduler.due`,
  `static/js/offline/scheduler.js:40-49`), so the whole fresh deck
  is the queue.
- Grading: `grader.grade` dispatches on `card.type`
  (`static/js/offline/grader.js:128-139`). Because guest rows now
  carry `type: "short"` plus a server-validated `answer_regex`, the
  regex path auto-grades typed answers (the defensive compile +
  cross-engine rules in `grader.js:matchRegex` apply as-is); a null
  regex falls through to reveal + self-verdict. This is the "feels
  like the app" moment: type an answer, get a verdict.
- Scheduling: `recordVerdict` (`static/js/study/source.js`) seeds the
  ladder from `local_step` and writes the overlay + the outbox
  review with `card_client_id`, exactly the M4 path. Anonymous
  study is real spaced repetition on the ladder
  (`scheduler.js:18-36`).
- Offline-after-first-visit is free: the landing registers the SW
  (`static/js/app.js`, SW registration block) and every module the
  guest surface needs is already in the precache manifest
  (`prep/web/pwa.py:96-104`).

**The deck without a server deck id.** The guest deck exists only
as `meta.guest` plus the shared `deck_name` on its rows. The
`decks` store (server snapshot ids) is never touched; guest mode
renders its one deck line from `meta.guest`. The server learns the
deck's name at adoption time via the `deck_name` field on the
new-cards wire (section 3.4), so no client-side fake numeric id
ever exists to collide with snapshot ids.

**Boot gate change.** `offline-app.js` `init()` currently renders the
empty state when `meta.owner` is absent. New rule: empty only when
there is neither an owner nor guest data; owner-absent with guest
data boots into guest-mode overview.

### 3.4 The adoption state machine

The walk of the current owner logic in `sync.js` that this design
builds on:

- `ownerAllows(serverUser)` (`sync.js:62-74`): mismatch between
  `meta.owner.user_id` and the server-resolved user disables sync
  for the session and arms the conflict dialog. **Owner ABSENT
  passes the guard silently today.**
- `refreshSnapshot` (`sync.js:95-174`): on a clean fetch it replaces
  `decks`/`cards` and STAMPS `meta.owner` (`sync.js:161-166`).
- `flushOutbox` (`sync.js:292-410`): posts `local_cards` as
  new-cards chunks first, then reviews; runs on every authenticated
  page load via `init()` (`sync.js:625-659`, wired in
  `static/js/app.js`).

Consequence, and the trap this section exists to close: without a
new gate, the first authenticated load on a guest device would
(a) silently flush the guest cards into whatever account signed in,
and (b) even if the flush were somehow skipped, `refreshSnapshot`
would stamp `meta.owner`, destroying the owner-absent signal while
the guest rows remain, converting the NEXT flush into a silent
adoption. The gate must therefore run before BOTH the flush and the
snapshot write, in the same code path, in the same milestone as the
landing ships.

**States.** Derived per authenticated page load from
(`meta.owner`, guest data present, server user), where "guest data
present" means `meta.guest` exists or `local_cards` is non-empty
while `meta.owner` is absent:

| # | meta.owner | Guest data | Server user | Behavior |
| --- | --- | --- | --- | --- |
| 1 | absent | none | any | Seed: snapshot refresh stamps the owner. Today's behavior, unchanged. |
| 2 | absent | present | any | **ADOPTABLE.** No flush, no snapshot write. Show the adoption confirm (below). |
| 3 | present | n/a | same id | Normal sync: silent flush + refresh. Unchanged (this is also what owner-present devices do with landing-generated cards, section 2 edge table). |
| 4 | present | n/a | different id | Owner MISMATCH: refuse, existing conflict flow, wholly unchanged (`sync.js:62-74`, `sync.js:585-611`). |

The distinction that matters: **absent is adoptable, mismatch
refuses.** They are different answers and must never collapse into
one check.

**State 2, the one-time confirm.** A dialog in the shape and DOM
discipline of the existing owner-conflict dialog
(`sync.js:481-576`: `textContent` only, backdrop/Esc closes,
busy-guarded buttons):

- Title: "Add your deck to this account?"
- Body: the deck's display name, card count, and recorded review
  count ("{name}: N cards, M reviews from before you signed in").
- **Add to my account** (primary):
  1. The in-memory `adoptionApproved` latch is set (implementation
     shape below). Without it the next two steps deadlock against
     the gate this section installs: the owner is still absent and
     `local_cards` is non-empty when Accept runs, so
     `guestAdoptionPending()` would refuse the very flush that
     completes adoption.
  2. `flushOutbox()` runs: cards first (now carrying `deck_name`
     and `answer_regex` on the wire), then reviews by
     `card_client_id`, through the untouched chunking and
     idempotency machinery (`sync.js:292-410`).
  3. Forced `refreshSnapshot()` stamps `meta.owner` and delivers
     the deck back as real snapshot cards (the existing
     created-card carry logic in `sync.js:344-365` keeps everything
     studyable if the refresh races).
  4. `meta.guest` is deleted. Toast: "Deck added to your account."
- **Discard it** (danger): under `withLock`, clear `local_cards`,
  `outbox_reviews`, `rejects`, and `meta.guest`, then proceed as
  state 1 (seed refresh). Explicit data loss, named counts in the
  dialog, never silent.
- **Dismiss** (backdrop/Esc): decides nothing. No flush, no
  snapshot write this session; re-prompt on the next authenticated
  load. Undecided is not consent (the same rule the owner-conflict
  dialog pins for "keep", `sync.js:523-529`).

Implementation shape in `sync.js`: a `guestAdoptionPending()`
predicate consulted at the top of BOTH `refreshSnapshot()` and
`flushOutbox()` (returning `{ok:false, adoption_pending:true}`),
plus a module-level in-memory `adoptionApproved` latch the
predicate consults FIRST. The latch is what lets Accept's own
flush and refresh through the gate (Accept step 1); it is
memory-only and never persisted, so an adoption interrupted
mid-flush (tab closed, network lost) loses the latch and
re-prompts on the next authenticated load, where the untouched
idempotency machinery makes the retried flush safe. After Accept
completes, the latch is moot: `meta.guest` is gone, `local_cards`
have flushed, and the owner stamp puts the device in state 3. An
`initAdoption()` step in `init()` fetches the snapshot payload
once (identity only, no store writes, reusing
`fetchSnapshotPayload`, `sync.js:76-87`), evaluates the state
table, and either proceeds with the normal chain or shows the
dialog. The offline shell's reconnect path
(`syncOnReconnect`, `offline-app.js`) gets the same treatment through the
shared module when a user is signed in; in guest mode reconnect is
a no-op, no flush and no banner (section 2, guest-mode
differences).

**What happens to anonymously recorded reviews.** They are ordinary
outbox rows keyed by `card_client_id`. At adoption the server
creates each card through the existing validated path
(`prep/offline/service.py:77-129`,
`prep/offline/repo.py:170-195`), then replays every review in
clamped `reviewed_at` order through the REAL scheduler
(`prep/offline/service.py:135-201`), with the `(offline auto)` /
`(offline self-graded)` grader notes
(`prep/offline/service.py:43-46`). The account's FSRS state
therefore reflects the studying the visitor did before signing up;
nothing is re-graded, nothing is lost, and retries are idempotent
by `(user_id, client_id)` exactly as for offline sync.

**Server-side wire extensions** (the only server changes adoption
needs):

- `SyncNewCard` (`prep/offline/entities.py:70-84`) gains
  `deck_name: Any = None` and `answer_regex: Any = None`. Same
  Any-typed, validate-per-item discipline; absent fields behave
  exactly as today, so every existing client and test stays green.
- `_process_card` (`prep/offline/service.py:77-113`): when
  `deck_id` is null and `deck_name` is a non-empty string (stripped,
  capped 80 chars), resolve through a new
  `SyncRepo.resolve_named_srs_deck(user_id, deck_name)`:
  get-or-create an SRS deck with a slug derived from the name,
  reusing the SRS-scoping defenses of `resolve_srs_inbox`
  (`prep/offline/repo.py:147-168`; a trivia deck squatting on the
  name gets a suffixed SRS sibling, never a cross-type insert). An
  unusable `deck_name` falls back to the inbox rather than
  rejecting: the card matters more than its label.
- `_validate_new_card` (`prep/offline/service.py:115-129`) +
  `create_card` (`prep/offline/repo.py:170-195`): a string
  `answer_regex` is re-validated server-side with
  `grading.validate_regex_update(regex, expected_literal=answer)`
  and stored on the question row (the `answer_regex` column exists,
  `prep/infrastructure/db.py:384-394`); failures store null. Never
  trust the client's copy of a regex the server will later grade
  with.

Batch caps are untouched (`prep/offline/entities.py:28-29`); a
5-card deck plus its reviews fits one chunk of each with two
orders of magnitude to spare.

### 3.5 Privacy disclosure

Anonymous prompts go to the deploy's shared free inference endpoint,
a third-party service (AI-PROVIDERS.md section 4). The landing
carries one quiet line directly under the topic input, always
rendered when the hero is:

> Deck generation runs on a shared free AI service; the text you
> type is sent there. Create an account and add your own key for a
> provider of your choosing.

Same information the settings page discloses, compressed to one
line, present BEFORE the first prompt is typed. No consent
checkbox: typing into a box labeled by this line is the consent,
matching the deploy-wide default-on decision already made for the
free tier.

---

## 4. What changes, per file

Server:

| File | Change |
| --- | --- |
| `prep/instant/routes.py` (new) | `POST /api/instant/generate`, `client_ip()` helper (header modes, fail-closed sentinel bucket, IPv6 /64 normalization), error `kind` mapping (section 3.1 table). |
| `prep/instant/service.py` (new) | prompt template (derived from `prep/trivia/service.py:51-133`, grader-fallback lines adapted), adapter resolution via `free_tier_agent()` only with the instant `max_output_tokens`, `set_instant_agent_factory` test seam, output hygiene + caps, display-name derivation, semaphore. |
| `prep/instant/repo.py` (new) | `instant_generations` table access: transactional check-and-reserve, outcome-class resolution (`failed_spent` vs `failed_free`), per-minute + daily window counts, pruning. |
| `prep/agent/selector.py:165-233` | `free_tier_agent()` gains an optional `max_output_tokens` parameter; default `None` preserves 32768 for every existing caller. |
| `prep/domain/qa_extract.py` (new) | `parse_qa_pairs` moved from `prep/trivia/service.py:142-161`, pure. |
| `prep/trivia/service.py:142-161` | import the parser from `prep/domain/qa_extract.py`; no behavior change. |
| `prep/infrastructure/db.py` | idempotent `instant_generations` DDL in `init()`. |
| `prep/web/index.py:196-208` | anonymous branch passes `instant_enabled=free_tier_configured()` into the landing context. Authenticated branch untouched. |
| `prep/web/pwa.py:152-172` | `/offline` context gains `sign_in_url` (from `get_provider().urls()`). |
| `prep/web/metrics.py` | `prep_instant_generate_duration_seconds{outcome}` histogram + observe helper (pattern of `prep/web/metrics.py:72-86`). |
| `prep/offline/entities.py:70-84` | `SyncNewCard` + `deck_name`, `answer_regex`. |
| `prep/offline/service.py:77-129` | named-deck resolution when `deck_id` is null; server-side regex re-validation. |
| `prep/offline/repo.py:147-195` | `resolve_named_srs_deck`; `create_card` stores `answer_regex`. |
| `prep/app.py:417-428` | mount the instant router. |
| `.env.example` | `PREP_CLIENT_IP_HEADER`, `PREP_INSTANT_PER_IP_PER_DAY`, `PREP_INSTANT_GLOBAL_PER_DAY`, `PREP_INSTANT_GLOBAL_PER_MINUTE`, `PREP_INSTANT_MAX_OUTPUT_TOKENS` names + comments. |

Client:

| File | Change |
| --- | --- |
| `templates/landing.html:16-30` | hero becomes the topic form (JS-gated on `instant_enabled`), disclosure line, `<noscript>` fallback; marketing sections demoted below (`:35-163` kept); Continue-studying strip container. |
| `static/js/modules/instant-start.js` (new) | form state machine (idle / generating / ready / error), IDB writes via `@/offline/store.js` (`meta.guest` only when `meta.owner` is absent), returning-visitor strip gated on owner-absent, replace-deck confirm (guest mode only). Lazy-imported by `app.js` on its `data-instant-start` hook (same convention as `offline-link.js`). |
| `static/js/app.js` | one lazy-import block for the new hook. |
| `static/js/offline/sync.js` | `guestAdoptionPending()` gate at the top of `refreshSnapshot` (`:95-174`) and `flushOutbox` (`:292-410`), opened by the in-memory `adoptionApproved` latch for Accept's own flush; `initAdoption()` state-table step in `init()` (`:625-659`); the adoption dialog (shape of `:481-576`); `toWireCard` (`:202-210`) passes `deck_name` + `answer_regex`. `ownerAllows` (`:62-74`) and the owner-conflict flow (`:585-611`) untouched. |
| `static/js/offline/offline-app.js` | boot gate (`init()`) admits owner-absent guest data; owner-null guards on the two `state.owner` dereferences the gate exposes (both in `renderOverview`); guest-mode presentation branches (prelude, deck line from `meta.guest`, the `scheduleNote` verdict qualifier, footer disclosure line, post-session nudge banner in `renderCaughtUp`); reconnect (`syncOnReconnect`) is a no-op in guest mode (no flush, no banner) and defers to the shared adoption gate when signed in. |
| `templates/offline.html` | `sign_in_url` data attribute on the root element. |

Untouched on purpose: `static/js/offline/grader.js`,
`static/js/offline/scheduler.js`, `static/js/offline/store.js`
schema, all signed-in routes and templates, the worker, and every
`prep/agent/` adapter.

---

## 5. Test plan

Layered as the rest of the suite (`make lint` / `make test` /
`make e2e`).

**Unit + route (pytest, `tests/instant/`, new):**

- Rate limiter: burst (2nd request inside 60s -> 429 minute),
  daily (4th spend inside 24h -> 429 day with `retry_after_s`),
  outcome classes (`failed_spent` rows count toward the daily
  windows, the per-minute cap, AND the breaker; `failed_free` rows
  count toward the burst only), the forced-failure drill (a run of
  `failed_spent` rows trips the global breaker exactly as `ok`
  rows would; pins finding-shaped abuse where every call fails on
  purpose), global per-minute cap at the env cap, global breaker
  at the env cap, per-IP isolation (limits on IP A never throttle
  IP B), IPv6 bucketing (two addresses inside one /64 share a
  bucket; two different /64s do not), check-and-reserve atomicity
  (two concurrent requests at count N-1 admit exactly one),
  pruning. Time injected, no sleeps.
- `client_ip`: header modes (`x-real-ip`, `x-forwarded-for-last`),
  missing or non-IP header values key to the sentinel bucket and
  NEVER to `request.client.host` (pins the fail-closed rule),
  sentinel-bucket requests share the per-IP windows, spoofed
  `X-Forwarded-For` with header trust on the OTHER header does not
  move the derived IP, IPv6 normalization to the /64 bucket.
- Free-tier-only selection: with `CLAUDE_CODE_OAUTH_TOKEN` set on a
  tailscale-mode test env AND a BYOK row present, the endpoint
  still resolves the free adapter or 503s when the free tier is
  unset; the subscription and BYOK paths are unreachable from this
  endpoint by construction.
- Generation semantics with `FakeAgent` via the factory seam:
  happy path (5 items -> 200 with validated regexes), the factory
  receives the instant `max_output_tokens` and never the
  transform-sized default (pins the output cap), fenced / preamble
  output survives the shared parser, invalid regex dropped to
  null, over-cap and under-3-card outputs -> 502 recorded
  `failed_spent`, `AgentBusy` -> 429 busy recorded `failed_free`,
  `AgentUnavailable` -> 502 `failed_spent`, timeout -> 502
  `failed_spent`, topic caps -> 422 (no row). Outcome class
  asserted per branch.
- Parser move: existing trivia generation tests stay green against
  the `prep/domain/qa_extract.py` import (pins the no-behavior-
  change refactor).

**Adoption, server side (extend `tests/offline/test_sync.py`):**

- `deck_name` get-or-create: new deck created SRS-typed with
  slugged name; second batch with the same name reuses it; a
  trivia deck squatting on the name yields a suffixed SRS deck
  (mirrors the inbox pins); absent/invalid `deck_name` falls back
  to inbox; existing no-deck_name tests untouched and green.
- `answer_regex`: valid regex stored and returned in the next
  snapshot's card; regex failing `validate_regex_update` stored as
  null; non-string garbage ignored.
- Reviews recorded anonymously: a new_cards + card_client_id
  reviews batch replays through FSRS at the client timestamps and
  the resulting card state equals direct `schedule_review` calls
  (extends the existing replay-math pin).

**Existing pins that MUST stay green (regression contract):** the
whole `tests/offline/` suite, the owner-guard behavior (mismatch
refuses; the conflict dialog semantics), sync idempotency, and the
purge-guard + selector precedence tests in `tests/agent/` and
`tests/byok/`. The adoption gate adds a state, it must not move any
existing one.

**Browser e2e (`tests/e2e/`, marked `browser`):**

- **Full anonymous loop, mocked generation:** Playwright with NO
  auth header, `ctx.route` intercepting `POST /api/instant/generate`
  with a canned 5-card response. Land -> type topic -> Generate ->
  ready panel -> Start studying -> `/offline` guest overview (deck
  name, N due; no thrown owner-null errors in the console) ->
  auto-grade a regex short (typed answer, verdict without
  self-grade) -> self-verdict a null-regex card -> reach caught-up
  -> nudge banner present, footer disclosure present, and the
  "Back online" sync banner NEVER appears during the guest session
  (pins the reconnect suppression) -> reload landing ->
  Continue-studying strip. Asserts the entire client machine with
  zero upstream spend.
- **Busy path:** same harness, route returns 429 `busy`: inline
  copy renders, input preserved, no IDB writes.
- **Adoption path:** seed guest data (either via the mocked loop or
  direct IDB writes in `page.evaluate`), then reload with the
  header-injection auth fixture (`tests/e2e/conftest.py`): adoption
  dialog appears BEFORE any sync effect; Accept -> deck + cards +
  review state visible via the API, `meta.owner` stamped,
  `meta.guest` gone (Accept COMPLETING is itself the pin on the
  `adoptionApproved` latch: without the disarm the gate refuses
  Accept's own flush and this test hangs on a deck that never
  lands); separate cases for Discard (stores cleared, normal seed
  follows) and Dismiss (no flush, no owner stamp, re-prompt on
  next load). Plus the existing owner-mismatch e2e running
  unchanged.
- **Owner-present device path:** with the auth fixture and an
  owner snapshot seeded, a landing generation writes `local_cards`
  only: no `meta.guest`, no Continue-studying strip, no adoption
  dialog on the next load (the cards ride the normal state-3 silent
  flush).
- **One real-endpoint smoke** (marked `slow`), against a deploy
  with the free tier configured: a single real generation through
  the live endpoint, asserting 200-shape or tolerating 429
  busy/rate_limited (the limiter applies to the runner too). The
  only test that spends real tokens.

**Staging gates (manual, per milestone 4):** the forged-header
real-IP verification (section 3.2, required), one real
generate-study-signup-adopt pass on a phone, and a rate-limit probe
from a second network.

---

## 6. Milestones

Each merges to main, tags, and deploys independently; the feature
stays invisible until M4 swaps the landing.

- **M1: the endpoint, dark.** `prep/instant/` context: route +
  service + limiter (outcome classes, per-minute cap) + breaker +
  semaphore + `client_ip` (sentinel bucket, IPv6 /64) + the
  `free_tier_agent()` output-cap parameter + metrics + the
  `qa_extract` promotion + DDL. Nothing links to it, but it is
  internet-reachable from the moment it deploys, so the full abuse
  layer ships INSIDE this milestone, not after it. All unit/route
  tests. Env knobs documented in `.env.example`. The real
  generation-shaped latency measurements are DONE (section 1:
  11.5s and 15.5s for 5 cards; 33 to 44s for the 12-card probes
  that drove the 5-card cap); the section-1 range and the client
  copy already carry them.
- **M2: sync wire extensions.** `SyncNewCard.deck_name` +
  `answer_regex`, named-deck resolution, regex re-validation +
  storage. Pure server-side widening, exercised only by tests until
  M4. Existing offline clients unaffected.
- **M3: the adoption gate.** `sync.js` state table +
  `guestAdoptionPending` interlock + the `adoptionApproved` latch +
  the adoption dialog + offline-shell reconnect integration + the
  adoption e2e (guest data seeded directly). Ships BEFORE the
  landing so no build ever exists where guest data can be created
  but silently absorbed: the gate precedes the data source. One
  residual window: a shell precached before M3 carries the ungated
  sync.js until the SW updates (`skipWaiting`, `static/sw.js:79`,
  narrows but does not close it; it needs a network slow enough to
  fall back to the old shell yet good enough to flush). Accepted:
  guest data cannot exist before M4 ships, and M4 deploying after
  M3 means every client that can reach the landing hero has also
  fetched the gated shell at least once.
- **M4: the landing + guest surface.** Landing hero swap +
  `instant-start.js` (owner-absent gating of `meta.guest` and the
  strip) + guest-mode presentation (owner-null guards, reconnect
  suppression) + nudges + disclosure + `/offline` sign-in URL + the
  anonymous-loop, busy, and owner-present-device e2e. **Gate: the staging real-IP verification and the manual
  phone pass (section 5) before promote.** This is the milestone
  where the target metric is measured for real: a stopwatch run of
  landing-to-first-card on staging goes in the PR description.
- **M5: polish + proof.** Real-endpoint smoke in the e2e suite,
  metrics panel check on the operator side, limiter knob tuning
  from first-week numbers, and LAST: record the demo of the full
  loop (landing -> deck -> study -> nudge -> sign-up -> adopted
  dashboard) on the simulator or a real phone, as the before/after
  companion to the baseline audit.

---

## 7. Follow-ups (explicitly not this spec)

- The signed-in flow frictions from the baseline audit (the kind
  fork, name-before-description, plan-approval fold): a separate
  milestone with its own spec, after instant start proves the
  landing conversion.
- CAPTCHA escalation (section 3.2) if the metrics demand it.
- Multi-deck guest state, guest trivia decks, guest explanations.
- A `retry_after_s`-aware auto-retry on the generating screen.
- Redaction pattern for the free-tier key in `log_redaction.py`
  (already tracked in AI-PROVIDERS.md section 5).
