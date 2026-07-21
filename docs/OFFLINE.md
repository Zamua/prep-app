# Offline study

Design spec for offline support: cold-launching the installed PWA
with no network into a usable study surface, studying due cards for
multiple days without connectivity, authoring new cards by hand while
offline, and syncing everything back through the real scheduler when
connectivity returns.

Companion to [architecture.md](architecture.md). This doc describes a
feature that deliberately bends one of prep's rules (server-rendered
HTML, JS as sprinkles) in one tightly scoped place, and explains
exactly where the bend starts and stops.

---

## 1. Goal, user story, non-goals

**User story.** A user is about to spend several days somewhere with
no connectivity. Before leaving (or while away) they build up a bank
of cards by hand and study daily. Cards studied on day 1 come back on
day 3. When they're back online, their reviews and new cards sync up,
and the server's scheduler takes back over as if it had been watching
the whole time.

**Goals**

- Cold-launch the installed PWA with no network and land in a working
  offline study surface. Not an error page, not a sign-in spinner.
- Study due cards offline: deterministic grading where the card type
  allows it, reveal-and-self-verdict everywhere else.
- Author new cards offline (front/back, optional deck assignment).
- A local re-surfacing schedule so multi-day offline study is real
  spaced repetition, not a one-shot cram queue.
- Sync on reconnect: queued reviews replay through the real FSRS
  scheduler server-side, new cards go through the existing validation
  path. The server stays the source of truth.

**Non-goals (v1)**

- AI anything offline: no generation, no LLM grading, no transforms.
- Notifications / web push offline.
- Deck import/export offline.
- Trivia decks. Offline covers SRS decks only.
- Account management offline (sign-in, BYOK keys, settings).
- Offline as a second full app. The online app stays server-rendered;
  the offline surface is a small self-contained companion, described
  below.

---

## 2. UX

### Entry points

There are three ways into the offline surface:

1. **Offline cold launch.** The user opens the installed PWA (or a
   tab on the app origin) with no network. The service worker's
   navigation fallback serves the cached offline app. No sign-in
   screen, no spinner: the user lands directly on their due-card
   queue.
2. **"Study offline" while online.** The offline app lives at a real
   route (`/offline`), linked from the user panel in the masthead.
   Visiting it online renders the same client-side app against the
   same local snapshot. This is also how a user "preflights" before a
   trip: open it once, confirm the card bank is present.
3. **The authoring form.** Inside the offline app, an "Add a card"
   action opens a minimal front/back form with an optional deck
   picker (decks come from the local snapshot). Saved cards appear in
   the local queue immediately as due, mirroring the online
   behavior of `/deck/{name}/question/new` ("shows up as due
   immediately").

### The offline study flow, per card type

The offline queue is: local cards and snapshot cards whose effective
due time is in the past, oldest-due first. One card at a time, same
one-card-per-screen rhythm as the online session flow.

| Type | Offline flow |
| --- | --- |
| `mcq` | Choices render, user picks one, graded locally (exact match against the answer, mirroring `prep/domain/grading.py`). Verdict + correct answer shown. |
| `multi` | Checkboxes render, graded locally by set equality, same as the online deterministic grader. |
| `short` with `answer_regex` | User types an answer; the stored regex is applied (case-insensitive, whole-string, same semantics as `match_regex`). If the pattern is absent, fails to compile in the browser's regex engine, or is over the length cap, fall through to self-verdict. |
| `short` without a usable regex | Reveal flow: user answers, taps "Show answer", the canonical answer (and rubric, if present) renders, user self-verdicts Right / Wrong. Same shape as the existing no-agent `self_grade.html` path. |
| `code` | Always the reveal + self-verdict flow. The rubric and reference answer render on reveal. No editor niceties in v1: a plain textarea. |
| Locally authored cards | Treated as `short` without a regex: reveal + self-verdict. |

"I don't know" is available on every card and records a wrong verdict
with an empty answer, matching the online `idk` path.

Every grade writes two things locally: a queued review (for sync) and
an updated local schedule (for re-surfacing, section 5). The verdict
screen shows the local "next up in N" line so the mechanics feel like
the online app, with a small "offline schedule" qualifier so the user
knows the interval is provisional.

When the queue is empty, the surface says so and shows when the next
card comes due locally, plus the Add-a-card action.

### Reconnect and sync

Sync is driven from the **online** app, not from the offline shell
(the shell never loads the identity provider's JS, so it can never
mint fresh credentials; see section 3, identity).

- While in the offline app, when connectivity returns (an `online`
  event plus a successful probe request), a banner appears: "Back
  online. Return to prep to sync." Tapping it navigates to `/`, which
  now reaches the server and renders the real app (via the reauth
  shell if the session needs re-minting, exactly the existing flow).
- On any authenticated page load, the sync module in `app.js` checks
  the outbox in IndexedDB. If anything is queued, it POSTs the batch
  to the sync endpoint (section 4) with session cookies, then
  refreshes the local snapshot so local SRS state converges to the
  server's FSRS truth.
- The user sees a small toast: "Synced N reviews, M new cards." Items
  the server permanently rejected (validation failures) are kept in a
  "needs attention" list inside the offline app rather than silently
  dropped.

Sync also runs opportunistically in the background on every
authenticated page load, so a user who never opens the offline app
after a trip still syncs by just using prep normally.

---

## 3. Architecture

### The offline companion app

The online app stays server-rendered; that philosophy is not up for
renegotiation here. Offline mode is a **self-contained client-rendered
mini-app**: one page, its own JS modules, the shared stylesheet.

- **Route**: `GET /offline`, registered alongside the PWA routes in
  `prep/web/pwa.py`. Un-auth-gated, like `/manifest.json` and
  `/sw.js`, for the same reason: it must be reachable and cacheable
  without a live session, and it renders nothing user-specific
  server-side. All data comes from IndexedDB client-side.
- **Template**: `templates/offline.html`. It does NOT extend
  `base.html`. Base pulls in the identity provider's CDN script, the
  web-font stylesheet, htmx, and the user-chip masthead, all of which
  either require network or a server-resolved user. The offline
  template is a standalone document that:
  - links the shared stylesheet at its versioned URL
    (`/static/css/v<build>/index.css`, where the token is the
    requested `?build=` token when the SW fetched the shell, else
    the current one; see the shell-token construction in the SW
    section); font-family stacks already fall back to system fonts,
    so the missing web font degrades gracefully offline,
  - declares its own importmap for `@/` at the same versioned JS
    prefix,
  - loads a single `offline-app.js` bootstrap module.
- **JS modules**, under `static/js/offline/`:
  - `offline-app.js`: bootstrap, view switching (queue / card /
    verdict / author / empty), reconnect banner.
  - `store.js`: the IndexedDB layer (schema below), the only module
    that touches IDB.
  - `scheduler.js`: the local ladder (section 5). Pure functions,
    no I/O, mirroring the shape discipline of `prep/domain/`.
  - `grader.js`: the deterministic grader port (mcq exact-match,
    multi set-equality, regex short-answer). Pure functions.
  - `sync.js`: outbox flush + snapshot refresh. This module is also
    imported by `app.js` in the online app; it is the one shared
    seam between the two surfaces.
- Rendering is plain DOM templating inside these modules. No
  framework. The whole app should stay in the low hundreds of lines;
  if it wants a framework, it has grown past its charter.

### Service worker changes

`static/sw.js` today handles `push` and `notificationclick` only.
It gains exactly three things: a precache, a navigation fallback, and
cache-first serving of precached URLs. Nothing else. No runtime
caching of app pages, no request interception for normal online use.

**Registration.** Today the SW is only registered from
`notify-settings.js` when a user subscribes to push, so an installed
PWA that never touched notifications has no SW at all. Registration
moves to `app.js` (always-on, idempotent, same scope-relative
`/sw.js` URL); `notify-settings.js` keeps its `navigator.serviceWorker.ready`
usage and drops the registration call.

**The build token must be build-stable.** Today's asset versioning
uses a boot-stamped token (`_STATIC_BUILD_VERSION` in
`prep/web/templates.py` is the process start time, surfaced to
templates as the css/importmap version). That is a per-process
value, not a build identity. It was harmless when the token only
busted HTTP caches; it is disqualifying once a service worker hangs
on it. Every pod restart with the same image (a Recreate deploy, an
OOM kill on the 2GB nodes, a node reboot) would mint a new token,
byte-change `/sw.js`, and push every client through a full SW
reinstall plus re-download of the entire CSS tree, JS, and icons for
bytes that did not change; and with more than one replica the token
would differ per pod, so `/sw.js` would flip-flop between tokens on
every update check. Offline support therefore replaces the boot
stamp with a deterministic build id: `PREP_BUILD_ID` (git SHA or
image tag) baked in at image build time, with a fallback of hashing
the static tree plus the offline template at boot when the env var
is absent (dev). Identical bytes produce an identical token across
restarts and replicas; the SW updates when and only when a new build
ships. The versioned asset routes in `prep/app.py` must accept the
new token format; they continue to treat the version segment as an
opaque token and serve the current build's bytes for any value
(which is what lets a page from the previous build keep resolving
assets across a deploy), but offline consistency never leans on that
behavior; it comes from the shell-token construction below.

**Getting the build token into the SW.** The `/sw.js` route in
`prep/web/pwa.py` changes from a plain `FileResponse` to a rendered
response: it reads `static/sw.js`, substitutes two placeholders, and
serves the result with `Cache-Control: no-cache`:

- `__BUILD__`: the current build token.
- `__PRECACHE__`: a JSON array of scope-relative URLs, enumerated
  server-side at request time: the offline shell at
  `/offline?build=<token>` (the query parameter is load-bearing; see
  the shell-token construction below), every file under
  `static/css/` at its `/static/css/v<build>/...` URL (the entry
  stylesheet `@import`s the whole component tree, so the entire tree
  must be cached, and the server is the only party that knows the
  file list), every module under `static/js/offline/` AND
  `static/js/modules/` (both directories wholesale, so a new import
  inside a shared module can never silently fall outside the
  manifest), and the PWA icons.

A new build changes the token, which changes the served `/sw.js`
byte-for-byte, which is exactly the browser's trigger to install the
new SW version. A restart of the same build changes nothing and
triggers nothing.

**Precache (install).** The install handler opens a cache named
`prep-offline-v<BUILD>` and fetches every precache URL with
`cache: "reload"` (bypassing the HTTP cache so a deploy can never
precache stale bytes). Before anything is stored, every response is
checked: `response.ok` must be true and `response.redirected` must
be false; any failing response rejects the entire install. The check
is load-bearing: `cache.put` happily stores a transient 502 from the
ingress or a redirect, and a fetch that resolves with a 5xx still
counts as a "successful" fetch. Since the SW only reinstalls when
`/sw.js` changes, a poisoned shell or stylesheet would keep serving
an error page on every offline cold launch until the next build
shipped. (`cache.addAll` rejects on non-OK responses for exactly
this reason; the hand-rolled fetch-plus-put exists only to get
`cache: "reload"` semantics, so it must replicate that protection.)
A rejected install leaves the old SW, with its complete old cache,
active.

**The shell is fetched as `/offline?build=<token>`, never bare
`/offline`.** A deploy can land between the browser fetching
`/sw.js` and the install handler fetching the precache URLs. Fetched
as bare `/offline`, the shell would be rendered by the NEW process
with the NEW build's asset URLs, while the install stores everything
under the OLD `v<token>` keys: a complete-looking cache whose shell
references URLs that are not in it. Offline cold launch would render
an unstyled shell with dead JS, precisely for the user who
preflighted at the last online moment. So the SW asks for the shell
with its own token in the query string, and the `/offline` route
echoes THAT token into the shell's stylesheet URL and importmap
prefix (the token is validated against the token charset before
being echoed, never reflected raw). Shell and precache keys are
consistent by construction: every URL the cached shell references is
a URL the same install stored. If a deploy does race the install,
the versioned asset routes serve the new build's bytes under the old
keys, so the worst case is a mixed-build cache that still works, for
one cycle; the next online navigation sees the new `/sw.js` and
replaces the cache wholesale.

Two interactions with existing middleware, both deliberate:

- The no-cache middleware in `prep/app.py` stamps
  `no-cache, no-store, must-revalidate` on all `text/html`, which
  includes the `/offline` shell. That header governs the HTTP cache
  and has **no effect on the Cache Storage API**: `cache.put` stores
  whatever we put, and `caches.match` returns it without consulting
  response cache headers. Explicitly putting the shell in the SW
  cache is precisely how an aggressively-no-cached HTML page becomes
  available offline, and it is the only way the shell gets in (the
  HTTP cache will never hold it).
- The versioned asset routes already serve
  `Cache-Control: immutable`; the SW ignores that too and stores its
  own copy, because the HTTP cache is allowed to evict at any time
  and offline cold launch cannot depend on it.

**Update lifecycle.** The SW keeps its existing `skipWaiting()` +
`clients.claim()`. The flow after a deploy: the next online
navigation triggers the browser's SW update check, the new `/sw.js`
bytes differ (new build, new token), install runs (precaching the
new build's URLs into a new cache), the new SW activates
immediately, and activate deletes every `prep-offline-*` cache whose
build doesn't match. The page the user is currently looking at keeps
working (its versioned asset URLs still resolve from the network);
the next navigation is fully on the new build. This preserves the
update-on-reload behavior the versioned URL scheme was built for: an
installed PWA can never be stranded on a stale bundle, and the
offline cache is internally consistent because the shell-token
construction above makes it so: the cached shell references exactly
the URLs the same install stored.

If the install-time precache fails partway (user goes offline mid
update, or any precache response comes back non-OK), the install
promise rejects, the browser discards the new SW, and the old one,
with its complete old cache, stays active. All-or-nothing is
enforced by two things together: `install` waits on all puts, and
the response checks above turn server-side failures into install
failures instead of cached garbage.

**Navigation fallback (fetch).** The fetch handler:

- Only intercepts `GET` requests. For `request.mode === "navigate"`
  within scope: first check whether the cache holds the shell. If it
  does not, pass the request through untouched (no timeout, no
  interception effects: an uncached browser on a slow network sees
  exactly today's behavior). If it does, race `fetch(request)`
  against `AbortSignal.timeout(4000)` and respond with the cached
  shell (matched at its `/offline?build=__BUILD__` key) when the
  fetch **rejects or times out**. The timeout matters because
  airplane mode is the easy case: it rejects instantly. The
  realistic remote-travel network is timeout-shaped (one-bar
  cellular, DNS blackholes), where `fetch` can hang for the
  browser's full network timeout, tens of seconds, before rejecting;
  a feature whose premise is bad connectivity cannot handle only the
  clean failure. Any response the server does produce within the
  window passes through untouched, including redirects, 4xx and 5xx:
  an error page from a reachable server is information, and
  swallowing it behind the offline app would mask real outages. A
  server that cannot produce a response within the window is treated
  as unreachable, which is the honest reading from a field with one
  bar.
- For requests that match a precached URL when the `v<token>`
  segment is disregarded (the shell's CSS/JS/icons): cache-first on
  exact URL match, network on miss. If the network then fails too,
  serve the normalized cache match, so a page rendered online at a
  newer build that loses connectivity mid-session still resolves its
  subresources from the cache this device has, instead of failing
  outright. The normalized fallback runs only after a network
  failure; online, exact matching keeps every page byte-correct (a
  new build's URLs are never answered with an older build's bytes
  while the network can supply the real ones).
- Everything else: not intercepted (no `respondWith`), so the online
  app's behavior is byte-identical to today.

**Cold launch, precisely.** `start_url` is `/`. Offline, the
navigation fetch to `/` rejects at the network layer (or hangs past
the 4s window) and the SW serves the cached `/offline` shell. The
server-side branching in `prep/web/index.py` (landing page for
anonymous visitors, the reauth shell for dormant sessions, the
dashboard for live ones) never enters the picture offline because
all three are server renders: no response, no branch. Conversely,
when the server is reachable and responding within the window, the
SW never substitutes the offline app, so the reauth shell's recovery
dance and its fallback-to-landing escape hatch work exactly as they
do today. The degraded middle case (server reachable but the
identity provider's CDN is not) stays on the existing reauth
fallback path; the user can still reach `/offline` by hand from the
landing page, which gets a small "study offline" footer link when a
snapshot exists.

### IndexedDB schema

One database, `prep-offline`, schema version 1, owned by `store.js`.
All object stores are logically namespaced by the owner user id
(section on identity below); in practice the whole database belongs
to one user at a time and is wiped on owner change, which is simpler
and safer than per-store composite keys.

| Store | Key | Contents |
| --- | --- | --- |
| `meta` | name (string) | `owner` = `{user_id, display_name, snapshot_at, build}`; `device` = `{device_id}` (UUID minted on first open). |
| `decks` | `id` | Snapshot of the user's SRS decks: `{id, name, display_name}`. |
| `cards` | `question_id` | Snapshot of every non-suspended question in an SRS deck: `{question_id, deck_id, type, prompt, choices, answer, answer_regex, rubric, skeleton, step, next_due}` plus local overlay fields `{local_step, local_next_due}` (null until studied offline). |
| `local_cards` | `client_id` (UUIDv4) | Cards authored offline: `{client_id, deck_id (nullable), prompt, answer, created_at, local_step, local_next_due}`. |
| `outbox_reviews` | `client_id` (UUIDv4) | Queued reviews: `{client_id, question_id OR card_client_id, verdict, user_answer, graded_by ("auto" or "self"), reviewed_at}`. A row's presence IS its status (queued); acked rows are deleted and rejected ones move to `rejects`. Index on `reviewed_at`. |
| `rejects` | `client_id` | Items the server permanently rejected, with the error, for the "needs attention" list. |

**Snapshot refresh.** While online, `sync.js` (running inside
`app.js` on authenticated pages) refreshes the snapshot from
`GET /api/offline/snapshot` (contract in section 4): on dashboard
loads. The hourly throttle applies only to devices with no owner
snapshot yet; once a snapshot exists the refresh runs on every
online page load, because the same request doubles as the
owner-mismatch check (a different signed-in account must be
discovered on load, not up to an hour later). It always runs
immediately after a successful outbox flush. Decks and cards are text and small; the
snapshot is a full replace of the `decks` and `cards` stores (with
local overlay fields for cards that still have queued reviews
preserved). A full replace sidesteps tombstone bookkeeping for
deleted cards and decks.

**Storage persistence and eviction margin.** On the first successful
snapshot write, `sync.js` calls `navigator.storage.persist()` (and
surfaces `navigator.storage.estimate()` in the offline app's footer
for debugging). Platform reality this design leans on:

- The 7-day script-writable-storage cap applies to Safari
  **browsing-context** usage: a site not visited in Safari for 7 days
  of Safari use can have its IndexedDB wiped.
- A web app **installed to the home screen** keeps its website data
  separately and is exempt from that cap; using the installed app
  counts as its own use.

So the design margin for multi-day offline is: the installed PWA is
the supported offline vehicle, and using it daily (which is the whole
point of the feature) keeps it maximally safe. The offline app
detects a plain-Safari-tab context (`display-mode` not `standalone`)
and shows a one-line nudge that the installed app is the reliable
home for offline data. Eviction is additionally survivable by design:
the snapshot is disposable (re-fetched anytime online) and the outbox
is the only real loss surface, which `persist()` plus installed-PWA
storage keeps as small as the platform allows. Zero-data cold launch
(evicted or never-seeded) renders an honest "nothing cached on this
device yet; open prep online once" screen.

### Identity offline

The offline shell must work with no network, which means **no
identity provider JS** (its CDN is unreachable) and no server-side
session. Identity offline is a local snapshot, and real
authentication happens only at sync time:

- The `meta.owner` record is written by `sync.js` whenever an
  authenticated page loads: the server includes the resolved
  `{user_id, display_name}` in the snapshot payload. The offline app
  reads it purely for display ("Studying as &lt;name&gt;") and for
  stamping ownership.
- All local data implicitly belongs to `meta.owner.user_id`. The
  sync endpoint ignores any client claim of identity: the
  authenticated Clerk session on the sync POST is the identity, full
  stop.
- **Different-user sign-in on the same device**: on every
  authenticated page load, `sync.js` compares the server-resolved
  user id with `meta.owner.user_id`. On mismatch it does NOT sync:
  the outbox belongs to a different account and replaying it would
  cross-pollinate data. This comparison is the load-bearing guard
  and ships in the same milestone as the first outbox flush
  (section 8, M3); it is a few lines in `sync.js`, not hardening.
  Until the wipe flow below exists, mismatch simply disables
  `sync.js` for the session: no flush, no snapshot write, so a
  mismatched sign-in can neither absorb the other account's outbox
  nor overwrite its local data. v1 behavior is wipe-and-reseed: clear every
  store, write the new owner, fetch a fresh snapshot. If the old
  outbox is non-empty, the wipe is preceded by a confirm dialog
  ("This device has N unsynced reviews from another account; they
  will be discarded"), so data loss is explicit, never silent.
  Preserving multi-account outboxes side by side is out of scope for
  v1.
- The offline surface never shows another user's data because the
  database only ever holds one owner's data.

---

## 4. Sync protocol

Two JSON endpoints, both authenticated by the standard `current_user`
dependency (session cookies; a fresh short-lived token is minted by
the identity provider's script on the page doing the sync, which is
why sync only runs from the online app). Both live in a new
`prep/offline/` bounded context (`routes.py`, `service.py`,
`repo.py`), following the existing per-context layout.

### `GET /api/offline/snapshot`

Response:

```json
{
  "user": {"id": "user_abc", "display_name": "Ada"},
  "generated_at": "2030-01-01T12:00:00+00:00",
  "decks": [
    {"id": 3, "name": "capitals", "display_name": "Capitals"}
  ],
  "cards": [
    {
      "question_id": 123, "deck_id": 3, "type": "mcq",
      "prompt": "...", "choices": ["a", "b"], "answer": "a",
      "answer_regex": null, "rubric": null, "skeleton": null,
      "step": 2, "next_due": "2030-01-03T09:00:00+00:00"
    }
  ]
}
```

Scope: SRS decks only, non-suspended questions only, every card (not
just currently-due ones: multi-day offline needs the cards that
become due later in the window, and the whole payload is small text).
`step` is the existing 0 to 5 maturity bucket
(`step_for_stability`), which doubles as the seed for the local
ladder. All reads filter by the authenticated user id, same IDOR
discipline as every repo accessor.

### `POST /api/offline/sync`

Request:

```json
{
  "device_id": "d5b0…",
  "new_cards": [
    {
      "client_id": "9f2a…", "deck_id": 3,
      "prompt": "front text", "answer": "back text",
      "created_at": "2030-01-02T08:00:00+00:00"
    }
  ],
  "reviews": [
    {
      "client_id": "77c1…", "question_id": 123,
      "verdict": "right", "user_answer": "a",
      "graded_by": "auto",
      "reviewed_at": "2030-01-02T08:05:00+00:00"
    },
    {
      "client_id": "8d40…", "card_client_id": "9f2a…",
      "verdict": "wrong", "user_answer": "",
      "graded_by": "self",
      "reviewed_at": "2030-01-03T09:00:00+00:00"
    }
  ]
}
```

Response:

```json
{
  "cards": [
    {"client_id": "9f2a…", "status": "created", "question_id": 987}
  ],
  "reviews": [
    {"client_id": "77c1…", "status": "applied"},
    {"client_id": "8d40…", "status": "applied"},
    {"client_id": "0000…", "status": "rejected", "error": "unknown question_id"}
  ]
}
```

Caps: 100 cards and 500 reviews per request. The client chunks under
a strict cross-chunk ordering rule: every `new_cards` chunk is
flushed and acked before the first `reviews` chunk is sent, and
`reviews` chunks are sent in `reviewed_at` order. The rule exists
because cards-before-reviews is guaranteed by the server only WITHIN
a request; without it, a review whose `card_client_id` sat in a
later chunk would arrive before its card and be rejected as unknown,
and the server cannot distinguish "card not yet synced" from "card
never existed". A `deck_id` of null files the card into a
get-or-created deck named `inbox` (the existing
`DeckRepo.get_or_create` semantics).

**Processing order and semantics** (per request, all under the
authenticated user):

1. **Cards first.** Each new card goes through the existing
   validation shape (`NewQuestion` with `type="short"`, prompt and
   answer required) and the existing add path, so length limits and
   deck ownership checks are the same ones the online form gets. The
   created question starts due immediately (matching online manual
   authoring), before any of its queued reviews apply.
2. **Reviews second, in `reviewed_at` order** across the whole
   batch. Reviews referencing `card_client_id` resolve through the
   just-created (or previously-synced) card's idempotency mapping.
3. Each applied review runs the REAL scheduler:
   `schedule_review(state, verdict, now=reviewed_at)`. The pure FSRS
   entry point already accepts an explicit `now`, so replay is
   first-class, not simulated. A review row is written to the
   append-only `reviews` log with the client's timestamp and a
   grader-notes marker (`(offline auto)` / `(offline self-graded)`).

**Idempotency.** Every item carries a client-generated UUID. A new
`offline_sync_idempotency` table maps
`(user_id, client_id) -> outcome` (created question id, or applied /
logged_no_reschedule status), written in the same transaction as the
item's effect. Rejections are deliberately not pinned: a rejected
item has no side effects, and a retried rejection is re-validated
from scratch, so it may legitimately succeed on a later flush (the
interrupted-flush recovery below depends on exactly that). A retried
batch (network flap mid-response, double-tap, crashed tab) replays
committed items as pure lookups: same response, no duplicate
review rows, no double-advanced SRS state, no duplicate cards. This
is the same shield the grading workflow already uses
(`grading_idempotency`), extended to sync. Items are processed in
per-item transactions (savepoints), so one rejected card cannot fail
the batch; the response reports each item's fate and the client
prunes its outbox accordingly: `created`, `applied`, and
`logged_no_reschedule` are removed, `rejected` moves to the local
`rejects` store, and transient server errors (5xx, network) leave
the item queued for the next flush. The status vocabulary is exactly
those four; there is no "duplicate" status, because a retried item
replays as a lookup and comes back with its original status. One
exception to the pruning rule: a review `rejected` for an unknown
`card_client_id` while its card still sits queued in `local_cards`
stays in the outbox instead of moving to `rejects`; that rejection
means the card has not been created yet (an interrupted flush), and
the next flush, which always sends cards first, resolves it.

**Conflict handling (multi-device).** The server compares each
review's `reviewed_at` against the card's `last_review`:

- `reviewed_at > last_review`: apply through the scheduler (the
  normal case).
- `reviewed_at <= last_review`: another device already recorded a
  later review while this one was offline. The review row is still
  written to the audit log (the study effort is real history) but
  the scheduler is NOT run for it; status comes back as
  `logged_no_reschedule`. Net effect: timestamp-ordered replay with
  last-writer-wins on card state, and no FSRS calls with negative
  elapsed time.

**Clock skew.** `reviewed_at` timestamps in the future relative to
the server are clamped to server-now before ordering and replay
(logged with the original value in grader notes). Timestamps are
required to be ISO-8601 with offsets; naive timestamps are rejected
per-item. A device with a slow clock simply produces slightly
conservative FSRS intervals; day-granular scheduling absorbs it.

**After a successful flush** the client immediately refreshes the
snapshot, clears the local overlay fields (`local_step`,
`local_next_due`) for synced cards, and deletes acked outbox rows.
From that moment the device is back on pure FSRS truth.

---

## 5. Offline scheduling: the local ladder

Offline devices cannot run FSRS (the scheduler needs the full
per-card float state and the upstream library; porting it would
create a second source of scheduling truth to keep honest). Instead
the offline app uses a deliberately simple ladder, the same shape
prep used before FSRS:

```
step:      0     1    2    3    4     5
interval:  10m   1d   3d   7d   14d   30d
```

- Each snapshot card seeds `local_step` from the server-computed
  `step` bucket on first offline review (the bucket exists precisely
  as the coarse maturity view of FSRS stability).
- Right: `local_step = min(step + 1, 5)`,
  `local_next_due = now + interval[local_step]`.
- Wrong: `local_step = 0`, `local_next_due = now + 10m`.
- A card's effective due time offline is `local_next_due` when set,
  else the snapshot's `next_due`.
- Locally authored cards start at `local_step = 0`, due immediately.

`scheduler.js` implements this as pure functions with the table as a
constant, so it is unit-testable in isolation, mirroring the
discipline of `prep/domain/srs.py`.

**Worked example (the day-1 to day-3 story).** A fresh card authored
offline on day 1: answered right (step 0 to 1, due +1d), answered
right again on day 2 (step 1 to 2, due +3d), so it returns on day 5;
answered wrong on day 2 instead, it returns within the same study
sitting (+10m) and then climbs again. A mature snapshot card at
bucket 4 answered right offline lands at step 5, +30d, and won't
reappear during any plausible offline window, which matches intent.

**Acceptable drift.** The ladder is coarser than FSRS in both
directions and that is accepted:

- Ladder-sooner-than-FSRS (common for mature cards): the user sees a
  card offline earlier than FSRS would have shown it. Cost: a few
  extra reviews. Retention is never harmed by an extra review.
- Ladder-later-than-FSRS (possible for lapsed cards FSRS would
  hammer): the user sees a card less often for a few days. FSRS
  learns from the actual reviewed_at gaps at replay time, so the
  post-sync state accounts for what really happened.

The invariant that matters: local scheduling only ever decides what
to show while offline. It never writes server state. At sync, the
server recomputes truth from the review log through FSRS, and the
snapshot refresh discards every local interval. Drift has a bounded
lifetime of exactly one offline period.

---

## 6. Failure modes and edge cases

| Scenario | Behavior |
| --- | --- |
| Cold launch offline, nothing cached | SW has no cache (never installed / evicted): the navigation fails as it does today. With a cache but empty IDB: the shell renders the "nothing cached on this device yet" screen. |
| Cold launch on a hanging network (one-bar cellular, DNS blackhole) | The navigation fetch races a 4s timeout; on timeout the cached shell is served, same as a hard reject. With no cached shell the request passes through with no timeout, so uncached slow-online users see today's behavior. |
| Storage evicted between sessions | Snapshot is disposable; re-seeded on next online visit. Outbox loss is mitigated (installed-PWA storage, `persist()`) but not impossible; the app shows outbox size in its footer so heavy offline users can see what is at stake, and syncing promptly on reconnect keeps the window small. |
| Partial sync (network flap mid-batch) | Client retries the whole batch; the idempotency table makes retries read-only replays. Per-item statuses mean a poisoned item cannot wedge the queue. |
| Different user signs in on the device | Sync refuses to run against a mismatched owner; explicit confirm-then-wipe flow (section 3). No silent cross-account writes, no silent data loss. |
| Same user, second device studied online meanwhile | Timestamp-ordered replay with last-writer-wins card state; superseded reviews land in the audit log as `logged_no_reschedule`. |
| Device clock ahead of server | Future `reviewed_at` clamped to server-now at sync; ordering preserved. |
| Deploy while a user is offline | Old SW and old cache keep serving the old shell + assets (internally consistent build). The update lands on the next online navigation. |
| Snapshot card deleted server-side while its review was queued | The review's `question_id` no longer resolves for this user: `rejected` with `unknown question_id`, surfaced in the needs-attention list. |
| Regex differences between engines | The stored `answer_regex` was authored for the server's regex engine; the offline grader compiles it defensively (try/catch, length cap, anchored whole-string match, case-insensitive + dot-all flags) and any compile failure falls through to self-verdict. A locally "auto" verdict from a regex is replayed server-side as a verdict, not re-graded. Compile-failure divergence therefore only changes which grading UI the user saw. Patterns that compile in BOTH engines with different semantics (the shorthand classes: JS \w \d \b are ASCII-only even under the u flag, Python's are Unicode) could misgrade, so the offline grader refuses to auto-grade when a shorthand class meets non-ASCII content on either side and falls to self-verdict instead; the divergence is pinned in the parity fixtures. |
| Offline app opened online with a stale build | The shell is served by the SW only when the network fails; online, `/offline` comes from the server at the current build. |

---

## 7. Testing strategy

Following the existing pyramid (`make lint` / `make test` /
`make e2e`):

**Unit (pytest, in `tests/offline/`)**

- Sync service: replay ordering across interleaved cards + reviews,
  last-writer conflict rule, clock clamping, idempotent re-POST of
  the same batch produces identical responses and zero new rows
  (assert on `reviews` count and card FSRS state).
- Snapshot repo: SRS-only, non-suspended-only, user-scoped (IDOR
  test: user B's ids invisible to user A, same shape as existing
  repo tests).
- Replay math: feeding N reviews with explicit timestamps through
  the sync service equals calling `schedule_review` N times directly
  with those timestamps (the service adds no scheduling logic of its
  own).

**Route tests (pytest, same directory)**

- `/offline` renders without auth, contains no identity-provider or
  external-CDN references (regression-pin the self-containment).
- `/sw.js` response carries the build token, valid JSON precache
  list, `no-cache` header; every URL in the precache list actually
  resolves 200 via the TestClient (the check that catches a renamed
  CSS file breaking offline silently).
- The reverse completeness check: statically parse the import
  specifiers reachable from `offline-app.js` (transitively, through
  every module it pulls in, resolving the `@/` importmap prefix) and
  assert every resolved URL appears in the precache list. The
  200-resolution test validates only URLs that are already listed;
  this one catches the import added to a shared module later that
  would break offline cold launch (the module fetch rejects offline)
  while staying green online and in every other test. Precaching
  `static/js/offline/` and `static/js/modules/` wholesale makes that
  drift unlikely; this test makes it loud.
- `/offline` echoes a valid `?build=` token into the stylesheet URL
  and importmap prefix, and refuses (falls back to the current
  token) when the query value is not token-shaped.
- `/api/offline/sync` and `/snapshot`: auth required, caps enforced,
  per-item validation errors surface as `rejected` not 4xx-on-batch.

**Offline JS logic (browser tests, `tests/e2e/`, marked `browser`)**

- `scheduler.js` and `grader.js` are pure modules: a Playwright page
  imports them and asserts the ladder table, right/wrong
  transitions, and grader parity against fixture cards (the same
  fixtures the Python grader tests use, exported as JSON, so the two
  implementations are pinned to each other).

**End-to-end offline (browser tests)**

The auth mechanism has to be chosen deliberately, because the
existing browser harness cannot sign in to staging: the `page`
fixture in `tests/e2e/conftest.py` authenticates by injecting the
`Tailscale-User-Login` header (and its default base URL is the
retired tailnet deploy), while staging runs `PREP_AUTH_MODE=clerk`,
which that header does nothing for. The offline e2e suite therefore
runs against a **tailscale-mode deploy of the same build**, using
the existing header-injection fixture unchanged. Everything this
suite exercises (SW install and precache, IndexedDB, the ladder,
grading, the outbox flush) is auth-provider independent, so the
coverage transfers. The Clerk-specific interplay (the reauth shell
versus the offline fallback, standalone-PWA session state) is
covered by the iOS-Simulator manual gate below, which runs against
Clerk staging. If a scripted Clerk path is wanted later, Clerk
testing tokens with a fixed-OTP test user are the mechanism; that is
a follow-up, not a gate.

- Prime: sign in, load dashboard (seeds snapshot + SW), then
  `context.set_offline(True)`, navigate to start_url, assert the
  offline app rendered (not an error, not the reauth shell), study
  an mcq and a self-verdict card, author a card, assert queue and
  ladder behavior.
- Reconnect: `set_offline(False)`, navigate home, assert the outbox
  flushes (poll the API for the review rows), assert snapshot
  refresh cleared local overlays.
- Airplane-mode cold launch on real WebKit (iOS Simulator) is the
  manual verification gate before each milestone ships: install to
  home screen, enable airplane mode, cold launch, study, disable,
  sync. Chromium's offline emulation does not exercise Safari's
  storage or standalone-PWA behavior, so it never counts as the
  final word for this feature.

---

## 8. Phased build plan

Each milestone is independently shippable and goes through the
staging gate before promotion. Order is dependency-driven.

**M1: SW precache + offline shell (read-only).**
The build-stable token (`PREP_BUILD_ID` replacing the boot stamp,
plus the asset routes accepting the new format); unconditional SW
registration in `app.js`; the rendered `/sw.js` route (build token +
precache manifest + the `?build=` echo on `/offline`);
install/activate/fetch handlers with the precache response checks
and the navigation timeout; the `/offline` route + template +
`store.js`; the snapshot endpoint + `sync.js` snapshot refresh +
owner record. Shippable
result: cold-launch offline shows your decks and due cards,
read-only. Push behavior in the SW is untouched (regression-pinned
by existing notify tests).

**M2: sync endpoint, server side.**
The `prep/offline/` context: sync route + service + idempotency
table + FSRS replay + conflict/clock rules, fully covered by unit
and route tests. `sync.js` gains the outbox flush (vacuously empty
until M3). Shippable: the API contract is live and testable end to
end with curl-shaped tests before any offline UI writes to it.

**M3: offline study.**
`scheduler.js` + `grader.js` + the card/verdict views; outbox
writes; local ladder overlays; reconnect banner; toast on flush; and
the owner-mismatch guard in `sync.js` (compare the server-resolved
user id in the snapshot payload against `meta.owner.user_id` before
ANY flush or snapshot write; on mismatch, do nothing). The guard
ships in the same milestone as the first flush, not in hardening,
because a flush without it fails the leaves-prod-no-worse bar on
shared devices: user A studies and authors offline, user B signs in,
and B's session would replay A's outbox (reviews bouncing into
rejects as unknown ids, and once M4 lands, A's authored cards being
created inside B's account). Shippable: the full
study-offline-sync-later loop for existing cards. The e2e offline
suite lands here.

**M4: offline authoring.**
The add-a-card form; `local_cards`; `card_client_id` reviews; the
new-card ingestion path in the sync service (already contract-tested
in M2); the `inbox` deck fallback. Shippable: the complete v1 story.

**M5: hardening.**
The confirm-then-wipe UX for the different-owner case (the
refuse-to-sync guard itself shipped in M3; this milestone adds the
dialog and the wipe-and-reseed so a legitimate second user gets a
working device instead of a silently disabled sync);
`navigator.storage.persist()` + estimate readout; needs-attention
list for rejects; Safari-tab nudge; snapshot throttling tuning; the
landing-page "study offline" link. Shippable: the edges are as
designed rather than accidental.
