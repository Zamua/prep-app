# Anonymous accounts

Design spec for server-side anonymous identity. A visitor who
generates a deck becomes a real user row, identified by a cookie,
served by the normal UI. If they later sign in, their anonymous
account merges into the signed-in one.

The rule this spec exists to enforce:

> Anonymous users are anonymous, not ephemeral. For an anonymous
> user, a signed-in user, and a signed-in user studying offline, the
> UI is the same UI. The only people who see the splash page are
> visitors who are not signed in AND have never made a deck.

Companions: [OFFLINE.md](OFFLINE.md) (the local-first machinery this
spec KEEPS and now applies to anonymous users too) and
[AI-PROVIDERS.md](AI-PROVIDERS.md) (the free tier the generation
endpoint spends). [INSTANT-START.md](INSTANT-START.md) sections 2.3
through 3.4 are SUPERSEDED by this document; its sections 1, 3.1 and
3.2 (the endpoint, the prompt, the rate limiter) stay true.

---

## 0. What this replaces

The shipped instant-start flow stores an anonymous visitor's deck in
IndexedDB as guest data (`meta.guest`, owner-absent `local_cards`),
studies it through the offline shell at `/offline`, and adopts it into
a real account at signup through the offline sync engine (an adoption
dialog, an `adoptionApproved` latch, `guestAdoptionPending()` gates on
`refreshSnapshot` and `flushOutbox`).

Every GUEST-specific piece is deleted (section 8). Under this design
the anonymous visitor's deck is an ordinary `decks` row owned by an
ordinary `users` row, rendered by the ordinary dashboard, deck page
and study loop. There is no guest surface, no adoption, and no
guest-specific copy.

The offline machinery itself STAYS: the IndexedDB snapshot, the
outbox, the local scheduler and grader, `meta.owner`, the
owner-mismatch conflict dialog, and `offline_sync_idempotency`. That
is a different feature, and it now serves anonymous users because an
anonymous user is a user.

Two things inside it do change, and section 8 names them precisely.
The owner guard has to learn that a merged account used to have
another id, or the happy path of this feature ends at a dialog
offering to wipe the user's own unflushed work. And the owner-ABSENT
flush gate has to survive the deletion of the guest flow it is
tangled with, because "no owner yet" and "wrong owner" are different
questions with different right answers.

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
with `hmac.compare_digest`, reject when `iat` is in the future by more
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
`_no_cache_html` middleware (`prep/app.py:266`) emits the
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
(`prep/byok/crypto.py:74`) with
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
separate from Safari (`templates/base.html:231`). Adding prep to the
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
a cached manifest is a session-fixation footgun. Follow-up, section 11.

### Where the branch lives

Three candidate shapes. One wins.

**Chosen: a decorator over the configured provider.**
`AnonymousFallbackProvider(inner: IdentityProvider)` satisfies
`IdentityProvider` (`prep/auth/port.py:62`), delegates `urls()` and
`has_dormant_session()` straight through, and implements `resolve()`
as:

```
def resolve(self, request):
    resolved = self._inner.resolve(request)
    if resolved is not None:
        return resolved          # 1. signed-in ALWAYS wins
    if self._inner.has_dormant_session(request):
        return None              # 2. let the reauth shell recover it
    return self._resolve_anon_cookie(request)   # 3. then the cookie
```

It is composed in `prep/auth/providers/_build_provider()`
(`prep/auth/providers/__init__.py:24`), wrapping whichever adapter
`PREP_AUTH_MODE` selected, and only when the cookie secret resolves.

**The precedence rule, in full: signed-in > dormant session >
anonymous cookie > visitor.** All four steps live in this one
function, so no call site has to remember them.

The dormant-session step is load-bearing, not decoration. A returning
Clerk user on a PWA cold launch has an expired `__session` JWT and a
live `__client_uat`, so `_inner.resolve()` returns None while
`has_dormant_session()` returns True. Without step 2 the resolver
would fall through to a `prep_anon` cookie left on that browser and
serve a signed-in person their old anonymous account, and every
recovery path keyed on "no user resolved" would stop firing:
`index()` (`prep/web/index.py:201`) tests `user is None` BEFORE
`has_dormant_session`, so `reauth.html` would never render, and a
`current_user` route would be served as the wrong user instead of
returning the 401 that triggers a re-handshake. With step 2 in place
`index()` and every other `current_user` route are genuinely
untouched by this spec.

**Rejected: a fourth `PREP_AUTH_MODE=anon` provider.** The registry
picks exactly one provider. An anonymous-only provider could not also
resolve Clerk sessions, so "signed-in wins" would be unimplementable
inside the seam and would leak back out to the caller.

**Rejected: a branch in `optional_current_user`.** It puts provider
composition in the FastAPI dependency, duplicates the precedence rule
across `prep/auth/identity.py` and any future resolver, and makes the
`IdentityProvider` seam decorative. The dependency should keep asking
one question of one object.

### One narrow addition to the port

`ResolvedUser` (`prep/auth/port.py:31`) gains
`is_anonymous: bool = False`. `optional_current_user`
(`prep/auth/identity.py:42`) branches on exactly that:

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
miss (`prep/auth/repo.py:70`), so a stale cookie naming a reaped
account would silently resurrect it as an empty user forever. Rows
are created at mint time only (section 3).

The merge call sits AFTER `upsert` and takes the upserted row's id.
Ordering is not stylistic. On a fresh sign-up the target `users` row
does not exist until `upsert` creates it, `decks.user_id` has an FK to
`users(tailscale_login)`, and the merge refuses a missing target
(section 5). Calling it before the upsert makes the single most
important path in this spec a silent no-op whenever the Clerk
`user.created` webhook (`prep/auth/webhooks_clerk.py:67`) has not
already raced ahead and mirrored the row. That failure is
intermittent by construction and invisible to any test that seeds the
target user first, which is why section 10's M4 has a test that signs
up an id with no pre-existing row and no webhook.

Clearing a stale cookie needs a response, which `resolve()` does not
have. The existing `_no_cache_html` middleware (`prep/app.py:266`)
grows a two-branch block, placed BEFORE its `text/html` check so JSON
responses get it too: `request.state.anon_cookie_stale` triggers
`response.delete_cookie` with the same name/path,
`request.state.anon_cookie_refresh` triggers `response.set_cookie`
with the re-minted value and the full attribute set from the table
above. Starlette backs `request.state` with `scope["state"]` and
`BaseHTTPMiddleware` passes the same scope object down, so both flags
survive the middleware boundary.

`prep/api/auth.py` (bearer tokens) is untouched. Anonymous users
cannot mint API tokens (section 4), so that surface never sees one.

---

## 2. The user row

### What marks a user anonymous

Both a namespaced id and an explicit column.

- `users.tailscale_login = "anon:" + <32 hex chars>`. The column is
  the universal external id (`prep/auth/port.py:35`); the prefix makes
  the row self-describing in logs and cannot collide with a Clerk
  `user_...` id or a Tailscale email.
- `users.is_anonymous INTEGER NOT NULL DEFAULT 0`. This is what code
  branches on. Branching on a string prefix is an implicit contract
  that rots the first time an identity provider emits an id starting
  with `anon:`; the column cannot be spoofed by a provider.

Migration (into `db.init()`, PRAGMA-guarded like every other step):

```python
# 25. Anonymous accounts. `is_anonymous` marks a user row minted from
#     a prep_anon cookie rather than an identity provider. The partial
#     index serves the reaper's only query.
ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
if "is_anonymous" not in ucols:
    c.execute("ALTER TABLE users ADD COLUMN is_anonymous INTEGER NOT NULL DEFAULT 0")
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_users_anon_last_seen "
    "ON users(last_seen_at) WHERE is_anonymous = 1"
)

# 26. Merge audit. One row per attempted merge of an anonymous account
#     into a provider account. Retained: it is the only record that a
#     given deck used to belong to a different user id, and the
#     offline snapshot reads the completed rows to tell a device that
#     its old owner id is now this account (section 5).
c.executescript("""
    CREATE TABLE IF NOT EXISTS account_merges (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        anon_user_id   TEXT NOT NULL,
        target_user_id TEXT NOT NULL,
        started_at     TEXT NOT NULL,
        completed_at   TEXT,
        status         TEXT NOT NULL,   -- started | completed | failed
        counts         TEXT,            -- JSON {table: rows_moved}
        error          TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_account_merges_anon
        ON account_merges(anon_user_id);
    CREATE INDEX IF NOT EXISTS idx_account_merges_target
        ON account_merges(target_user_id, status);
""")
```

`ALTER TABLE ... ADD COLUMN` with a constant default is safe on
sqlite and rewrites nothing.

### Repo surface

`UserRepo` gains one method and `upsert` is untouched.

- `touch(user_id) -> None`: bare `UPDATE users SET last_seen_at = ?`.
  Keeps the reaper honest without upsert's insert-on-miss.
- `get_by_external_id` (exists, `prep/auth/repo.py:150`) is reused
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
(`templates/base.html:176`), so the chip mark is `G` and never leaks
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

`prep/instant/routes.py:153` keeps its body-size ceiling, its
limiter, its ledger resolution, and every error `kind`. What changes
is the tail:

1. Resolve the request user first (`optional_current_user`), BEFORE
   `check_and_reserve`, so the limiter knows who is spending. Three
   cases:
   - signed-in, non-anonymous: no mint, no cookie, the deck is
     created under their real id.
   - valid `prep_anon` naming a live row: reuse it. A returning
     anonymous visitor generating a second deck stays the same person.
   - neither: mint after success. The reservation for THIS request
     carries no user id at insert time, because the account it will
     pay for does not exist yet; step 4 back-stamps it (section 6).
2. Generate (unchanged, free tier only, `prep/instant/service.py`).
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

The existing repos cannot express step 3. `DeckRepo.create`
(`prep/decks/repo.py:125`) and `QuestionRepo.add` (`:486`) each open
their own `cursor()`, and `cursor()`
(`prep/infrastructure/db.py:41-50`) is a connection factory that
commits and closes on exit. Calling them in sequence is three or more
independent commits, and `decks.user_id`'s FK to
`users(tailscale_login)` forces the `users` row to land first. A crash
between commits produces exactly the states this section forbids: a
minted user with no deck, or a 2-of-5 deck.

So the write is one repo method holding one connection, following the
pattern `SyncRepo.create_card` (`prep/offline/repo.py:233`) already
uses to write a question, its `cards` row and an idempotency pin in
one transaction:

```python
# prep/instant/repo.py
def create_instant_deck(
    *,
    user_id: str | None,   # None mints a new anonymous account
    display_name: str,
    cards: list[dict],     # InstantDeck.cards, already validated by the service
) -> InstantDeckResult:    # (user_id, slug, minted: bool)
```

It writes `users`, `decks`, `questions` and `cards` from the instant
context, which crosses two other contexts' tables. That is deliberate
and it has precedent: `prep/offline/repo.py` writes the same tables
for the same reason. Atomicity is a property of ONE connection, and
`cursor()` gives every repo method its own, so any composite write
that must not tear has to be a single method. The alternative is
threading a connection through `DeckRepo` and `QuestionRepo`
signatures, which spreads transaction management across three
contexts to keep one write in one place.

Body, all inside a single `with cursor() as c:` opened with
`BEGIN IMMEDIATE`:

1. When `user_id` is None, generate `"anon:" + secrets.token_hex(16)`
   and INSERT the `users` row (`display_name="Guest"`, `email=NULL`,
   `is_anonymous=1`, `created_at`/`last_seen_at` from `now()`).
   Set `minted=True`.
2. When `user_id` is given, SELECT `is_anonymous` and the caller's
   deck and question counts on THIS connection, and refuse over the
   section 4 row cap. The cap check and the inserts it guards read
   the same transaction, so a concurrent second generation cannot
   slip a sixth deck past a count taken on another connection.
3. Pick a free slug (below) and INSERT the `decks` row with
   `display_name=display_name`.
4. Per card, INSERT the `questions` row (`type='short'`, `answer`,
   `answer_regex`) and its `cards` row (`step=0`,
   `next_due=created_at`), mirroring `QuestionRepo.add`'s SRS seeding.

`BEGIN IMMEDIATE` takes the write lock up front, the same way
`check_and_reserve` (`prep/instant/repo.py:112`) does, so the count
in step 2 and the inserts in steps 3 and 4 are serialized against
another generation from the same account. `PRAGMA foreign_keys` is ON,
and an FK is satisfied by a parent inserted earlier in the same
transaction, so step 1 and step 3 co-exist in one commit.

`answer_regex` is re-validated by
`grading.validate_regex_update(regex, expected_literal=answer)` in the
SERVICE, before the transaction opens. Nothing that can reject a card
runs inside the write.

The ledger resolve (step 4 of the endpoint) stays outside the
transaction. It already fails closed, and a `pending` row counting as
spend is the correct outcome for a crash. A crash between the deck
write and the resolve leaves a `pending` row with a NULL `user_id`:
it still counts against the IP, which is the window that bounds a
brand-new account anyway.

### The slug

Opaque and random, from the same generator every UI-created deck
uses: 8 characters over the 32-symbol alphabet in
`_unique_slug` (`prep/decks/routes.py:102`), whose docstring gives the
reason ("renames don't break links"). Instant decks get the same URL
shape as every other deck.

NOT `_slug_for_deck_name` (`prep/offline/repo.py:30`). It kebab-cases
the label, so a 500-character topic (the landing textarea's
`maxlength`, and `TOPIC_MAX_CHARS`) yields a 500-character URL, and
any topic with no Latin characters collapses to the shared `"deck"`
fallback and then collides with every other such topic. That helper
stays where it is, serving the offline named-deck path it was written
for.

`_SLUG_ALPHABET` and `_SLUG_LENGTH` move from `prep/decks/routes.py`
to `prep/decks/entities.py` so both call sites share one definition.
`create_instant_deck` cannot reuse `_unique_slug` itself: that helper
checks freeness through `deck_repo.find_id`, which opens its own
connection. It draws a candidate and checks it with a SELECT on the
transaction's own connection, retrying up to 100 times, then raising.

`display_name` is `service.display_name_for(topic)`, which already
collapses whitespace and truncates to `DISPLAY_NAME_MAX_CHARS`.

Model output no longer round-trips through the browser at all. That
removes a whole class of client-side trust question: the cards the
user studies are the cards the server validated and stored.

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

Every surface that special-cases a guest today, and what it becomes.

| Surface | Today | Becomes |
| --- | --- | --- |
| `GET /` (`prep/web/index.py:185`) | dashboard / reauth shell / landing | Landing ONLY when no user resolves at all. An anonymous user gets the dashboard. The reauth branch is untouched, because the provider refuses to fall back to the cookie while a dormant session exists (section 1). |
| Landing continue strip (`templates/landing.html:31`) | reveals a device-local guest deck | deleted. An anonymous user with a deck never sees the landing. |
| `/deck/{name}`, `/study/*`, `/session/*`, question edit, export, reorganize | signed-in only | unchanged. No anonymous branch: they take `current_user` and an anonymous user is a user. |
| Deck import (`/decks/import/csv`, `/prepdeck`, `/anki`) | `current_user` | `signed_in_user`. Uncapped upload into a cookie identity, exception 2 below. |
| Workflow starts (`/decks/new/srs` action=plan, `/decks/new/trivia`, `/transform/*`, plan signals) | `current_user`, agent checked inside the activity | refused when `funding_tier_for_user` is `none`, exception 2 below. |
| `/offline` shell (`static/js/offline/offline-app.js`) | guest-mode overview, guest nudge, guest disclosure | normal owner device. `meta.owner` holds the anonymous user id. Every guest branch deleted. |
| `sync.js` adoption gates | `guestAdoptionPending()` blocks flush and refresh | the guest half is deleted; the owner-ABSENT flush gate survives under a non-guest name, and the owner-MISMATCH guard is unchanged (section 8). |
| PWA install nudge (`templates/base.html:236`) | `{% if user %}` | `{% if user and not user.is_anonymous %}`. iOS storage jars, section 1. |
| PWA install button in the colophon (`templates/base.html:258`) | `{% if user %}` | `{% if user and not user.is_anonymous %}`. The SECOND gate on the same feature, whose own comment reads "Signed-in only, same reason as the nudge partial above". Tightening only the nudge leaves the footer entry as a working install path for anonymous users, which defeats the argument entirely. |
| Clerk bootstrap flag (`templates/base.html:103`) | `window.__prepServerSignedOut = {{ 'false' if user else 'true' }}` | `{{ 'false' if user and not user.is_anonymous else 'true' }}`. See below. |
| `agent_available` (`prep/web/templates.py:37`) | per-user probe | unchanged mechanism, but anonymous resolves false (below). AI controls hide and manual paths show, which is an EXISTING supported state, not a new branch. |

**Why the bootstrap flag is in this table.** `__prepServerSignedOut`
gates the client-side Clerk session recovery at
`templates/base.html:104-141`. An anonymous user makes `user` truthy,
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

**2. Capability gates.** A new dependency in `prep/auth/identity.py`:

```python
def signed_in_user(request: Request) -> dict:
    """current_user, plus: anonymous accounts are refused. For
    surfaces that need a durable, provable identity (a push endpoint
    that outlives the cookie, a secret we must protect, a token that
    authenticates elsewhere) and for surfaces whose cost is unbounded
    per account."""
```

Applied per ROUTE, not per module. Handing a whole router to the
dependency is how a route nobody thought about ends up gated or
un-gated by accident, and `prep/notify/routes.py` holds one route that
must NOT be gated. The full list:

| Route | Dependency | Why |
| --- | --- | --- |
| `POST /notify/subscribe` (`prep/notify/routes.py:114`) | `signed_in_user` | A push endpoint outlives the cookie. |
| `POST /notify/unsubscribe` (`:126`) | `signed_in_user` | Pairs with subscribe. |
| `POST /notify/test` (`:142`) | `signed_in_user` | Sends a real push. |
| `POST /notify/prefs` (`:79`) | `signed_in_user` | Configures a capability they do not have. |
| `GET /notify` (`:57`) | `signed_in_user` | It IS the subscribe UI. |
| `GET /notify/log` (`:38`) | `signed_in_user` | An anonymous user cannot subscribe, so this page can only ever render empty. A route whose only possible answer is "nothing here" is worse than a redirect to the thing that would fill it. |
| `GET /notify/vapid-public-key` (`:109`) | UNCHANGED | It takes no user today and must not start. It returns the deploy-wide VAPID public key, which is public by definition, and the service worker fetches it on a path where identity is not resolved. |
| `/settings/agent` + the BYOK routes | `signed_in_user` | Never store a secret against an identity a cleared cookie erases. |
| `/settings/api` | `signed_in_user` | A PAT authenticates from outside the browser. |
| `/settings/account` | `signed_in_user` | There is no upstream account to delete. |
| `/decks/import/csv`, `/prepdeck`, `/anki` (`prep/decks/routes.py:1874, 1940, 2001`) | `signed_in_user` | Uncapped upload, exception 2's cost argument below. |
| `/settings/editor`, `/settings/srs` | `current_user` | Per-user preferences with nothing to protect. Section 5 rule 12 carries both onto the target at merge. |

An anonymous request to a gated route gets 303 to `/sign-in` for HTML
routes and 403 for JSON ones.

Import is on this list for cost, not for secrecy. All three routes do
`raw = await upload.read()` with no size cap and no row cap, so one
successful generation (one pass of a 1-per-60s per-IP limiter) would
otherwise buy a cookie identity with unlimited `.apkg`, `.prepdeck`
and CSV upload. That surface is behind a real account today and stays
there. Export stays on `current_user`: reading back your own rows
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
`/decks/new/srs` with `action=plan` (`prep/decks/routes.py:330`) or
to `/decks/new/trivia` (`:406`) still registers an `active_workflows`
row and holds a worker slot until the Noop adapter fails the
activity, and worker-slot starvation is a known prod failure mode.
So every `start_*` call site in `prep/decks/service.py`,
`prep/study/service.py` and `prep/trivia/service.py` refuses when
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
entry the panel renders today (`templates/base.html:174-218`), and
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
`window.confirm` naming the consequence ("your decks stay on this
browser only; sign up first to keep them"). It deletes no rows: the
account survives and reaps on the section 6 schedule. Without it an
anonymous user on a shared machine has no way to leave.

Two consequences worth pinning:

- In tailscale mode `urls().sign_in` is empty, so the primary link
  renders nothing and the panel is Scheduling, Editor and Forget this
  device. The panel must degrade to that rather than emitting a dead
  anchor.
- `_notif_unseen_context` (`prep/web/templates.py:183`) is left alone.
  It runs a `COUNT` per render and returns 0 for a user with no rows,
  which is the correct answer for an anonymous user and the same query
  every signed-in user already pays for. Adding an anonymous branch to
  a context processor to save a zero-row indexed count is a branch
  bought with nothing.

### AI for anonymous users

`agent_for_user` (`prep/agent/selector.py:287`) falls through to the
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
  (`prep/agent/selector.py:270`) already does on that same path.
- `_agent_context` (`prep/web/templates.py:37`) does NOT go through
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

Why not gate by quota instead: the plan/transform flows fan out
through Temporal activities on the Go worker, which has no request,
no client IP, and no natural place to reserve against. One
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
`prep_anon` cookie naming a live anonymous user.** Implemented in
`optional_current_user`, immediately after `UserRepo.upsert` has
created or refreshed the target row:

```python
def _try_merge_anon_cookie(request: Request, user: dict) -> None:
    """Merge, and never fail the request while doing it."""
    raw = request.cookies.get(ANON_COOKIE)
    if not raw:
        return
    anon_id = verify_cookie(raw)          # cheap, no DB read on failure
    if not anon_id or anon_id == user["tailscale_login"]:
        return
    try:
        result = merge_anonymous_into(anon_id, user["tailscale_login"])
    except Exception:
        logger.exception("anon merge failed: anon=%s", anon_id)
        return                            # cookie SURVIVES; retry next request
    if result.resolved:
        request.state.anon_cookie_stale = True     # only now clear it
    if result.merged:
        request.state.anon_merged = result.counts  # feeds the toast
```

Three properties of that shape, each of which the obvious version
gets wrong.

**The target row must exist first.** See section 1: the merge takes
the id of the row `upsert` just wrote. Running before the upsert
makes every fresh sign-up a no-op.

**The cookie is cleared only when the anonymous account is provably
gone.** `MergeResult.resolved` is the whole point of returning a
result. Clearing unconditionally means any failure path (missing
target, missing anon row, a lock timeout, an exception) throws away
the only pointer the browser has to a live account holding real
decks, which section 6 then reaps after a year while nothing in the
UI ever mentions it. Keeping the cookie costs one cheap HMAC verify
per request until the retry succeeds, and the idempotency argument
below makes that retry safe.

**Nothing here may raise.** This runs inside `optional_current_user`,
which every route reaches through `current_user`. An uncaught
exception turns every authenticated request carrying an anon cookie
into a 500 with no product-visible cause, so a lock timeout
(`sqlite3.OperationalError`) or an internal guard tripping would take
the whole app down for those users. The blanket `except` is
deliberate: data integrity is enforced by the transaction rolling
back, availability is enforced here, and the two do not have to trade
against each other. Repeated failures show up as `failed` audit rows
and log lines, which is the operator's signal.

**Not the Clerk webhook.** `user.created`
(`prep/auth/webhooks_clerk.py`) arrives server to server with no
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

### The module

`prep/auth/merge.py`, one public function:

```python
def merge_anonymous_into(anon_user_id: str, target_user_id: str) -> MergeResult

@dataclass(frozen=True)
class MergeResult:
    resolved: bool            # the anonymous account is gone from the DB
    merged: bool              # THIS call is what moved it
    counts: dict[str, int]    # per-table rows moved
    reason: str | None        # why not, when resolved is False
```

Preconditions are checked inside the transaction, and each one maps
to a specific result rather than a generic no-op, because the caller
has to decide from it whether to drop the cookie:

| Precondition | Result | Cookie |
| --- | --- | --- |
| success | `resolved`, `merged`, counts | cleared |
| anon row absent (already merged, or reaped) | `resolved`, `reason="anon_missing"` | cleared: it points at nothing |
| ids identical | `resolved`, `reason="same_user"` | cleared |
| anon row present but `is_anonymous = 0` | not resolved, `reason="not_anonymous"` | kept, logged. Should be unreachable |
| target row absent | not resolved, `reason="target_missing"` | kept, retried next request |

`resolved` and `merged` are two different answers and collapsing them
is how the cookie gets dropped on a failure. `resolved` means the
browser's pointer is safe to discard. `merged` means data moved and
the user should be told.

### Table discovery: fail closed, at boot

The set of user-scoped tables is DISCOVERED from the live schema, not
hand-listed:

```
discovered = {t : t has an FK to users(tailscale_login)}
           | {t : t has a column named user_id or user_login}
```

(The FK set alone misses `notifications_log`, `active_workflows` and
`offline_sync_idempotency`, which carry a user column with no
declared FK. Verified against `prep/infrastructure/db.py`.)

The policy map below is keyed by table name, and **any discovered
table not in the map raises `UnknownUserScopedTable`.** This is the
same deny-list-of-known-keys shape the `.env.example` generator uses:
a new user-owned table added by a future feature fails loudly rather
than being silently cascaded away when the anonymous row is deleted.

**Where that check runs is the design decision.** It runs once at
BOOT, at the tail of `db.init()`, and again in CI (section 10, M4).
It does NOT run per merge. A schema-drift check on the request path
converts a future migration into a 500 for every authenticated
request that happens to carry an anon cookie: the guard is about a
developer's mistake, and the request is the wrong place to discover
it. At boot the same mistake fails the deploy, which is where a
rollback is the available answer, and no user is served a broken
request in the meantime. The merge itself then walks the static
policy map.

The cascade guard below is different and stays inside the
transaction: it is about THIS merge's data, it costs one COUNT per
table, and its failure rolls back rather than propagating.

### The policy map

Order matters: parents before children, and the `users` delete last.

| # | Table | Rule | Why |
| --- | --- | --- | --- |
| 1 | `decks` | REASSIGN, with slug de-collision | `UNIQUE(user_id, name)` is the only unique constraint in the whole merge that can fire. See below. |
| 2 | `questions` | REASSIGN | No unique constraint on user. Follows its deck. |
| 3 | `study_sessions` | REASSIGN | `current_grading_workflow_id` points at Temporal; harmless either way. |
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
target (`prep/auth/repo.py`, `get_desired_retention`), and the
scheduler already used it to compute the `next_due` values on the
anonymous user's `cards`. Dropping it leaves intervals that were
computed at one retention target being extended at another, with
nothing in the UI saying so. That is a silent scheduling change, not a
cosmetic one.

So step 12, inside the same transaction and immediately before the
delete:

```sql
UPDATE users SET
  desired_retention = COALESCE(desired_retention, :anon_desired_retention),
  editor_input_mode = COALESCE(editor_input_mode, :anon_editor_input_mode)
WHERE tailscale_login = :target
```

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
`PRAGMA table_info(users)` matches its rows exactly, so a new column
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
retry on `sqlite3.IntegrityError`. There is no exhaustion branch. The
alternative (raise and roll back the whole merge) makes a user with a
hundred same-named decks permanently unmergeable: every retry hits
the same wall, the cookie is kept forever by the rule above, and the
decks never arrive. Dropping a deck to resolve a slug collision is
still forbidden; a random suffix resolves it without losing anything
but a pretty URL.

### Atomicity

One `BEGIN IMMEDIATE` transaction for steps 1 through 13. Partial
failure rolls back: the anonymous user still owns everything, the
cookie is still valid, and the next authenticated request retries.
`PRAGMA foreign_keys` stays ON throughout; the merge only UPDATEs
column values and DELETEs rows, and never rebuilds a table, so the
`DROP TABLE`-cascade hazard documented in `db.init()` does not apply.

### What these transactions need from sqlite, and do not get today

`prep/infrastructure/db.py`'s `_connect` sets `PRAGMA foreign_keys`
and nothing else. Two consequences the claims above depend on:

**The database is on the rollback journal, not WAL.** Under the
rollback journal a writer's EXCLUSIVE lock excludes READERS too, so
the merge's transaction blocks every concurrent request that reads
the DB, not just other writers. The whole design assumes the merge is
invisible to everyone else.

**The busy timeout is the driver default, 5 seconds, and it raises.**
Once it lapses, sqlite3 raises `OperationalError` rather than waiting.
So the Idempotency section's "the second transaction blocks on the
first's write lock" is only true inside that window.

Both are fixed at the bottom of the stack, not worked around here:

- `db.init()` executes `PRAGMA journal_mode = WAL` once. The mode is a
  property of the database FILE, not the connection, so one execution
  is permanent and every later connection inherits it. Readers stop
  blocking on the writer and vice versa; writer-versus-writer is the
  only remaining contention, which is what `BEGIN IMMEDIATE` and the
  busy timeout are for.
- `_connect` executes `PRAGMA busy_timeout = 5000` explicitly, next to
  the `foreign_keys` pragma. Same value as the driver default, stated
  in the code so it is a decision rather than an inherited accident,
  and so raising it later is a one-line change with a visible before.

Blast radius: WAL is deploy-wide and one-way in practice. It creates
`-wal` and `-shm` files beside the database, so the volume must be
writable (both deploys mount one) and a backup that copies only the
main file is no longer sufficient. Rolling the image back is safe:
any sqlite build the app has ever run on opens a WAL database.

What survives if WAL is NOT enabled: nothing about correctness. The
merge still rolls back cleanly, and a lapsed busy timeout raises
`OperationalError` into `_try_merge_anon_cookie`'s blanket `except`,
which logs and keeps the cookie, and the next authenticated request
retries. The cost is availability, not integrity, which is precisely
the trade that section already refuses to make.

### The cascade guard

`DELETE FROM users WHERE tailscale_login = :anon` cascades. If any
table were missed, that one statement silently destroys its rows.
So, inside the transaction and immediately before it:

```
for table, column in policy_map:
    if count(table where column = anon_id) != 0:
        raise LeftoverAnonRows(table)
```

A non-zero count rolls back the whole transaction and returns
`resolved=False`, so the anonymous user still owns everything and the
cookie still points at them. The delete is then provably a no-op for
every cascade path, by construction rather than by inspection.

### Idempotency

- Anon row already gone: `resolved=True, merged=False`.
- Re-running after success: same, because step 13 removed the row.
- Concurrent duplicate requests carrying the same cookie: the second
  transaction blocks on the first's write lock for up to the busy
  timeout, then finds no anon row and returns
  `resolved=True, merged=False`. Exactly one merge happens, both
  responses clear the cookie, and only the first shows the toast. If
  the timeout lapses first the second request raises instead, the
  blanket `except` keeps its cookie, and the retry lands on the same
  no-anon-row answer. Either way exactly one merge happens.

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
against the snapshot's `user.id` (`prep/offline/routes.py:38`, the
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

A task on the existing scheduler tick (`prep/notify/scheduler.py`),
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

**It runs in a worker thread.** `_tick` (`prep/notify/scheduler.py:89`)
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
by `prep/infrastructure/db.py`'s `now()`, which emits an aware
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
(`prep/instant/repo.py:92`) takes `user_id` and grows a per-user
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
`signed_in_user`. Uncapped `upload.read()` into a cookie identity has
no ceiling worth computing.

Three levers, in the order to reach for them: the per-account row cap
(smallest blast radius), the anonymous retention window, then the
global daily mint cap. Reviews grow the typical number over time, and
that growth is the product working.

---

## 7. Privacy

The cookie is a persistent identifier. `templates/privacy.html` gains
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
stores `ip` (`prep/infrastructure/db.py:689`), and section 6 adds
`user_id` to the same row, which creates a durable IP-to-account-to-decks
join that did not exist before this spec. Section 6's per-user window
needs it. `RETENTION_DAYS = 7` (`prep/instant/repo.py:29`) with the
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

## 8. What gets deleted

### Client

`static/js/modules/instant-start.js`: `readGuestState`,
`renderContinueStrip`, `writeDeck`, `showSaveError`, the
`@/offline/store.js` and `@/offline/scheduler.js` imports, the
replace-confirm, the `data-instant-continue` handling. What remains is
the four-state form plus a redirect.

`static/js/offline/sync.js`: the `adoptionApproved` latch,
`adoptGuestData`, `discardGuestData`, `showAdoptionDialog`,
`adoptionBodyText`, `initAdoption` and its wiring in `init()`, the
`meta.guest` test inside `guestAdoptionPending()`, its call site at
the top of `refreshSnapshot`, and the opportunistic
`remove("meta", "guest")` sweep in the refresh path.

`init()`'s leading `initAdoption()` gate is not simply removed: it is
REPLACED by an awaited `wipeLegacyGuestData()` (section 9). Removing
it and leaving `flushOutbox` first is the data-safety regression that
wipe exists to prevent.

**What survives, renamed.** `guestAdoptionPending()` is two guards
wearing one name, and only one of them is about guests. Its own
comment states the invariant to preserve: "Absent is adoptable,
mismatch refuses; they are different answers." Deleting the function
outright would delete the ABSENT answer and keep only the mismatch
one, and a device holding owner-absent `local_cards` would then flush
them into whichever account signed in next, with no dialog and no
guard. That device is not hypothetical: it is a signed-in user who
authored cards offline before their first snapshot refresh, and
section 9 already treats those rows as data that must survive.

So the owner-absent half stays, as `ownerUnstamped()`, with the guest
test removed:

```js
// A flush needs a stamped owner. An unstamped device cannot say
// whose outbox this is, so it refreshes first (which stamps) and
// flushes on the next pass.
async function ownerUnstamped() {
  if (await metaGet("owner")) return false;
  return (await getAll("local_cards")).length > 0;
}
```

`flushOutbox` keeps the gate and, on hitting it, forces a
`refreshSnapshot` rather than simply refusing, so the state closes
itself on the same online load instead of stranding the cards.
`refreshSnapshot` loses the gate entirely: it is the thing that
stamps the owner, and gating it on the absence of an owner is a
deadlock. Refresh replaces the `decks` and `cards` stores only, so
`local_cards` and `outbox_reviews` are untouched by it.

The result is that "absent" stays a distinct, transient, self-closing
state, and "mismatch" stays a hard refusal. After the stamp lands, a
later sign-in by a different person hits the mismatch path, which is
the guard that should handle it.

`static/js/offline/offline-app.js`: `state.guest`, `state.guestNudge`,
`isGuest()`, `guestDeckName()`, the guest overview branch, the guest
deck line, `guestDisclosureLine()`, `guestNudgeBanner()`, the
guest-aware boot gate in `init()`, and the guest reconnect
suppression.

`static/js/offline/store.js`: the `"guest"` and `"guest_nudge"` meta
key documentation.

`static/css/components/offline.css`: `.offline-guest-note`,
`.offline-guest-nudge`, `.offline-guest-nudge-copy`,
`.offline-guest-nudge-actions`, and the `.offline-adoption-dialog`
handle.

`static/css/components/landing.css`: the `.instant-continue*` block.

### Templates

`templates/landing.html`: the continue strip (line 31).
`templates/offline.html`: the `data-sign-in-url` attribute on
`[data-offline-root]` (line 54), with the rest of the sign-in-url
chain below. Line 40 is NOT guest copy: it is a Jinja comment on the
masthead anchor whose "Load-bearing for guests" clause is now stale
prose, so the sentence is reworded and the anchor stays.
`templates/base.html`: nothing deleted; both install gates tighten
(lines 236 and 258).

### Server

`prep/web/pwa.py`: the `sign_in_url` context key on the `/offline`
shell response (line 182) and the three-line comment above it that
explains it as the guest nudges' destination.

The "if no other consumer remains" condition resolves YES, and the
whole chain goes with it. `landing.html` and `study_shell.html` also
render a `sign_in_url`, but they get theirs from their own routes;
this one is scoped to the offline shell. Dead once the nudges are
gone: the pwa.py context key, `templates/offline.html:54`, the
`signInUrl` module variable (`static/js/offline/offline-app.js:64`),
the `root.dataset.signInUrl` read that fills it (`:707`), and its
three readers, all inside blocks section 8 already deletes:
`guestDisclosureLine` (`:344-346`), the nudge CTA href (`:518`), and
the `isGuest() && signInUrl` render condition (`:548`).

Nothing else. There is no guest machinery on the server today, which
is the shape of the problem this spec fixes.

### Docs

`docs/INSTANT-START.md` sections 2 (the guest study surface, the
account nudge, sign-up and adoption), 3.3 and 3.4 are replaced by a
pointer here. Sections 1, 3.1, 3.2 and 3.5 stay.

### Tests

- `tests/e2e/test_offline_adoption_e2e.py`: deleted whole.
- `tests/e2e/test_instant_start_e2e.py`: rewritten. The anonymous
  loop now ends on `/deck/<slug>`, studies a card through the normal
  loop, and asserts the dashboard renders on the next visit.
- `tests/offline/test_sync.py`: guest and adoption cases deleted;
  owner-mismatch cases kept untouched.
- `tests/web/test_landing_instant.py`: the two-gate tests stay; the
  continue-strip test goes.

### The boundary with what stays

Everything in OFFLINE.md stays: the IndexedDB stores, `meta.owner`,
the owner-mismatch conflict dialog, the outbox, `flushOutbox`,
`refreshSnapshot`, `offline_sync_idempotency`, the local scheduler
and grader, and the offline shell itself. An anonymous user studying
offline is a normal owner device whose owner id happens to start with
`anon:`.

Two edits inside that machinery are required by this spec, and they
are the only two:

1. `ownerAllows` accepts a match against the snapshot's
   `user.previous_ids` and re-stamps `meta.owner` when it hits one
   (section 5). Without this the happy path of the whole feature,
   an anonymous user who studied offline signing up, ends at a dialog
   offering to wipe their own unflushed work.
2. `guestAdoptionPending` becomes `ownerUnstamped` and keeps guarding
   `flushOutbox` only (above).

Two cases fall out of those, and they get different answers on
purpose:

- **Signed in after a merge**, on a device stamped with the anon id.
  The id is in `previous_ids`, the stamp is rewritten, sync continues
  silently. No dialog: the two ids ARE the same person and the server
  can prove it.
- **Lost the cookie, then signed in.** No merge happened, so nothing
  names the anon id, and the device hits the EXISTING owner-mismatch
  flow. That is correct behaviour from an unchanged guard, and the
  dialog's existing copy covers it. The offline data on that device
  is genuinely orphaned; the dialog is the honest way to say so.

---

## 9. Migration

Anyone on staging holding a guest deck in IndexedDB: **accepted
loss.** prod has never shipped the guest flow, so no real user has
guest data; staging is the operator's own devices.

The new build wipes it, once, under one condition, exported from
`sync.js` so both boot paths run the same code:

```js
// sync.js
export async function wipeLegacyGuestData() {
  if (!(await metaGet("guest"))) return false;
  await withLock(async () => {
    for (const c of await getAll("local_cards")) await remove("local_cards", c.client_id);
    for (const r of await getAll("outbox_reviews")) await remove("outbox_reviews", r.client_id);
    await remove("meta", "guest");
    await remove("meta", "guest_nudge");
  });
  return true;
}
```

**It must run before anything reads those stores, on EVERY boot path.
Putting it only on the offline app's boot leaves the larger path
uncovered.** `sync.js init()`
(`static/js/offline/sync.js:835`) runs on every ONLINE page, lazily
imported by `app.js`, and its first act after the adoption gate is
`flushOutbox()`. Deleting `guestAdoptionPending` (section 8) removes
the only thing holding that chain. A browser carrying staging guest
data that loads any online page before it ever opens `/offline` would
then flush those guest cards and reviews straight into whichever
account just signed in. That is silent adoption without a dialog. It
is not the "accepted loss" this section is agreeing to; it is the
adoption flow the spec deleted, running by accident.

So:

- `sync.js init()` awaits `wipeLegacyGuestData()` as its FIRST
  statement, ahead of `flushOutbox`. The chain proceeds afterward
  either way.
- `offline-app.js boot()` (`static/js/offline/offline-app.js:705`)
  awaits the same imported function before it reads `local_cards` or
  `meta`, so a device that reaches `/offline` first is wiped there.
- Neither call site reimplements it. One function, one gate, one
  place the `"guest"` meta key is still named (which is what makes
  section 10's grep gate expressible).

The `meta.guest` condition is load-bearing and the only safe gate.
Owner-absent `local_cards` WITHOUT `meta.guest` is a different state:
a signed-in user who authored cards offline on a device that has never
completed a snapshot refresh. Those rows must survive and flush on the
next authenticated load, via the stamping refresh that `ownerUnstamped`
forces (section 8). Wiping on owner-absence alone would destroy them.
This is the same absent-versus-guest distinction section 8 preserves,
enforced at a second site; both have to hold, and each has its own
test.

A claim endpoint that uploads a guest deck into a fresh anonymous
account was considered and rejected: it is a new anonymous write path,
with its own abuse surface and its own failure modes, built to rescue
decks the operator generated while testing. Not worth it.

---

## 10. Milestones

Each is shippable and independently verifiable. `make lint test`
gates every one; `make e2e` gates M2, M3 and M6.

### M1: Identity, minting nothing

Schema (migrations 25 and 26), the WAL and busy-timeout pragmas, the
cookie codec, `is_anonymous` on `ResolvedUser`,
`AnonymousFallbackProvider`, the `identity.py` branch,
`UserRepo.touch`, the stale-cookie and refresh middleware block. No
route mints yet, so no user-visible change.

Tests:
- codec: round trip; tampered signature rejected; tampered id
  rejected; future `iat` rejected; expired `iat` rejected;
  wrong-secret rejected; garbage strings rejected without raising.
- rolling window: a cookie whose `iat` is 31 days old resolves the
  same user AND yields a re-minted value with the same `id`, a fresh
  `iat` and a valid signature; one 29 days old resolves with no
  re-mint; a cookie re-minted at day 31 still verifies at day 200,
  which a Max-Age-only refresh would fail (the regression this rule
  exists to prevent).
- middleware: both the stale-cookie delete and the refresh
  `Set-Cookie` are emitted on a JSON response, not only on
  `text/html`.
- pragmas: a fresh database reports `journal_mode = wal` after
  `db.init()`, and a connection from `_connect` reports a non-zero
  `busy_timeout`.
- secret resolution: explicit env wins; HKDF fallback derives
  deterministically and differs from the master key; neither set
  disables anonymous accounts entirely.
- provider precedence, all four steps: a signed-in provider result
  WINS over a present, valid `prep_anon` cookie (the single most
  important assertion in M1); a DORMANT session beats the cookie, so
  `resolve()` returns None and `GET /` renders `reauth.html` even
  with a valid cookie present; the cookie resolves only when the
  provider returns None AND no dormant session exists; a forged
  cookie resolves None and marks the request stale; `urls()` and
  `has_dormant_session()` delegate unchanged.
- repo: `touch` bumps `last_seen_at` and inserts nothing;
  `optional_current_user` on a stale cookie does NOT resurrect a
  deleted row (the regression this branch exists to prevent).

### M2: Mint plus server-side deck

`create_instant_deck`, the shared slug constants. The instant route
creates the deck server side, sets the cookie, returns a redirect.
`instant-start.js` follows it. `GET /` renders the dashboard for a
cookie-bearing visitor.

Tests:
- route: successful generation mints exactly one user and one deck;
  a failed generation mints nothing; a rate-limited request mints
  nothing; an invalid topic mints nothing.
- route: a request carrying a valid cookie reuses the account and
  sets no new cookie unless the rolling window is due; a request
  carrying a cookie for a deleted user mints a fresh one; a signed-in
  request mints nothing and creates the deck under the provider id.
- repo atomicity: inject a failure at the third card's insert and
  assert ZERO `users`, `decks`, `questions` and `cards` rows survive.
  The same test against a sequence of `DeckRepo.create` plus
  `QuestionRepo.add` calls leaves a partial deck behind, which is why
  `create_instant_deck` exists.
- repo: the row cap is enforced from inside the transaction. A
  returning anonymous user at 5 decks is refused a sixth, and the
  refusal writes nothing.
- route: two decks from one anonymous user with the same topic get
  distinct slugs, both matching the opaque 8-character shape, neither
  derived from the topic text.
- route: a 500-character topic and an all-non-Latin topic both yield
  a normal-length opaque slug and a truncated `display_name`.
- crawler: `GET /`, `/healthz`, `/manifest.json` and `/privacy` with
  no cookie create zero `users` rows.
- ledger: the minting request's row starts with a NULL `user_id` and
  is back-stamped by the `ok` resolve; a returning anonymous
  visitor's request carries the id at reserve time; a crash between
  mint and resolve leaves a `pending` row that still counts per IP.
- e2e (browser): type a topic, land on `/deck/<slug>`, study one card
  through the normal loop, reload `/` and get the dashboard.

### M3: One UI

The `signed_in_user` dependency and its route groups (including the
three import routes), the per-account row cap, the funded-workflow
guard, the chip panel exception, "Forget this device", the
install-nudge gate, the Clerk bootstrap flag, the `agent_for_user`
anonymous rule.

Tests:
- each gated route returns 303 (HTML) or 403 (JSON) for an anonymous
  user and 200 for a signed-in one, import routes included. Driven
  from the exception-2 table so every row is asserted.
- `/settings/editor` and `/settings/srs` return 200 for an anonymous
  user, and `GET /notify/vapid-public-key` returns 200 with no user
  at all (gating it would break the service worker).
- the chip panel rendered for an anonymous user contains Scheduling,
  Editor, "Forget this device" and the create-account link, and
  contains NO link whose href is a route the exception-2 table gates.
  That last assertion is written against the rendered HTML, so a
  future panel entry pointing at a gated route fails it.
- neither install entry renders for an anonymous user: the nudge
  partial and the colophon button are both absent from the rendered
  `base.html` (two assertions, because there are two gates).
- the dashboard, deck page and study shell render the SAME templates
  for an anonymous and a signed-in user, asserted by template name
  plus the absence of any anonymous-only markup.
- row cap: an anonymous user is refused the 6th deck and the 201st
  question; a signed-in user is refused neither; a merged account
  (target not anonymous) is refused neither.
- workflow guard: a direct POST to `/decks/new/srs` with
  `action=plan` and to `/decks/new/trivia` as an anonymous user
  starts NO workflow and writes NO `active_workflows` row; the same
  POSTs with `action=empty` still create the deck; a signed-in user
  with `funding_tier_for_user == "none"` is refused identically.
- the Clerk bootstrap flag renders `true` for an anonymous user
  (asserted against the rendered `base.html`, not the expression),
  so the session-recovery bootstrap still runs for a Clerk user
  whose JWT expired on a browser holding `prep_anon`.
- "Forget this device" clears the cookie and leaves the user row and
  its decks intact.
- `agent_for_user` returns `_NoopAgent` for an anonymous user even
  when the free tier is configured, and `funding_tier_for_user`
  reports `none`. Neither branches on the `anon:` prefix: a
  non-anonymous user whose external id happens to start with `anon:`
  still gets the free tier.
- `_agent_context` reports `agent_available` False for an anonymous
  user WITHOUT calling the selector, asserted by monkeypatching
  `agent_available_for_user` to raise.

### M4: Merge

`prep/auth/merge.py`, the audit table, the discovery plus policy map,
the trigger. The heaviest test set in the spec.

Tests:
- **fresh sign-up (the C1 regression).** Sign up a provider id with
  NO pre-existing `users` row and NO webhook having fired, carrying a
  valid cookie. Assert the decks moved. Then assert the merge runs
  after the upsert by checking the target row exists at merge time;
  a merge placed before the upsert makes this test fail and every
  webhook-seeded test pass, which is why the seeding is spelled out.
- **the cookie survives every failure.** For each non-resolved reason
  in the result table (`target_missing`, `not_anonymous`, an injected
  `sqlite3.OperationalError`, an injected internal guard failure):
  assert the response does NOT clear `prep_anon`, the anonymous user
  still owns its rows, and a later request completes the merge.
  Assert the reverse for `anon_missing` and `same_user`: cleared.
- **the merge never 500s.** Monkeypatch `merge_anonymous_into` to
  raise, then request a normal authenticated page with a valid
  cookie. Assert 200 and a correctly rendered page, not a 500.
- **schema drift (the fail-closed guard).** Derive the user-scoped
  table set from the live schema and assert it equals the policy
  map's keys. A future table with a `user_id` column fails this test
  until someone writes its rule. The same assertion runs at boot, so
  also assert `db.init()` raises on a schema carrying an uncovered
  user-scoped table.
- **column drift.** Assert `PRAGMA table_info(users)` matches the
  column-disposition table exactly. Table discovery covers tables,
  not columns, so a new `users` column is invisible to the guard
  above and this is the only thing that catches it.
- **preferences carry over.** Anonymous user sets a retention target
  and an editor mode; target has neither; merge; assert both landed
  on the target and the anonymous row is gone. Then the reverse: a
  target that already set its own keeps its own, and the anonymous
  values are discarded. Then the scheduling consequence: the merged
  `cards` rows keep the `next_due` computed at the anonymous
  retention target, and the account's retention target still matches
  the one that computed them.
- **per table.** For each entry in the map: seed a row under the
  anonymous user AND a row under an unrelated third user; merge;
  assert the anonymous row moved or dropped per its rule and the
  third user's row did not move.
- **derived tables.** Seed `cards`, `reviews`,
  `study_session_answers` and `trivia_queue` under the anonymous
  user; assert they are readable through the target user's repo
  accessors after the merge (they moved with their parents) and that
  the merge issued no statement against them.
- **slug collision.** Target owns `french-revolution`; anonymous owns
  `french-revolution`. Both survive, slugs differ, both display names
  are unchanged, both deck pages resolve.
- **slug exhaustion does not wedge the merge.** Target owns
  `x` plus `x-2` through `x-100`; anonymous owns `x`. The merge
  completes with a random suffix, and no deck is dropped.
- **previous ids reach the client.** After a merge,
  `GET /api/offline/snapshot` carries the anon id in
  `user.previous_ids`; an unmerged account carries an empty list; a
  third user's merge never appears in someone else's snapshot.
- **idempotency.** Merge twice. The second is a no-op, row counts are
  identical, and each attempt appends its own audit row.
- **atomicity.** Monkeypatch a failure at the last reassign step.
  Assert full rollback: the anonymous user still exists and still
  owns every row, the target gained nothing, and the audit row is
  `started` with no completion. Then let the retry succeed.
- **cascade guard.** Create a synthetic table with an FK to
  `users(tailscale_login)` that the policy map does not cover. Assert
  the merge raises before writing anything, and that the anonymous
  user is not deleted.
- **trigger.** A request carrying both a resolved Clerk identity and
  a valid cookie merges once and clears the cookie; the next request
  does nothing; a request with only the cookie does not merge; the
  Clerk webhook does not merge.
- **concurrency.** Two threads issue the same authenticated request
  with the same cookie against one DB file. Exactly one merge
  completes, no duplicate rows, no exception surfaces to either
  request.
- **review history end to end.** Anonymous user studies three cards
  with mixed verdicts; sign in; assert the reviews, the FSRS
  `stability` / `difficulty` / `fsrs_state` and `next_due` are
  unchanged under the new owner.
- **secrets never move.** Seed `byok_credentials` and `api_tokens`
  rows under the anonymous user by direct SQL (bypassing the
  capability gate). Assert both are DELETED, not reassigned.

### M5: Lifecycle and abuse

`instant_generations.user_id`, per-user windows, the reaper, the
privacy section.

Tests:
- limiter: per-user daily window refuses at the cap while a fresh IP
  is used; per-IP window refuses at the cap while a fresh cookie is
  used; both must pass to admit.
- limiter: the account-minting request reserves with `user_id=None`
  and is back-stamped on resolve, so the SECOND generation from that
  account sees a count of 1 in its per-user window, not 0.
- limiter: an anonymous user's spend rows follow them through a merge
  and still count against the signed-in daily window that day.
- reaper: deletes only anonymous rows past the cutoff; a row touched
  yesterday survives; a signed-in row past the cutoff survives; the
  non-FK tables are emptied for the deleted id and untouched for
  others.
- reaper: uses the same policy map, asserted by the same drift test
  as M4.
- reaper: 120 eligible accounts leave 70 behind after one pass and
  clear over three, so the `LIMIT` is real and the backlog drains.
- reaper: a row whose `last_seen_at` was written by `now()` at
  exactly the cutoff instant is NOT deleted. The same test with a
  `Z`-suffixed cutoff deletes it, which is the formatter bug the rule
  exists to prevent.
- reaper: the tick does not block the loop. Assert the reap runs
  through `asyncio.to_thread` (a coroutine scheduled alongside it
  makes progress while a slow reap is in flight).

### M6: Delete the guest machinery

Every removal in section 8, plus the one-time IDB wipe.

Tests:
- the wipe fires when `meta.guest` exists and clears cards, outbox
  and both meta keys.
- the wipe does NOT fire for owner-absent `local_cards` with no
  `meta.guest`, and those cards still flush on the next authenticated
  load (the regression this gate exists to prevent).
- the wipe runs BEFORE the flush on the online path. Seed guest data,
  call `sync.js init()` with a spy on the flush transport, and assert
  the transport was never given a guest card or review. Reordering
  the two statements fails this test, which is the silent-adoption
  regression it exists to prevent.
- the wipe runs on the offline path too: seed guest data, call
  `offline-app.js boot()` without ever loading an online page, and
  assert the stores are clear.
- `ownerUnstamped` blocks a flush on an owner-absent device holding
  `local_cards`, the forced refresh stamps `meta.owner`, and the
  cards flush on the next pass. Then a sign-in by a DIFFERENT user
  hits the mismatch dialog rather than absorbing them.
- `ownerAllows` accepts an id listed in `previous_ids`, re-stamps
  `meta.owner` to the current server id, and returns true; an id in
  nobody's `previous_ids` still disables sync and raises the dialog;
  a client-supplied `previous_ids` is ignored (the server payload is
  the only source).
- e2e: an anonymous user's `/offline` device behaves as a normal
  owner device: snapshot refresh stamps `meta.owner` with the
  `anon:` id, flush works, no adoption dialog exists in the DOM.
- e2e (the C3 regression): an anonymous user studies offline with
  unflushed reviews and an unflushed local card, signs in, and the
  next online load flushes cleanly with NO conflict dialog in the
  DOM and no data lost.
- grep gate in CI: no occurrence of `guestAdoption`,
  `adoptionApproved` or `guestNudge` anywhere, and no occurrence of
  the `"guest"` meta key outside the one-time wipe in section 9 (the
  wipe has to name the key it is removing, so the gate names the
  wipe's file and line as its single allowed site, and fails if the
  count there changes).

---

## 11. Follow-ups, explicitly not this spec

- **Claim link.** A one-shot signed URL that moves an anonymous
  account onto another device or into the iOS PWA jar. Fixes both
  the different-device merge gap and the install gate.
- **Anonymous free tier beyond one endpoint.** Needs a spend
  reservation the Go worker can participate in.
- **Upload caps on deck import.** `raw = await upload.read()` reads
  the whole body with no size cap for signed-in users too
  (`prep/decks/routes.py:1874, 1940, 2001`). This spec removes the
  anonymous exposure by gating the routes, and does not fix the
  underlying route. A streaming read with a byte ceiling is the fix.
- **Merge undo.** The audit row makes it possible; nothing consumes
  it yet.
- **Anonymous trivia decks.** The instant deck is SRS only.
