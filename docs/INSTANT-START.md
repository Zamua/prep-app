# Instant start

Design spec for the anonymous first-run flow: a new visitor lands,
types what they want to learn, gets an AI-generated deck, and studies
it immediately. No account, no auth wall, no configuration. Signing up
becomes the thing you do LATER, to keep the deck across devices,
instead of the thing that gates the first card.

Companions: [AI-PROVIDERS.md](AI-PROVIDERS.md) (the free inference
tier this flow spends) and [OFFLINE.md](OFFLINE.md) (the local-first
storage, study surface, and sync machinery this flow reuses).
Read both; this spec deliberately adds as little new machinery as
possible on top of them.

**Where the deck lives is no longer this spec's call.**
[ANONYMOUS-ACCOUNTS.md](ANONYMOUS-ACCOUNTS.md) is the authority on
identity, storage, and sign-up: a generation mints a server-side
anonymous account and stores the deck under it. Sections 1, 3.1, 3.2
and 3.5 below still describe live machinery. The rest described the
client-side plan that was built and then removed, and each of those
sections now carries a pointer in place of its content rather than a
description of code no reader can find.

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
branching logic in the entry worker; only the template content
differs. The hero section of `worker/templates/landing.html` is the
product:

- Eyebrow: `prep`. Headline: **"What do you want to learn today?"**
- A single textarea (placeholder: a concrete example topic), a
  primary **Generate my deck** button, and one quiet disclosure line
  under the form (section 3.5).
- The existing marketing sections (walkthrough, benefits, CTA band,
  further down `landing.html`) sit below the fold, demoted, not
  deleted. The masthead sign-in chip stays.
- The hero renders only when the deploy configured a free tier AND
  the identity provider exposes a sign-in URL (only Clerk does).
  Without a sign-in URL there is no self-serve sign-up for the
  nudge to link to. A deploy failing either condition renders the
  plain landing page.

Signed-in users never see any of this: the identified branch renders
the dashboard in the user's own cell, and a dormant session gets the
reauth shell instead.

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
3. **Ready.** On 200 the server has already stored the deck under the
   visitor's account and answers with the URL to land on, so this
   state is one navigation and nothing else. No panel, no preview, no
   client-side card write.
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

### The device-local deck, its nudges, and adoption

Retired. A generation mints a real server-side anonymous account and
stores the deck under it, so an anonymous visitor reaches the same
deck, study, and dashboard surfaces a signed-in one does, and signing
up MERGES the account instead of adopting a device-local deck. The
returning-visitor strip, the guest study surface, the account nudge,
and the adoption confirm are all gone with it. See
[ANONYMOUS-ACCOUNTS.md](ANONYMOUS-ACCOUNTS.md) sections 3 to 5, with
the removal itself in sections 8 and 9.

### Edge states

| Scenario | Behavior |
| --- | --- |
| Free tier not configured on the deploy | Landing renders today's marketing hero; no input, no endpoint promise. |
| Generation busy (breaker or per-minute cap tripped, or upstream contention) | Inline busy copy; input preserved; no retry storm (button stays manual). |
| Generation failed (unparseable output, upstream 5xx, timeout) | Inline error, input preserved. These failures spent upstream tokens, so they COUNT toward the daily windows and the global breaker (section 3.2); only refusals that never reached the upstream are free. |
| JS disabled | `<noscript>` line; sign-in path unaffected. |
| Returning anonymous visitor | The cookie resolves the account, so `/` is the dashboard and every deck is where they left it. |
| Anonymous visitor who already has an account | Signs in via the masthead chip; the merge moves the anonymous decks into it (ANONYMOUS-ACCOUNTS.md section 5). |
| Device already has an owner snapshot (a signed-out returning user, or a borrowed device) | The deck belongs to the cookie's account, not to the device: nothing is written locally, so the snapshot owner's offline state is untouched. The existing owner-mismatch guard still governs a later sign-in by a different user. |
| Second generation before sign-up | A second deck in the same account, up to the anonymous caps (ANONYMOUS-ACCOUNTS.md section 6). |
| IndexedDB unavailable | Nothing here needs it: generation, storage, and study all go through the server. |

---

## 3. Architecture

### 3.1 The anonymous generation endpoint

The use case is `worker/app/instant/generate.ts`, the route is
`worker/runtime/routes/instant.ts` (it is served by the entry worker,
not a cell, because a visitor has none yet), and the pure parts are
`worker/domain/instant/` (`cards.ts`, `limiter.ts`, `ip.ts`).

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
adapter from the free-tier configuration directly
(`runtime/adapters/agents/freeTier.ts`), NEVER through the per-user
selector. This is load-bearing, not stylistic: an anonymous internet
endpoint must never be able to reach a stored BYOK key under any
deploy shape. No free tier configured means 503 `not_configured`.

**Output-capped, by construction.** The shared tier's general cap is
the transform-sized `FREE_TIER_MAX_OUTPUT_TOKENS` (32768). Reusing it
here unchanged would let an adversarial topic pull 32x the output this
endpoint budgets for, on every call, so instant passes
`INSTANT_MAX_OUTPUT_TOKENS` (1024) instead: roughly 4x an honest
5-card response, so real decks never truncate. A response
cut off at the cap fails parsing or the 3-card floor and returns
`generation_failed`; that outcome still counts as spend
(section 3.2). The abuse arithmetic in 3.2 depends on this cap
being ENFORCED per call, never assumed.

**Synchronous single call, no job, no gate.** One prompt, one adapter
call in the route handler. This is deliberately simpler than the
signed-in plan flow: no `JobCell`, no polling UI, no alarm. The use
case owns the deadline and hands the adapter a longer transport
backstop: the shared adapter maps its own transport timeout to
`AgentBusy`, which section 3.2 would otherwise misclassify as a free
refusal, so the use case's deadline must fire first and classify the
stall as spend (`generation_failed`). A timeout the adapter does
surface arrives as `AgentTimeout`, an `AgentBusy` subclass meaning
"the request WAS issued", and also counts as spend.

**The prompt** (`domain/instant/cards.ts::buildPrompt`) reuses the
trivia generation shape, which already produces exactly what the
offline grader consumes: "exactly 5" q/a/r items (the
free-tier card cap, section 1), the same
answer-length constraints, the REGEX GUIDANCE block with its
regex-semantics rules intact but its grader-fallback lines
rewritten (the trivia block tells the model the grader has a
separate path for paraphrase and typo-tolerant matching, and that it
has fallbacks; the offline grader has neither, a null regex means reveal +
self-verdict, so the instant prompt states exactly that and pushes
the model to emit a regex whenever the answer shape allows one,
because a verbatim copy would bias it toward omitting regexes
exactly where they matter most), the same
"return ONLY valid JSON" instruction, minus the existing-questions
block and minus the explanation field (SRS short cards have no
explanation surface in the offline app; smaller output also means a
faster, cheaper call). No per-user context of any kind enters the
prompt: topic text only.

**Output parsing shares the tolerant parser.**
`domain/instant/cards.ts::extractCards` is the same extraction the
trivia path uses, plus the same per-item hygiene: skip non-object
items, require non-empty q and a, validate each regex with
`validateRegexUpdate` from `domain/grading/` and drop it to null on
failure. The caps are constants in that module: `MAX_CARDS` truncates
the list, `CARD_PROMPT_MAX_CHARS` (2000) and `CARD_ANSWER_MAX_CHARS`
(500) skip over-cap items, and fewer than `MIN_CARDS` (3) survivors is
degenerate output and returns `generation_failed`. Prompt-injection blast radius note: the model
output never touches server state; it is parsed, validated, and
returned to the requester's own device, so an adversarial topic can
only produce a bad deck for its own author.

`display_name` is derived server-side from the topic: whitespace
collapsed, capped at `DISPLAY_NAME_MAX_CHARS` (60).

**Metrics**: `prep_instant_generate_duration_seconds{outcome}`
histogram (outcomes: `ok`, `rate_limited`, `busy`, `failed_spent`,
`failed_free`, `invalid`), registered beside the grade histogram in
`worker/app/metrics.ts`. This is the operator's
abuse and saturation dial; the spend/free split is what makes a
forced-failure campaign (section 3.2 outcome classes) visible as a
`failed_spent` spike instead of hiding inside a generic failure
count.

### 3.2 Abuse control

This endpoint spends the deploy's shared free-tier key on
unauthenticated internet traffic; abuse control is the make-or-break
of the feature. Layers, outermost first:

**Client IP resolution, proxy-aware and verified.** The app runs
behind an ingress that terminates TLS. The leftmost `X-Forwarded-For`
entry is client-supplied and must never be the limiter key. Header
selection is the router's; the keying is `domain/instant/ip.ts`:

- Reads the header named by `PREP_CLIENT_IP_HEADER` (default
  `x-real-ip`, the ingress-set header on both public deploys).
- `PREP_CLIENT_IP_HEADER=x-forwarded-for-last` selects the LAST
  `X-Forwarded-For` entry (the one appended by the trusted ingress)
  for ingress setups that append rather than overwrite.
- A missing header, or a value that does not parse as an IP address,
  fails CLOSED: the request is keyed to a single shared sentinel
  bucket (`SENTINEL_BUCKET`) subject to the same per-IP windows. It
  NEVER falls back to a socket peer: no trustworthy socket-peer value
  is reachable in any deploy shape, proxied or not, because a
  direct-connecting client sends its own `X-Forwarded-For` too. Dev
  without an ingress lands in the sentinel bucket, a tight shared
  allowance that is fine for manual poking; tests inject the header.
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
| per-anonymous-account daily (`PREP_INSTANT_PER_ANON_USER_PER_DAY`) | 3 | 24h | pending + ok + failed_spent |
| per-signed-in-account daily (`PREP_INSTANT_PER_USER_PER_DAY`) | 20 | 24h | pending + ok + failed_spent |
| global daily breaker (`PREP_INSTANT_GLOBAL_PER_DAY`) | 200 | 24h | pending + ok + failed_spent |

The per-IP windows are the anti-Sybil lever and the per-account ones
are the anti-NAT lever; both must pass. The request that mints an
account has no id at reserve time, so it is admitted on the per-IP
and global windows alone and `resolve` back-stamps the row, which
counts against that account for the rest of the day.

The burst window counts everything, so a hammering client is
throttled regardless of upstream health. Spend failures counting
toward the visitor's daily allowance is a deliberate tradeoff: a
legitimate visitor whose three attempts all die on upstream 5xx
loses the day's allowance through no fault of their own, which is
the price of failing closed; the error copy (section 2) promises a
limit, not three successes.

The global daily breaker protects the free tier's shared daily
token quota for the whole deploy (AI-PROVIDERS.md section 2, the
shared tier): 200
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
`pending` row INSERT happen inside one synchronous transaction in the
`InstantLimiterCell`, so concurrent requests cannot both pass a count
they jointly exceed (the reserved row IS what the next
request's count sees; the gate and its subject come from one read).
The row's `outcome` resolves to `ok` / `failed_spent` /
`failed_free` when the request completes. Rows older than 7 days
are pruned opportunistically on insert.

**Concurrency cap.** A cap on in-flight upstream calls; contention
beyond it returns `busy` immediately rather than queueing (the
reserved row resolves `failed_free`: no spend happened). Two
concurrent generations bound the
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

### 3.3 Client storage model, 3.4 the adoption state machine

Retired with the surfaces above. A generated deck is server rows from
the moment it exists; the browser's only new state is the identity
cookie. The offline stores keep exactly the shape OFFLINE.md
describes, and an anonymous device is a normal owner device whose
owner id starts with `anon:`. See
[ANONYMOUS-ACCOUNTS.md](ANONYMOUS-ACCOUNTS.md) sections 3, 5 and 8;
the one-time wipe of devices still holding the old rows is section 9.


### 3.5 Privacy disclosure

Anonymous prompts go to the deploy's shared free inference endpoint,
a third-party service (AI-PROVIDERS.md section 6). The landing
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

## 4. What changes per file, 5. the test plan, 6. the milestones

Retired with the surfaces above. All three described the client-side
plan file by file: the device-local deck, its nudges, the adoption
dialog, the gates that fed it, and the milestones that shipped them.
Every symbol they named is now gone from the tree, so the per-file
tables no longer describe anything a reader can find, and a change
list naming deleted machinery is how it gets reintroduced.

The live parts of this flow keep their own sections: the endpoint is
3.1, the abuse controls are 3.2, the disclosure line is 3.5, and the
measurements that set the caps are section 1.

What replaced the rest is specified in
[ANONYMOUS-ACCOUNTS.md](ANONYMOUS-ACCOUNTS.md): a generation mints a
server-side anonymous account, the deck lives in that account's cell,
and signing in merges it into the real one.

---

## 7. Follow-ups (explicitly not this spec)

- The signed-in flow frictions from the baseline audit (the kind
  fork, name-before-description, plan-approval fold): a separate
  milestone with its own spec, after instant start proves the
  landing conversion.
- CAPTCHA escalation (section 3.2) if the metrics demand it.
- A `retry_after_s`-aware auto-retry on the generating screen.
- Redaction of the shared-tier key in log output.
