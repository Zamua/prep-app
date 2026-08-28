# Anonymous accounts

The design of server-side anonymous identity. A visitor who generates
a deck becomes a real user with a real cell, identified by a cookie,
served by the normal UI. If they later sign in, their anonymous
account merges into the signed-in one.

The rule this document exists to enforce:

> Anonymous users are anonymous, not ephemeral. For an anonymous
> user, a signed-in user, and a signed-in user studying offline, the
> UI is the same UI. The only people who see the splash page are
> visitors who are not signed in AND have never made a deck.

Read [architecture.md](architecture.md) first for the cell model this
rests on. Companions: [OFFLINE.md](OFFLINE.md) (the local-first
machinery this applies to anonymous users too) and
[AI-PROVIDERS.md](AI-PROVIDERS.md) (the shared tier the generation
endpoint spends). [INSTANT-START.md](INSTANT-START.md) is the
first-run flow that mints these accounts; where the two overlap on
identity and storage, this document is the authority.

---

## 1. Identity

### The cookie

| Property | Value |
| --- | --- |
| Name | `prep_anon` |
| Value | `v1.<id>.<iat>.<sig>` |
| `id` | 16 random bytes, base64url, no padding |
| `iat` | issue time, integer unix seconds |
| `sig` | `HMAC-SHA256(secret, "v1.<id>.<iat>")`, first 16 bytes, base64url |
| HttpOnly | yes |
| Secure | yes when `request.url.scheme == "https"`, else omitted |
| SameSite | `Lax` |
| Path | `<root_path>/` |
| Max-Age | 15552000 (180 days) |
| Rolling window | re-minted (new `iat`, new `sig`, SAME `id`) on the first resolve after `iat` is older than 30 days |

Verification: split into 4 parts, recompute the signature, compare
in constant time, reject when `iat` is in the future by more
than a minute or older than 180 days. A cookie that fails ANY of
those is treated as absent (and cleared, below). No exception is
raised: a forged cookie is an unauthenticated request, not a 400.

**The window rolls, and re-issuing Max-Age alone does not roll it.**
`iat` is inside the signed value, so a fresh `Set-Cookie` carrying the
same value has the same `iat` and expires on the same wall-clock day
however often the visitor returns. A daily visitor would be hard
logged out 180 days after their FIRST generation, with a live account
and no way back to it. That contradicts the rule this spec exists to
enforce.

So the resolver re-MINTS. On any resolve that yields an anonymous
user, if `now - iat > 30 days`, build a new value with the same `id`,
the current time as `iat`, and a fresh signature, and set it on the
response. The `id` never changes, so the `users` row, the decks and
every `meta.owner` stamp on that device are untouched: only the
expiry evidence is refreshed.

30 days is a sixth of the window, chosen so a returning visitor
re-mints at most once a month (one HMAC and one `Set-Cookie` header,
not one per request) while still leaving 150 days of slack before the
hard reject. The invariant to preserve if that number changes: the
refresh threshold must be far enough below 180 days that a visitor
who returns at any interval shorter than the gap is never rejected.

Resolve has no response object, which is the same problem stale-cookie
clearing has, and it gets the same answer. `resolve()` sets
`request.state.anon_cookie_refresh` to the new value; the
response wrapper at the composition root emits the
`Set-Cookie`. Both that emit and the stale-cookie `delete_cookie`
must sit OUTSIDE the middleware's existing
`if ct.startswith("text/html")` block, or a JSON response never
refreshes and never clears.

**Signed, not opaque-random.** An opaque 128-bit id in a DB column
would be equally unguessable. Signing buys three things the opaque
form does not: garbage cookies are rejected before touching sqlite
(the endpoint is anonymous and internet-facing, so a cheap reject
path matters); the issue time is trustworthy, so expiry is enforceable
without a read; and rotating the secret invalidates every outstanding
anonymous session at once, which is the only revocation lever this
identity has.

**The secret.** `PREP_ANON_COOKIE_SECRET` (hex, 32 bytes) when set.
Otherwise derived from the existing `PREP_KEY_ENCRYPTION_SECRET`
(`worker/runtime/adapters/byokCrypto.ts`) with
`HKDF-SHA256(ikm=master, info=b"prep-anon-cookie-v1", length=32)`.
Both public deploys already set the master key, so anonymous accounts
need no new deploy config. Never use the master key directly: one key,
one purpose, and the HKDF label is the domain separation.

**No secret at all means anonymous accounts are OFF.** The mint path
refuses, the resolver returns None, `GET /` renders today's landing.
Fail closed. An unsigned cookie is not an acceptable degradation:
it would let anyone name themselves any user id.

`SameSite=Lax` is load-bearing, not a default. Sign-in on the public
deploy is a top-level navigation to Clerk's hosted UI on
`accounts.prepcards.app` and back. `Lax` sends the cookie on that
return navigation; `Strict` would drop it, and the merge (section 5)
would never fire because the request that first carries the Clerk
session would not carry `prep_anon`.

### PWA survival

Same-browser survival is a cookie's normal job and needs nothing
special. The exception is iOS: an installed PWA has a storage jar
separate from Safari (`worker/templates/base.html`). Adding prep to the
home screen therefore yields an empty jar and a fresh visitor, while
the anonymous account and its decks still exist server side and are
unreachable.

Consequence, enforced in section 4: the install nudge stays gated to
non-anonymous users. Its comment already says "once signed in, the
data is server-side and install is safe"; the reason survives the
redesign with one word changed (the deck is server-side either way,
but only a signed-in user can prove who they are from a fresh jar).

A `start_url` carrying a one-shot claim token would fix this. Not in
v1: `manifest.json` is un-auth-gated and cached, and a claim token in
a cached manifest is a session-fixation footgun. Follow-up, section 8.

### Where the branch lives

Three candidate shapes. One wins.

**Chosen: one resolver, above the provider.**
`resolveIdentity` in `worker/app/auth/resolve.ts` asks the configured
`IdentityProvider` first, then consults the cookie only if the answer
was "nobody and not dormant":

1. the provider resolves a session: **signed-in always wins**
2. the provider reports a dormant session: return nobody, so the
   reauth shell can recover it
3. otherwise: verify the `prep_anon` cookie
4. otherwise: a visitor

**The precedence rule lives in that one function**, so no call site has
to remember it, and the entry worker is the only caller.

The dormant step is load-bearing, not decoration. A returning Clerk
user on a PWA cold launch has an expired `__session` JWT and a live
`__client_uat`, so the provider answers "nobody" while reporting a
dormant session. Without step 2 the resolver would fall through to a
`prep_anon` cookie left on that browser and serve a signed-in person
their old anonymous account, and every recovery path keyed on "no user
resolved" would stop firing: the reauth shell would never render, and
a route needing a user would be served as the WRONG user instead of
returning the 401 that triggers a re-handshake.

**Rejected: an anonymous identity provider.** Exactly one provider is
configured. An anonymous-only provider could not also resolve Clerk
sessions, so "signed-in wins" would be unimplementable inside the seam
and would leak back out to the caller.

**Rejected: a branch at each call site.** It duplicates the precedence
rule across every caller and any future resolver, and makes the
`IdentityProvider` seam decorative.

### One narrow addition to the port

`ResolvedUser` (`worker/app/ports.ts`) gains
`is_anonymous: bool = False`. `resolveIdentity`
(`worker/app/auth/resolve.ts`) branches on exactly that:

```
resolved = get_provider().resolve(request)
if resolved is None: return None
if resolved.is_anonymous:
    user = UserRepo().get_by_external_id(resolved.external_id)
    if user is None:
        request.state.anon_cookie_stale = True   # cleared on the way out
        return None
    UserRepo().touch(resolved.external_id)
else:
    user = UserRepo().upsert(...)                # unchanged; CREATES the row
    _try_merge_anon_cookie(request, user)        # section 5; AFTER the upsert
request.state.user = user
```

The anonymous path must NOT go through `upsert`. `upsert` inserts on
miss (`worker/runtime/adapters/sql/prefsRepo.ts`), so a stale cookie naming a reaped
account would silently resurrect it as an empty user forever. Rows
are created at mint time only (section 3).

The merge call sits AFTER `upsert` and takes the upserted row's id.
Ordering is not stylistic. On a fresh sign-up the target `users` row
does not exist until `upsert` creates it, `decks.user_id` has an FK to
`users(tailscale_login)`, and the merge refuses a missing target
(section 5). Calling it before the upsert makes the single most
important path in this spec a silent no-op whenever the Clerk
`user.created` webhook (`worker/runtime/webhooks.ts`) has not
already raced ahead and mirrored the row. That failure is
intermittent by construction and invisible to any test that seeds the
target user first, so a test signs up an id with no pre-existing row
and no webhook.

Clearing a stale cookie needs a response, which `resolve()` does not
have. The existing response wrapper at the composition root
grows a two-branch block, placed BEFORE its `text/html` check so JSON
responses get it too: `request.state.anon_cookie_stale` triggers
`response.delete_cookie` with the same name/path,
`request.state.anon_cookie_refresh` triggers `response.set_cookie`
with the re-minted value and the full attribute set from the table
above. Starlette backs `request.state` with `scope["state"]` and
`BaseHTTPMiddleware` passes the same scope object down, so both flags
survive the middleware boundary.

`worker/app/auth/` (bearer tokens) is untouched. Anonymous users
cannot mint API tokens (section 4), so that surface never sees one.

---

## 2. The user row

### What marks a user anonymous

Both a namespaced id and an explicit column.

- `users.tailscale_login = "anon:" + <32 hex chars>`. The column is
  the universal external id (`worker/app/ports.ts`); the prefix makes
  the row self-describing in logs and cannot collide with a Clerk
  `user_...` id or a Tailscale email.
- `users.is_anonymous INTEGER NOT NULL DEFAULT 0`. This is what code
  branches on. Branching on a string prefix is an implicit contract
  that rots the first time an identity provider emits an id starting
  with `anon:`; the column cannot be spoofed by a provider.

Storage: `is_anonymous` is a column on the user's own `profile` row,
and the `DirectoryCell` carries a copy alongside `created_at` and
`last_seen_at`. The directory's copy is what the reaper walks, and it
is the only reason the flag exists in two places: asking every user
cell would defeat the walk.

The merge audit is `account_merges` in the `DirectoryCell`: one row per
attempted merge of an anonymous account into a provider account.

```
account_merges
  id, anon_user_id, target_user_id
  started_at, completed_at
  status          started | completed | failed
  counts          JSON {table: rows_moved}
  error
```

It is retained rather than pruned. It is the only record that a given
deck used to belong to a different user id, and the offline snapshot
reads the completed rows to tell a device that its old owner id is now
this account (section 5).

### Repo surface

`UserRepo` gains one method and `upsert` is untouched.

- `touch(user_id) -> None`: bare `UPDATE users SET last_seen_at = ?`.
  Keeps the reaper honest without upsert's insert-on-miss.
- `get_by_external_id` (exists, `worker/runtime/adapters/sql/prefsRepo.ts`) is reused
  verbatim: read-only, no side effects, exactly what the resolver
  needs.

There is deliberately no `UserRepo.create_anonymous()`. The insert
lives inside `create_instant_deck` (section 3), because a `users` row
that is not in the same transaction as its first deck is the failure
mode that method exists to prevent. Making the insert reachable on its
own would put a second, non-atomic mint path next to the atomic one,
and the reaper would then have to clean up after it. The `anon:` id
generation and the `users` INSERT are that one method's private
business, and section 3 pins their shape.

### Display name

`display_name = "Guest"`, `email = NULL`. The masthead chip renders
`(user.display_name or user.tailscale_login)[0]|upper`
(`worker/templates/base.html`), so the chip mark is `G` and never leaks
`anon:9f3c...`. The chip panel's login line is the one place the raw
id would surface, and that is a named exception in section 4.

---

## 3. When one is minted

**Only inside `POST /api/instant/generate`, and only after a
generation succeeds.** Never on a page view, never on a GET, never on
a failed generation.

Crawler safety falls out of that: `GET /` resolves identity and
renders, but nothing on a GET path can insert a user row. A mint
requires a POST with a well-formed JSON body that passes
`sanitize_topic`, passes `check_and_reserve`, and returns usable
cards. `GET /healthz` and `GET /manifest.json` are unaffected.

### The endpoint's new shape

`worker/runtime/routes/instant.ts` keeps its body-size ceiling, its
limiter, its ledger resolution, and every error `kind`. What changes
is the tail:

1. Resolve the request user first (`resolveIdentity`), BEFORE
   `check_and_reserve`, so the limiter knows who is spending. Three
   cases:
   - signed-in, non-anonymous: no mint, no cookie, the deck is
     created under their real id.
   - valid `prep_anon` naming a live row: reuse it. A returning
     anonymous visitor generating a second deck stays the same person.
   - neither: mint after success. The reservation for THIS request
     carries no user id at insert time, because the account it will
     pay for does not exist yet; step 4 back-stamps it (section 6).
2. Generate (unchanged, free tier only, `worker/app/instant/generate.ts`).
3. Mint the user if needed, and create the deck and its cards SERVER
   SIDE, in ONE transaction: a single new repo method, below.
4. Resolve the ledger row `ok`, passing the user id (`repo.resolve(id,
   "ok", cards=n, user_id=uid)`). For a fresh mint this is the write
   that stamps `instant_generations.user_id`, so the generation that
   created the account counts against that account's daily window for
   the rest of the day.
5. Respond `{"kind": "ok", "redirect": "<root_path>/deck/<slug>"}`
   with `Set-Cookie: prep_anon=...` when a mint happened.

### The one write that makes step 3 atomic

`instantRepo.createInstantDeck(displayName, cards, mint)`, called
through the owner's cell. `mint` is null for an existing account and
carries the new id for a fresh one.

It writes `profile`, `decks`, `questions` and `cards` in one
synchronous transaction inside that cell. Crossing what were three
bounded contexts in one repo method is deliberate: a composite write
that must not tear has to be a single transaction, and the alternative
is threading transaction management through three repositories to keep
one write in one place.

Inside the transaction, in order:

1. **Mint, if asked.** Insert the `profile` row: display name `Guest`,
   no email, `is_anonymous`, `created_at` and `last_seen_at` from the
   clock.
2. **Check the cap, if not.** Read `is_anonymous` and the caller's deck
   and question counts, and refuse over the section 4 row cap. The
   count and the inserts it guards are the same transaction, so a
   concurrent second generation cannot slip an extra deck past a count
   taken elsewhere.
3. **Insert the deck** under a free slug (below).
4. **Insert each card**: the `questions` row (`type='short'`, answer,
   answer regex) and its `cards` row, seeded due immediately, exactly
   as the normal add path seeds one.

`answer_regex` is validated by `validateRegexUpdate` in the use case,
before the write opens. Nothing that can reject a card runs inside the
transaction.

The ledger resolve stays outside it. It already fails closed, and a
`pending` row counting as spend is the correct outcome for a crash. A
crash between the deck write and the resolve leaves a `pending` row
with no user id: it still counts against the IP, which is the window
that bounds a brand-new account anyway.

### The slug

Opaque and random, from the same generator every UI-created deck uses:
`uniqueSlug` in `worker/app/decks/validation.ts`, over `SLUG_ALPHABET`
and `SLUG_LENGTH` in `worker/app/entities.ts`. Renames do not break
links, and instant decks get the same URL shape as every other deck.

**Not the kebab-casing name slugifier** the offline named-deck path
uses. That one turns a 500-character topic (the landing textarea's
`maxlength`, and `TOPIC_MAX_CHARS`) into a 500-character URL, and
collapses any topic with no Latin characters to a shared fallback that
then collides with every other such topic. It stays where it is,
serving the path it was written for.

### The client

`static/js/modules/instant-start.js` loses `readGuestState`,
`renderContinueStrip`, `writeDeck` and `showSaveError`, and its
IndexedDB import. What is left is the form state machine plus:

```js
window.location.assign(body.redirect);
```

The "could not save on this device" error disappears with the local
write it described. Rate-limit, busy, invalid-topic and
generation-failed copy is unchanged.

---

## 4. The UI consequence

The rule is that an anonymous user gets the normal UI. Every surface
that used to special-case a device-local guest, and the rule that
replaced it. The middle column is there because several rows say
"deleted", and a reader needs to know what was deleted.

| Surface | The guest special case | The rule now |
| --- | --- | --- |
| `GET /` (`worker/runtime/worker.ts`) | dashboard / reauth shell / landing | Landing ONLY when no user resolves at all. An anonymous user gets the dashboard. The reauth branch is untouched, because the provider refuses to fall back to the cookie while a dormant session exists (section 1). |
| Landing continue strip (`worker/templates/landing.html`) | reveals a device-local guest deck | deleted. An anonymous user with a deck never sees the landing. |
| `/deck/{name}`, `/study/*`, `/session/*`, question edit, export, reorganize | signed-in only | unchanged. No anonymous branch: they take any resolved user and an anonymous user is a user. |
| Deck import (`/decks/import/csv`, `/prepdeck`, `/anki`) | any resolved user | **signed-in**. Uncapped upload into a cookie identity, exception 2 below. |
| Workflow starts (`/decks/new/srs` action=plan, `/decks/new/trivia`, `/transform/*`, plan signals) | any resolved user, agent checked inside the activity | refused when `funding_tier_for_user` is `none`, exception 2 below. |
| `/offline` shell (`static/js/offline/offline-app.js`) | guest-mode overview, guest nudge, guest disclosure | normal owner device. `meta.owner` holds the anonymous user id. Every guest branch deleted. |
| `sync.js` adoption gates | `guestAdoptionPending()` blocks flush and refresh | the guest half is deleted; the owner-ABSENT flush gate survives under a non-guest name, and the owner-MISMATCH guard is unchanged. |
| PWA install nudge (`worker/templates/base.html`) | `{% if user %}` | `{% if user and not user.is_anonymous %}`. iOS storage jars, section 1. |
| PWA install button in the colophon (`worker/templates/base.html`) | `{% if user %}` | `{% if user and not user.is_anonymous %}`. The SECOND gate on the same feature, whose own comment reads "Signed-in only, same reason as the nudge partial above". Tightening only the nudge leaves the footer entry as a working install path for anonymous users, which defeats the argument entirely. |
| Clerk bootstrap flag (`worker/templates/base.html`) | `window.__prepServerSignedOut = {{ 'false' if user else 'true' }}` | `{{ 'false' if user and not user.is_anonymous else 'true' }}`. See below. |
| `agent_available` (`worker/app/pageContext.ts`) | per-user probe | unchanged mechanism, but anonymous resolves false (below). AI controls hide and manual paths show, which is an EXISTING supported state, not a new branch. |

**Why the bootstrap flag is in this table.** `__prepServerSignedOut`
gates the client-side Clerk session recovery at
`worker/templates/base.html`. An anonymous user makes `user` truthy,
so the untightened flag reads `false`, so the bootstrap returns early
at "Server already saw us" AND clears the `clerk_reauth_reload`
guard. The victim is a real Clerk user whose `__session` JWT expired
on a browser that still holds `prep_anon`: they would be served the
anonymous account with the recovery that fixes it switched off. The
provider's dormant-session step (section 1) covers the common case;
this flag covers the case where ClerkJS can rehydrate from state the
server cannot see. Both, because they fail independently.

### The exceptions that remain

Three, and only three. Each is named here so a reviewer can check
that nothing else grew one.

**1. The landing splash gate.** Signed-out AND no resolvable user.
This is the whole point of the feature.

**2. Capability gates.** A route can demand a **signed-in** identity
rather than merely a resolved one: anonymous accounts are refused. It
is for surfaces that need a durable, provable identity (a push
endpoint that outlives the cookie, a secret to protect, a token that
authenticates elsewhere) and for surfaces whose cost is unbounded per
account. The cell's route table carries the gate per entry.

Applied per ROUTE, not per module. Handing a whole router to the
dependency is how a route nobody thought about ends up gated or
un-gated by accident, and `worker/app/notify/routes.ts` holds one route that
must NOT be gated. The full list:

| Route | Dependency | Why |
| --- | --- | --- |
| `POST /notify/subscribe` (`worker/app/notify/routes.ts`) | **signed-in** | A push endpoint outlives the cookie. |
| `POST /notify/unsubscribe` (`:126`) | **signed-in** | Pairs with subscribe. |
| `POST /notify/test` (`:142`) | **signed-in** | Sends a real push. |
| `POST /notify/prefs` (`:79`) | **signed-in** | Configures a capability they do not have. |
| `GET /notify` (`:57`) | **signed-in** | It IS the subscribe UI. |
| `GET /notify/log` (`:38`) | **signed-in** | An anonymous user cannot subscribe, so this page can only ever render empty. A route whose only possible answer is "nothing here" is worse than a redirect to the thing that would fill it. |
| `GET /notify/vapid-public-key` (`:109`) | UNCHANGED | It takes no user today and must not start. It returns the deploy-wide VAPID public key, which is public by definition, and the service worker fetches it on a path where identity is not resolved. |
| `/settings/agent` + the BYOK routes | **signed-in** | Never store a secret against an identity a cleared cookie erases. |
| `/settings/api` | **signed-in** | A PAT authenticates from outside the browser. |
| `/settings/account` | **signed-in** | There is no upstream account to delete. |
| `/decks/import/csv`, `/prepdeck`, `/anki` (`worker/app/decks/, 1940, 2001`) | **signed-in** | Uncapped upload, exception 2's cost argument below. |
| `/settings/editor`, `/settings/srs` | any resolved user | Per-user preferences with nothing to protect. Section 5 rule 12 carries both onto the target at merge. |

An anonymous request to a gated route gets 303 to `/sign-in` for HTML
routes and 403 for JSON ones.

Import is on this list for cost, not for secrecy. All three routes do
`raw = await upload.read()` with no size cap and no row cap, so one
successful generation (one pass of a 1-per-60s per-IP limiter) would
otherwise buy a cookie identity with unlimited `.apkg`, `.prepdeck`
and CSV upload. That surface is behind a real account today and stays
there. Export stays on any resolved user: reading back your own rows
grows nothing.

**Anonymous accounts are also row-capped.** The manual card form is
their only path to more content, so it needs a ceiling of its own:
5 decks and 200 questions per anonymous account, enforced in
`DeckRepo.create` and `QuestionRepo.add` when
`users.is_anonymous = 1`, refused with the existing "limit reached"
error shape. Signing up removes the cap, and the merge does not
re-apply it (the target row is not anonymous). Section 6 derives the
storage ceiling from these two numbers.

**Workflow starts need funding, not just a user.** `agent_available`
false only hides controls in templates; a direct POST to
`/decks/new/srs` with `action=plan` (`worker/app/decks/`) or
to `/decks/new/trivia` (`:406`) still registers an `active_workflows`
row and holds a worker slot until the Noop adapter fails the
activity, and worker-slot starvation is a known prod failure mode.
So every `start_*` call site in `worker/app/decks/service.ts`,
`worker/app/study/` and `worker/app/trivia/` refuses when
`funding_tier_for_user(uid)` is `none`, returning the same
"AI is not configured" error the templates already render. Anonymous
users are always `none` (below), so this closes the hole for them
without a single anonymous-specific branch, and it fixes the same
hole for a signed-in user with no key. `action=empty` is unaffected:
it starts no workflow. Free-text grading falls back to the
deterministic grader rather than starting a `GradeAnswer` workflow it
cannot pay for.

**3. The chip panel.** The masthead chip renders for everyone with
the same mark, size and behaviour. Its panel BODY differs, because
there is no email to show and most of its links are gated. Every
entry the panel renders today (`worker/templates/base.html`), and
what an anonymous user gets:

| Panel entry | Anonymous |
| --- | --- |
| display-name line | `Guest` (section 2) |
| login line (`user.tailscale_login`) | replaced by `Not signed in`. This is the one place the raw `anon:9f3c...` id would surface. |
| Activity group heading | omitted with its only child, below |
| Notification log (`/notify/log`) | omitted. The route is gated above, and the panel must not link to a route that bounces. The unseen badge beside it can never be non-zero for a user who cannot subscribe. |
| AI agent (`/settings/agent`) | omitted, gated |
| Scheduling (`/settings/srs`) | kept |
| Editor (`/settings/editor`) | kept |
| Notifications (`/notify`) | omitted, gated |
| API tokens (`/settings/api`) | omitted, gated |
| Account (`/settings/account`) | omitted. Already clerk-mode-only today; now also non-anonymous-only. |
| Sign out | replaced by **Forget this device** |
| (new) | **Create an account to keep your decks**, a single primary link to `sign_in_url` |

**Forget this device** is a POST that clears the cookie after a
`window.confirm` naming the consequence ("your decks stay on our
server, but this browser will not be able to reach them; create an
account first to keep them"), which is the same fact section 7's
privacy page states. It deletes no rows: the
account survives and reaps on the section 6 schedule. Without it an
anonymous user on a shared machine has no way to leave.

Two consequences worth pinning:

- On a deploy with no sign-in URL the primary link
  renders nothing and the panel is Scheduling, Editor and Forget this
  device. The panel must degrade to that rather than emitting a dead
  anchor.
- `_notif_unseen_context` (`worker/app/pageContext.ts`) is left alone.
  It runs a `COUNT` per render and returns 0 for a user with no rows,
  which is the correct answer for an anonymous user and the same query
  every signed-in user already pays for. Adding an anonymous branch to
  a context processor to save a zero-row indexed count is a branch
  bought with nothing.

### AI for anonymous users

`agent_for_user` (`worker/app/agent/funding.ts`) falls through to the
deploy free tier for any user with no BYOK row, which today would
hand an anonymous user unlimited worker-driven generation on the
operator's shared key.

**Rule: anonymous users reach the free tier through
`POST /api/instant/generate` and nowhere else.**
`agent_for_user(uid)` returns `_NoopAgent` when the user is
anonymous, and `funding_tier_for_user` reports `none`.

**How they learn it, given section 2 forbids the prefix test.** Both
take a bare `user_id` string, so the answer has to be a `users` read
or a string test, and the string test is the one section 2 rejects.
The read is affordable because the two call-site SHAPES are different
and only one of them is hot:

- `agent_for_user` / `funding_tier_for_user` add a single
  `SELECT is_anonymous FROM users WHERE tailscale_login = ?` at the
  top, returning `_NoopAgent` / `"none"` on a hit. This is a primary-key
  lookup, and it runs once per agent call or workflow start, next to
  the `byok_credentials` lookup `_user_has_byok_rows`
  (`worker/app/agent/funding.ts`) already does on that same path.
- `_agent_context` (`worker/app/pageContext.ts`) does NOT go through
  either. It runs on EVERY template render, and it already holds the
  whole user dict on `request.state.user`, which carries
  `is_anonymous` because `get_by_external_id` selects `*`. It reads
  the flag off the dict and short-circuits `agent_available` to False
  without calling the selector at all.

So the per-render path gains no query, the per-call path gains one
indexed row read, and nothing anywhere parses an id for a prefix.

That rule changes what an activity DOES, not what a route ADMITS, so
it needs the workflow-start guard above to have teeth. Without it an
anonymous POST still books a worker slot and only discovers the Noop
adapter inside the activity. The two together are what bound the
cost: the funding check refuses the start, and the Noop adapter is
the backstop if a start ever slips through.

Why not gate by quota instead: the plan and transform flows fan out
across job steps in a `JobCell`, which has no request, no client IP,
and no natural place to reserve against. One
user-initiated flow is between 1 and 13 upstream calls depending on
plan size, so "one reservation per flow" is not a bound. The instant
endpoint is one call, synchronously reserved, with a card cap. That
is the only anonymous shape whose spend is actually bounded.

The UI cost is zero new branches: an anonymous user sees what any
user with no AI configured sees. Their path to more decks is the
manual card form up to the row cap, or signing up.

---

## 5. Merge on signup

The heart of the spec. Data loss here is unacceptable.

### The trigger

**The first authenticated request that also carries a valid
`prep_anon` cookie naming a live anonymous user.** The entry worker
resolves the identity, sees the cookie names a different id, and runs
the merge before forwarding. It reads the cookie cheaply (one HMAC
verify, no cell read on failure), skips when the ids are the same, and
turns the result into two response decisions: clear the cookie when
`resolved`, and stash the counts for the toast when `merged`.

Three properties of that shape, each of which the obvious version gets
wrong.

**The target account must exist first.** The merge names the target
cell, so it runs after the identity resolves and the target's profile
row is written. Running earlier makes every fresh sign-up a no-op.

**The cookie is cleared only when the anonymous account is provably
gone.** `MergeResult.resolved` is the whole point of returning a
result. Clearing unconditionally means any failure path (missing
target, missing anon row, a lock timeout, an exception) throws away
the only pointer the browser has to a live account holding real
decks, which section 6 then reaps after a year while nothing in the
UI ever mentions it. Keeping the cookie costs one cheap HMAC verify
per request until the retry succeeds, and the idempotency argument
below makes that retry safe.

**Nothing here may raise.** This runs on the path of every
authenticated request. An uncaught exception turns each one carrying
an anon cookie into a 500 with no product-visible cause, so an
unreachable cell or an internal guard tripping would take the app down
for those users. The blanket catch is deliberate: data integrity is
enforced by the saga's own resume, availability is enforced here, and
the two do not have to trade against each other. Repeated failures
show up as `failed` audit rows and log lines, which is the operator's
signal, and after `MAX_ATTEMPTS` the cookie is dropped rather than
retried forever.

**Not the Clerk webhook.** `user.created`
(`worker/runtime/webhooks.ts`) arrives server to server with no
cookies. It cannot know which browser signed up. The webhook keeps
its current job (mirror the users row) and gains nothing.

**Sign-in counts, not just sign-up.** A visitor who makes a deck and
then signs into an account they already had expects the deck to come
with them. The trigger does not distinguish.

**Different device.** No cookie, no merge. The anonymous account
keeps its decks and eventually reaps (section 6). This is the honest
answer and it is a real gap; the fix is a claim link and it is a
follow-up, not v1.

**Shared browser.** Someone signs in on a machine holding a
stranger's anonymous deck and inherits it. Accepted, silently, and
here is why: the anonymous account is by construction content typed
into this browser by whoever was holding it, with no identity
attached. Treating browser-local content as belonging to whoever
signs in on that browser is the same trust model as every guest cart
on the web. The mitigations are the confirm-gated "Forget this
device" control (section 4) and the audit row (below). No adoption
dialog: the operator directive removes it, and an "is this yours?"
prompt is a question the user cannot answer better than the browser
can.

### The saga

`worker/app/auth/mergeSaga.ts`. Two cells cannot share a transaction,
so the merge is a saga bracketed by the `DirectoryCell`: take a marker,
dump the anonymous cell, apply the domain's policy, import into the
target keyed by row id, then run the anonymous cell's three-step
deletion and write the audit row.

Every step is retry-safe and **the marker is what a later request
resumes from**, so a crash anywhere leaves the anonymous account still
owning its rows, or the target already holding them, and never half of
each. A resumed attempt recomputes what is left to do: rows a previous
attempt already moved are the target's and count for nobody, so the
audit counts describe the attempt that completed rather than the union
of the attempts. The rows converge; the counts are an operator's record
of the run.

The result the caller reads has two independent booleans, and
collapsing them is exactly how the cookie gets dropped on a failure:

| Precondition | Result | Cookie |
| --- | --- | --- |
| success | `resolved`, `merged`, counts | cleared |
| anon account absent (already merged, or reaped) | `resolved`, `reason="anon_missing"` | cleared: it points at nothing |
| ids identical | `resolved`, `reason="same_user"` | cleared |
| anon account present but not anonymous | not resolved, `reason="not_anonymous"` | kept, logged. Should be unreachable |
| target account absent | not resolved, `reason="target_missing"` | kept, retried next request |
| another merge holds the marker | not resolved, `reason="merge_in_progress"` | kept: those rows are going somewhere else |
| failed `MAX_ATTEMPTS` times | not resolved, `reason="merge_failed"` | **cleared** |

`resolved` means the browser's pointer is safe to discard. `merged`
means data moved and the user should be told.

The last row is the one exception to "never clear a cookie that points
at a live account", and it is deliberate. A transient failure resolves
inside the cells adapter's own retry, so a merge that fails three times
is failing deterministically: retrying it costs the user a pair of cell
reads on every request and will never end. The rows stay where they
are and the audit row says why.

### The table set: fail closed

The set of user-scoped tables is not discovered at request time. It is
the static policy map below, and
`worker/tests/domain/merge.test.ts` asserts that the map names
every user-scoped column the schema declares: a table the schema has
and the map does not name fails the suite.

This is the same deny-list-of-known-keys shape the `.env.example`
generator uses. A user-owned table added by a future feature fails
loudly in CI rather than being silently dropped when the anonymous
cell is destroyed. The alternative, discovering the set per merge,
turns a future migration into a 500 for every authenticated request
carrying an anon cookie: the guard is about a developer's mistake, and
the request path is the wrong place to find one.

### The policy map

Order matters: parents before children, and the `users` delete last.

| # | Table | Rule | Why |
| --- | --- | --- | --- |
| 1 | `decks` | REASSIGN, with slug de-collision | `UNIQUE(user_id, name)` is the only unique constraint in the whole merge that can fire. See below. |
| 2 | `questions` | REASSIGN | No unique constraint on user. Follows its deck. |
| 3 | `study_sessions` | REASSIGN | `current_grading_workflow_id` names a `JobCell`; harmless either way. |
| 4 | `trivia_sessions` | REASSIGN | One-active-per-(user, deck) is enforced in the repo, not the schema, and the decks moved too, so no new conflict. |
| 5 | `notifications_log` | REASSIGN | Expected empty: an anonymous user cannot subscribe (section 4), so there is no push history to preserve. Reassigned rather than dropped for the same reason as rule 6: the map states what happens to rows that exist, not what the gate promises will not. |
| 6 | `active_workflows` (`user_login`) | REASSIGN | Expected empty, and enforced at the `start_*` call by the funding guard (section 4), not merely hidden in a template. Reassign anyway rather than assume. |
| 7 | `offline_sync_idempotency` | REASSIGN, conflicts DROP | PK `(user_id, client_id)`. Client ids are UUIDs so a real collision is nil, but a target row for the same client_id already records an outcome and wins. Reassigning matters: a flush interrupted by the signup must not double-apply on retry. |
| 8 | `instant_generations` (new `user_id`, section 6) | REASSIGN | The daily quota follows the person. Signing up must not reset a spent budget mid-day. |
| 9 | `push_subscriptions` | DELETE | PK is `endpoint`; reassigning could collide with the target's own device. Anonymous users cannot subscribe, so this is defence in depth on an expected-empty set. |
| 10 | `byok_credentials` | DELETE | PK `(user_id, provider)`. Anonymous users cannot store keys. Never move a secret between identities. |
| 11 | `api_tokens` | DELETE | A token authenticates elsewhere; silently re-pointing one at a different account is a credential transfer. Anonymous users cannot mint them. |
| 12 | `users` (anon row) | COPY-IF-NULL, two columns | The anonymous row carries preferences the anonymous user was allowed to set. Copied onto the target only where the target's value is NULL. See below. |
| 13 | `users` (anon row) | DELETE, guarded | Last, and only after the assert below. |

Tables with NO user column, listed explicitly so a reader can confirm
nothing was missed: `cards` (via `question_id`), `reviews` (via
`question_id`), `study_session_answers` (via `session_id`),
`trivia_queue` (via `question_id`), `grading_idempotency` (keyed by
workflow id). Ownership is derived; they need no action and must not
get one.

### The `users` row: what carries over and what is dropped

Rule 13 destroys a row with eleven columns on it, and section 4 grants an
anonymous user the two settings pages that write to two of them. A
delete with no carry-over step silently reverts those settings at
signup.

`desired_retention` is the load-bearing one. It is the FSRS retention
target (`worker/runtime/adapters/sql/prefsRepo.ts`, `get_desired_retention`), and the
scheduler already used it to compute the `next_due` values on the
anonymous user's `cards`. Dropping it leaves intervals that were
computed at one retention target being extended at another, with
nothing in the UI saying so. That is a silent scheduling change, not a
cosmetic one.

So step 12, inside the same transaction and immediately before the
delete:

```sql
UPDATE profile SET
  desired_retention = COALESCE(desired_retention, :anon_desired_retention),
  editor_input_mode = COALESCE(editor_input_mode, :anon_editor_input_mode)
```

(the target's `profile` table holds exactly one row, so there is no
`WHERE`)

COPY-IF-NULL, never overwrite. A target who already chose a retention
target or an editor mode keeps their own; the anonymous value fills a
gap rather than winning an argument. `counts` records whether each
column was filled, so the audit row says what moved.

Every `users` column and its disposition, so a reader can confirm none
was overlooked:

| Column | Disposition | Why |
| --- | --- | --- |
| `tailscale_login` | dropped | It IS the anonymous identity. `account_merges.anon_user_id` preserves it. |
| `display_name` | dropped | Always the literal `"Guest"` (section 2). Never the user's own text. |
| `email` | dropped | Always NULL on an anonymous row. |
| `profile_pic_url` | dropped | Always NULL; nothing sets it on an anonymous row. |
| `created_at` | dropped | The target's own signup date is the meaningful one. |
| `last_seen_at` | dropped | The current request refreshes the target's. |
| `is_anonymous` | dropped | The target is not anonymous, which is what lifts the row cap. |
| `notification_prefs` | dropped | Anonymous users cannot reach `/notify` (section 4), so the column is always NULL and the reader already merges over defaults. |
| `active_byok_provider` | dropped | Anonymous users cannot store keys, and a provider preference with no key behind it is meaningless. |
| `desired_retention` | COPY-IF-NULL | Set through `/settings/srs`, which anonymous users reach. Already shaped the `cards` rows being merged. |
| `editor_input_mode` | COPY-IF-NULL | Set through `/settings/editor`, which anonymous users reach. |

A future column added to `users` is NOT caught by the table-discovery
guard, which discovers user-scoped tables, not user-scoped columns.
The mitigation is this table plus a test that asserts
the `profile` table's columns match its rows exactly, so a new column
fails the suite until someone writes its disposition here.

### Slug de-collision

Still required, and not by the instant deck. Instant slugs are random
over a 32-symbol alphabet (section 3), so two accounts colliding on
one is negligible. The reachable collisions are the slugs users
actually control: offline-authored named decks
(`_slug_for_deck_name`, which kebab-cases the label) and imported
decks. Two people who both made a `french-revolution` deck is the
ordinary case.

Computed BEFORE the bulk update, inside the transaction:

```sql
SELECT name FROM decks WHERE user_id = :anon
  AND name IN (SELECT name FROM decks WHERE user_id = :target)
```

For each hit, pick the first free `"{name}-{n}"` for n in 2..100
against the target's names, and `UPDATE decks SET name = ?` on the
anon row first. `display_name` is never touched: the user's label
survives, only the URL slug moves.

When all 100 suffixes are taken, fall back to `"{name}-{6 hex}"` and
retry on the uniqueness violation. There is no exhaustion branch. The
alternative (raise and roll back the whole merge) makes a user with a
hundred same-named decks permanently unmergeable: every retry hits
the same wall, the cookie is kept forever by the rule above, and the
decks never arrive. Dropping a deck to resolve a slug collision is
still forbidden; a random suffix resolves it without losing anything
but a pretty URL.

### Atomicity, without a transaction that spans cells

Each cell's own writes are one synchronous transaction inside that
cell: the import into the target, and the deletion of the anonymous
cell. What cannot be one transaction is the pair. The marker is the
substitute, and it buys the same guarantee at a coarser grain:

- **Before anything moves**, the `DirectoryCell` records that this
  anonymous id is being merged into this target. A second request
  carrying the same cookie sees the marker and answers
  `merge_in_progress` rather than starting a second walk.
- **The import is keyed by row id**, so replaying it writes the same
  rows to the same places. A retry after a partial import is a no-op
  for what already landed.
- **The anonymous cell is destroyed last**, and only after the import
  reports the rows are in the target. Until that point the anonymous
  account still owns everything and the cookie still points at a live
  account.
- **The audit row is written at the end**, and it is what
  `previous_ids` reads so an offline device can learn its old owner id
  is now this account.

The failure that a single transaction would have prevented, and this
shape does not, is a crash between the import and the deletion: the
rows exist in both places for as long as it takes the next
authenticated request to resume. That window is bounded by the
marker, which is why resuming recomputes rather than replaying blind.

### The deletion destroys a whole cell, so the map is the guard

Destroying the anonymous cell destroys everything in it. A table
missing from the policy map is not a row left behind: it is data
silently taken with the cell. That is why the map's coverage is a test
rather than a runtime check, and why the map is the deny-list shape it
is.

The deletion itself is three steps, shared with the reaper and with
account deletion:

1. **Terminate the owner's jobs**, so no in-flight step spends another
   LLM call on an account that is going away. The tombstone is what
   actually refuses a late write; this only saves the work, so one job
   that cannot be reached does not hold up the deletion.
2. **Wipe the cell**, and write its tombstone.
3. **Scrub**, in its own RPC. A wipe leaves the freed pages readable in
   the next snapshot, and combining the scrub with the wipe fails the
   output gate and rolls the whole RPC back.

The directory's tombstone outlives the cell's: it answers for an id
whose cell has since been reclaimed.

### Idempotency

- Anonymous account already gone: `resolved`, `merged=false`.
- Re-running after success: the same, because the deletion removed it.
- Concurrent duplicate requests carrying the same cookie: the first
  takes the marker, the second reads it and answers
  `merge_in_progress`, keeping its cookie. Exactly one merge happens.

This is what makes keeping the cookie on failure safe: a retry that
lands after a success is indistinguishable from a retry that lands
after a reap, and both are resolved.

### Pre-merge safety and making a user whole

Nothing is copied and nothing is recreated: rows are REASSIGNED in
place, so there is no window in which the data exists only in a
temporary form. The only destructive statements are the three DELETE
rules on expected-empty tables and the guarded `users` delete.

The audit trail:

- `account_merges` row `status='started'` written in its OWN
  transaction before the merge begins, so it survives a rollback and
  records that an attempt happened.
- The merge transaction flips it to `completed` and stores per-table
  moved counts as JSON.
- A crash leaves a `started` row with no completion, which is the
  operator's signal to look.

To make a user whole after a bad merge, the counts plus the moved
decks are enough to reverse it: every reassigned row is still present
under the target id, and `account_merges.anon_user_id` names what it
used to be. Recovery is an operator SQL job, not a product feature;
the audit row exists precisely so that job is possible.

### The merge must be visible to the offline device

A merge changes the server's answer to "who are you", and one
existing guard reads that answer every time it syncs. `ownerAllows`
(`static/js/offline/sync.js:76-86`) compares `meta.owner.user_id`
against the snapshot's `user.id` (`worker/app/offline/`, the
`tailscale_login`). An anonymous user who studies offline has
`meta.owner.user_id = "anon:..."` stamped on that device. The merge
moves their rows to the Clerk id, so the very next snapshot fetch
returns a DIFFERENT id, `ownerAllows` sets `syncDisabled`, and
`maybeConfirmOwnerConflict` offers the user a dialog whose primary
destructive option wipes this device's offline data, including
unflushed reviews and unflushed `local_cards`.

That is the guard doing exactly what it was built to do. It cannot
know the two ids are the same person, because nothing tells it. So
the merge tells it.

**The snapshot payload carries the account's previous ids.**
`GET /api/offline/snapshot` gains one field:

```json
"user": {"id": "user_2ab...", "display_name": "...",
         "previous_ids": ["anon:9f3c..."]}
```

read from `account_merges WHERE target_user_id = :uid AND status =
'completed'` (the `idx_account_merges_target` index in section 2
serves this exact query). `ownerAllows` then treats a match against
`previous_ids` as a MATCH, and re-stamps `meta.owner.user_id` to the
current server id before returning true. The next sync compares equal
without consulting the list at all, so the list is read once per
merged device and the field stays a handful of bytes for everyone
else.

**Why not a one-shot server flag on the merging request.** The
request that performs the merge is usually not the request that
fetches a snapshot, and a flag consumed by the wrong request, a
reload, or a second tab is a flag that silently does not arrive. The
merge is a durable fact, so the thing that reports it has to be
durable too. `account_merges` already records it, and section 5
already requires retaining it.

**What is NOT weakened.** A genuine owner mismatch (a different
person signing in on this device) still hits the dialog, because
their id is in nobody's `previous_ids`. `previous_ids` is
server-derived and never accepted from the client, so a device cannot
assert its way past the guard.

### What the user sees

One toast on the next dashboard render: "Your deck {name} was added
to this account." It renders from `request.state.anon_merged`, which
`_try_merge_anon_cookie` sets only on `merged=True`, on the same
response that clears the cookie, so it renders exactly once. No
dialog, no choice, no undo button. The audit table is the recovery
path, not the UI.

### Review history

Anonymous reviews are ordinary `reviews` rows written by the normal
study loop against real `cards` rows, with real FSRS state. The merge
moves their parent `questions`, so the account's scheduling reflects
the studying the visitor did before signing up. Nothing is re-graded,
nothing is replayed, nothing is reset. This is strictly simpler than
the adoption path it replaces, which had to replay an outbox through
the scheduler.

---

## 6. Lifecycle and abuse

### The reaper

A task on the existing scheduler tick (`worker/app/notify/wake.ts`),
running every tick, off the event loop, bounded per tick:

```sql
SELECT tailscale_login FROM users
 WHERE is_anonymous = 1 AND last_seen_at < :cutoff
 LIMIT 50
```

with `cutoff = now - 365 days`. For each id, in its OWN transaction:
delete the non-FK rows explicitly (`notifications_log`,
`active_workflows`, `offline_sync_idempotency`,
`instant_generations`), then `DELETE FROM users`, letting the FK
chain cascade the rest. Same table set the merge discovers, same
fail-closed policy map, so a new user-owned table cannot be forgotten
in one place and remembered in the other.

**It runs in a worker thread.** `_tick` (`worker/app/notify/wake.ts`)
is a coroutine that calls blocking sqlite directly, so a cascading
delete over a day's worth of reaped accounts would hold the write lock
while the event loop is parked, and every in-flight request would
stall behind it. The same file already solves this for the trivia
tick, and the reaper uses the same escape:
`await asyncio.to_thread(_reap, now_utc)`.

**The bound replaces a throttle rather than needing one.** The tick is
`_TICK_SECONDS = 300`, and no state persists across a restart, so any
"at most hourly" rule would be a claim with nothing behind it: a
deploy loop would re-run the reaper on every boot. `LIMIT 50` per tick
is self-limiting instead. It clears 14,400 accounts a day against a
mint ceiling of 200 a day (the global daily cap), so a backlog drains
in days while no single tick can hold the lock for long. A per-account
transaction lets request traffic interleave between accounts rather
than waiting out the whole batch.

**The cutoff must be formatted by `now()`.** `last_seen_at` is written
by `worker/runtime/adapters/sql/schema.ts`'s `now()`, which emits an aware
`isoformat()` with a `+00:00` suffix, and the comparison is a TEXT
sort. A cutoff formatted with a `Z` suffix sorts ABOVE an equal-instant
`+00:00` value, because `'Z' > '+'`, so a mismatched formatter deletes
rows it should keep. Build the cutoff as
`(datetime.now(timezone.utc) - timedelta(days=365)).isoformat()` and
never with `strftime`. A test pins a row written by `now()` at exactly
the cutoff instant and asserts it survives.

**A returning visitor is never reaped**, because `UserRepo.touch`
bumps `last_seen_at` on every resolved request, including a request
that only renders the dashboard. The 365-day window deliberately
EXCEEDS the cookie's 180-day Max-Age: a cookie that still verifies
always names a live row, so "valid cookie, missing user" is a rare
path (secret rotation, manual delete) rather than a routine one. It
is still handled (section 1: clear and treat as a visitor).

### Rate limits

`instant_generations` gains `user_id TEXT` (nullable) and an index on
`(user_id, created_at)`. `check_and_reserve`
(`worker/runtime/adapters/sql/limiterRepo.ts`) takes `user_id` and grows a per-user
window alongside the per-IP ones. Both must pass.

**When the id is known, and what to do when it is not.**
`check_and_reserve` inserts the `pending` row BEFORE generation, and
the endpoint mints the account AFTER generation succeeds, so the one
request that creates an account cannot name it at reserve time. Two
consequences, both deliberate:

- A returning anonymous visitor and a signed-in user both have an id
  at reserve time (section 3 resolves the user before the limiter
  runs), so their per-user window is checked up front, which is every
  request except the first of a new account.
- The account-minting request passes `user_id=None`, is admitted or
  refused on the per-IP and global windows alone, and then
  `repo.resolve(..., user_id=uid)` back-stamps the row. The spend is
  attributed from that moment, so it counts against the new account's
  daily window for the rest of the day.

NULL is therefore two different things in this column, and the code
must not conflate them: a row predating this change, and a row whose
account did not exist yet. Neither is queried by the per-user window,
which only ever filters on a concrete id.

| Window | Scope | Default |
| --- | --- | --- |
| burst | per IP | 1 per 60s (unchanged) |
| daily | per IP | 3 spend (unchanged) |
| daily | per anonymous user | 3 spend (new) |
| daily | per signed-in user | 20 spend (new) |
| per-minute | global | 4 spend (unchanged) |
| daily | global | 200 spend (unchanged) |

The per-IP windows are the anti-Sybil lever (clearing cookies does
not buy a fresh budget). The per-user windows are the anti-NAT lever
(a stranger on the same carrier CGNAT cannot spend a signed-in user's
budget). Neither alone is sufficient; the pair is.

### Storage

Two ceilings, because an account's size is bounded by policy, not by
the one deck that minted it. Quoting only the typical figure is how a
storage model ends up wrong by orders of magnitude.

**Typical.** 1 `users` row, 1 `decks` row, 5 `questions`, 5 `cards`,
plus reviews as they study. Roughly 3 to 4 KB at rest before any
studying. The global daily cap of 200 spend outcomes bounds the mint
rate at 200 accounts per day, so at the 365-day reap window the
steady state is about 73,000 accounts and roughly 280 MB of sqlite on
a box with 38 GB.

**Adversarial.** An account, once minted, can add content without
spending anything: the manual card form is not on the generation
limiter. What bounds it is the per-account cap from section 4, 5
decks and 200 questions, which is why that cap is part of this design
and not a detail. 200 questions with their cards and some review
history is on the order of 100 KB, so the worst case is 200 accounts
per day times 100 KB times 365 days, about 7 GB. Large, bounded, and
visible long before it lands.

Deck import is NOT in either number, because section 4 moves it to
**signed-in**. Uncapped `upload.read()` into a cookie identity has
no ceiling worth computing.

Three levers, in the order to reach for them: the per-account row cap
(smallest blast radius), the anonymous retention window, then the
global daily mint cap. Reviews grow the typical number over time, and
that growth is the product working.

---

## 7. Privacy

The cookie is a persistent identifier. `worker/templates/privacy.html` gains
a section, and the "What we collect" list gains a line.

> **Anonymous accounts.** If you create a deck without signing in, we
> store it on our server and set a cookie named `prep_anon` on your
> browser so we can show it back to you. That cookie is a random
> identifier with a signature. It is not used for analytics or
> advertising, it is not shared with anyone, and it holds nothing
> about you beyond that identifier. If you later sign in on the same
> browser, the decks move to your account and the cookie is deleted.
> If you clear your cookies, or use "Forget this device", the decks
> stay on our server but nothing can reach them and they are deleted
> after a year of no use. Anonymous accounts idle for a year are
> deleted along with their decks.
>
> **Rate-limit records.** Each deck generation writes one row holding
> your IP address, the time, and the account it was charged to. We use
> it to enforce the daily generation limits and for nothing else. Those
> rows are deleted after 7 days, so the link between an IP address and
> an account does not persist.

The IP line is not optional politeness. `instant_generations` already
stores `ip` (`worker/runtime/adapters/sql/schema.ts`), and section 6 adds
`user_id` to the same row, which creates a durable IP-to-account-to-decks
join that did not exist before this spec. Section 6's per-user window
needs it. `RETENTION_DAYS = 7` (`worker/runtime/adapters/sql/limiterRepo.ts`) with the
prune inside `check_and_reserve` (`:113`) is what bounds it, and a
bound is only a privacy property if it is stated where users read.

What an anonymous user cannot do, and why each one:

| Capability | Anonymous | Reason |
| --- | --- | --- |
| Study, create, edit, delete, export decks | yes | No durable identity needed. |
| Offline study and sync | yes | The owner guard works on any user id, and the merge tells it about the id it used to have (section 5). |
| Deck import (CSV, `.apkg`, `.prepdeck`) | no | Uncapped upload into an identity that costs one generation to obtain. Section 6 derives the ceiling from its absence. |
| More than 5 decks or 200 questions | no | The same cost bound, for the paths that have no upload. Signing up removes the cap. |
| AI beyond the one generation that minted the account | no | Only `POST /api/instant/generate` has a synchronously reserved, card-capped spend. Section 4. |
| Scheduling and editor preferences | yes | Per-user preference, nothing to protect. Both carry onto the account at signup when it has not set its own (section 5, rule 12). |
| Web push | no | A push endpoint outlives the cookie. A notification delivered to a person we can no longer identify is a notification to nobody. |
| BYOK key | no | Storing someone's provider key against an identity that a cleared cookie erases is a way to lose their secret with no recovery path. |
| API tokens | no | A PAT authenticates from outside the browser; issuing one to a browser-cookie identity creates a credential that outlives the identity that owns it. |
| Account deletion page | no | There is no upstream account to delete. "Forget this device" is the equivalent control. |
| PWA install nudge and footer install button | no | iOS storage jars, section 1. Both entries, not just the nudge. |

The generation disclosure (prompts go to a shared third-party free
inference endpoint) is unchanged and still renders under the landing
topic box.

---

## 8. Follow-ups, explicitly not this spec

- **Claim link.** A one-shot signed URL that moves an anonymous
  account onto another device or into the iOS PWA jar. Fixes both
  the different-device merge gap and the install gate.
- **Anonymous free tier beyond one endpoint.** Needs a spend
  reservation the Go worker can participate in.
- **Upload caps on deck import.** `raw = await upload.read()` reads
  the whole body with no size cap for signed-in users too
  (`worker/app/decks/, 1940, 2001`). This spec removes the
  anonymous exposure by gating the routes, and does not fix the
  underlying route. A streaming read with a byte ceiling is the fix.
- **Merge undo.** The audit row makes it possible; nothing consumes
  it yet.
- **Anonymous trivia decks.** The instant deck is SRS only.
