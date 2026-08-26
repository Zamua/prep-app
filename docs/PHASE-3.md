# Phase 3: the user's data and auth

Spec for phase 3 of `docs/CELLD-REWRITE.md` (2.1, 2.2, 2.5, 2.6, 5.5,
decision 7.4). Lane A (storage) lands first and alone; B (auth and the
merge), C (pages) and D (APIs) then run in parallel with no shared files.
The gate is phase 0's, pointed at the TypeScript server, with the corpora
under `tests/fixtures/parity/` as oracles. Every lane: TDD with vitest,
DDD layering (the layering test stays green), ports and adapters (use
cases call ports; adapters own SQL, fetch, Response, crypto), no operator
context in this repo, terse comments, no em dashes, no push. Corpora are
read-only; a gap is closed in the Python extractor and re-extracted.

## 0. Layout and settled points

```
worker/app/       ports.ts (all ports, A) pageContext.ts (C)
                  auth/{resolve,mergeSaga}.ts (B)  decks/ trivia/ settings/ dashboard/ (C)
                  study/ offline/ notify/ api/{v1,mcp,openapi}.ts instant/ badge/ (D)
worker/runtime/   worker.ts compose.ts (B after A)  routes/{instant,openapi}.ts (D)
                  cells/{UserCell,DirectoryCell,InstantLimiterCell}.ts cells/router.ts (A)
                  cells/routes/{pages,api}.ts (C, D)  cells/seed/{reader,empty,study}.ts (A)
                  adapters/sql/{schema,migrate,*Repo}.ts adapters/random.ts (A)
                  adapters/{clerk,anonCookie,pat,svix,byokCrypto,hkdf}.ts (B)
                  adapters/{webpush,freeTier,runnerStub}.ts (D)
worker/tests/     fakes/sqlStorage.ts fakes/cells.ts (A), one test file per module
tests/parity/     oracles/harness.py remote_app (A); test_ts_contracts.py (D);
                  test_ts_pages.py (C); harness/contextspec.py token header (B)
```

Settled here, to be copied into `CELLD-REWRITE.md` 7.0 and the staging
manifest: **option (c)**. The fake identity provider accepts the
`tailscale-user-*` headers only when `X-Internal-Token` equals
`PREP_INTERNAL_TOKEN` on the same request; the pixel harness
(`contextspec.py: _inject`) and `Harness.headers()` send it. The parity
fleet then holds seeded data behind the seed credential, and no second
fleet is needed.

**Ids are globally unique across cells.** A merge reassigns rows, never
renumbers them (offline devices hold `question_id`s in their outbox), so
per-cell autoincrement is not an option. `DirectoryCell.register` hands
each new cell an index `i >= 1`; the cell seeds `sqlite_sequence` for every
autoincrement table to `i * 2^32`, so ids stay below 2^53 for two million
users and block 0 (ids below 2^32) is reserved for rows migrated in phase
6. The parity seed pins block 0 so seeded ids equal Python's.

**Randomness is a port.** `Random { bytes(n): Uint8Array; choice(seq) }`
and `SessionIds { next(): Promise<string> }`. Production: WebCrypto and
`token_hex(8)`. Under parity: `SeededRandom(20260314)`, a port of CPython's
`random.Random(int)` (MT19937 with `init_by_array` seeding, `getrandbits(k)`
for `k <= 32`, `_randbelow` for `choice`), pinned against Python through
`tests/pyoracle.ts`; and session ids `sha1("parity-session-" + n)[:16]`
with `n` a per-cell counter in storage. Both reset on `POST /_parity/seed`.
This is what makes the seeded slugs, anonymous ids and session ids in the
contracts corpus reproducible.

**The request clock.** Under parity a request carrying `X-Parity-Now`
(ISO) runs on `FixedClock` of that instant: the router strips inbound
`x-prep-now`, sets it from the header, and cells and cookie hooks read
`clockFor(request)`. Outside parity the header is ignored. Contract pairs
recorded after `h.clock.set(...)` replay this way.

## A. Storage (lane A, lands first)

### A1. UserCell schema (`adapters/sql/schema.ts`)

Python's tables minus every `user_id`/`user_login` column, names and
remaining columns unchanged so phase 6 copies rows verbatim:

| table | key | notes |
| --- | --- | --- |
| `profile` | one row | `id` (the subject), `display_name`, `profile_pic_url`, `email`, `created_at`, `last_seen_at`, `is_anonymous`, `notification_prefs`, `editor_input_mode`, `active_byok_provider`, `desired_retention`, `id_base`. Read as the `user` dict with Python's key names (`tailscale_login` = `id`), which the templates and the `pages` corpus pin |
| `decks` | `id` AUTOINCREMENT, `name` UNIQUE | all deck columns of `db.py` |
| `questions` | `id` AUTOINCREMENT, FK `deck_id` CASCADE | `idx_questions_deck` |
| `cards` | `question_id` PK CASCADE | `step, next_due, last_review, stability, difficulty, fsrs_state` written exactly as `ReviewRepo.record` does: `step = step_bucket`, `next_due` = `isoUtc`, `last_review = now`, `fsrs_state` from the scheduler, `step` never read by the scheduler |
| `reviews`, `study_sessions`, `study_session_answers`, `trivia_sessions`, `trivia_queue`, `notifications_log`, `push_subscriptions` (`endpoint` PK), `byok_credentials` (`provider` PK), `api_tokens` (`token_hash` UNIQUE) | as Python | |
| `grading_idempotency` (`idempotency_key` PK), `offline_sync_idempotency` (`client_id` PK), `questions_idempotency` (`idempotency_key` PK, `question_id`) | the three ledgers | the third is the `<job>-insert-N` key phase 4 writes |
| `active_workflows` | `workflow_id` PK | the job status read model, columns as Python |
| `tombstone` | one row | `reason` (`merged`, `reaped`, `deleted`), `at`, `scrubbed_at`, `former_bytes` |
| `schema_version` | one row | |

`migrate(sql)` runs in the constructor under `blockConcurrencyWhile`:
`MIGRATIONS[]` applied above the stored version, each idempotent (`IF NOT
EXISTS`, column checks via `pragma_table_info`), version written last.
The tombstone check precedes every request: a tombstoned cell answers
`{ tombstoned: reason }` and the router turns that into 401 plus a cookie
clear (anonymous) or 404 (others). `deleteAll` wipes `schema_version`, so a
wiped cell re-migrates on its next activation.

`DirectoryCell`: `users(id PK, is_anonymous, created_at, idx UNIQUE)`,
`account_merges` (as Python, `counts` JSON), `merge_markers(anon_id PK,
target_id, audit_id, started_at)`, `tombstones(id PK, reason, at)`. RPC:
`register(id, isAnonymous) -> { idx }` (idempotent), `lookup`,
`beginMerge`, `completeMerge`, `failMerge`, `previousIds(target)`
(`status = 'completed' ORDER BY id`), `tombstone`, `remove`,
`listAnonymous(after, limit)`. `InstantLimiterCell`: `instant_generations`
as Python (`id, ip, created_at, outcome, cards, topic_chars, user_id`) with
both indexes; RPC `reserve(ip, topicChars, userId, userIsAnonymous, at)
-> Reservation | Refusal` running `domain/instant/limiter.checkWindows`
over the rows of the last day after pruning past `RETENTION_DAYS`, and
`resolve(id, outcome, cards, userId)`. Limits from `PREP_INSTANT_*` vars
as Python names them.

### A2. Ports and repositories

`app/ports.ts` declares every port of the phase; the repositories mirror
the Python repos method for method, `user_id` parameters dropped:
`DeckRepo`, `QuestionRepo`, `CardRepo` (`ReviewRepo`'s card-state reads and
writes), `ReviewRepo`, `SessionRepo` (`StaleVersionError`, `advance`,
`_pick_next_question` with the `substr(next_due, 1, 13)` bucket and
`RANDOM()` tiebreak), `TriviaRepo` (queue and sessions), `NotifyRepo`
(log), `PushSubRepo`, `ByokRepo` (ciphertext in, ciphertext out; crypto is
B's), `TokenRepo`, `IdempotencyRepo` (the three ledgers), `PrefsRepo`
(profile columns, `DEFAULT_NOTIFICATION_PREFS` merge, editor modes),
`JobStatusRepo` (`active_workflows`), `ExportRepo` (`dump(): Snapshot`,
`importRows(snapshot, idempotentBy: 'id')`, `wipe()`), plus `Clock`,
`Random`, `SessionIds`, `IdentityProvider`, `Signer`, `Cipher`,
`WebPush`, `AgentPort`, `WorkflowRunner`, `Directory`, `Limiter`,
`UserCells` (stub RPC surface). Adapters in `adapters/sql/` are one class
per port over `ctx.storage.sql`, SQL transcribed from the Python repo, no
business rule (caps, FSRS, grading, merge policy come from `domain/`).
`SqlStorageFake` (`tests/fakes/sqlStorage.ts`, better-sqlite3 as a dev
dependency, boardtogether's shape plus `transactionSync`) backs every repo
test; `tests/fakes/cells.ts` gives in-memory `Directory`, `Limiter` and
`UserCells` fakes for the sagas.

### A3. Cell plumbing, seed, destroy

`cells/router.ts`: `Route { method, pattern, gate: 'user' | 'signedIn' |
'pat', handler }` matched in declaration order; `UserCell.fetch` reads the
identity headers, applies the gate (`SignInRequired` renders as Python's
handler: 303 `/sign-in` for HTML, 403 `{ detail: "sign in required" }` for
JSON; `RowCapReached` as the 429 page or `{ error: { code:
"deck_limit", message } }`), bumps `last_seen_at` (`upsert` with the
claims for provider identities, `touch` for anonymous and PAT never
inserts), then calls the handler, which returns `{ page, context, status,
headers }`, `{ json, status }`, `{ redirect }`, `{ text }` or `{ empty,
status }`; the cell renders pages through `pageContext` (C3) and the
renderer. `cells/routes/{pages,api}.ts` export `Route[]`, empty in A.
`seed(profile)`: `deleteAll`, re-migrate, `id_base = 0`, reset the session
counter, run `cells/seed/<profile>.ts` (the Python profile transcribed,
timestamps from `clockFor`), register in the directory with idx 0, return
Python's seed JSON byte-equal; `anonymous` wipes only. `destroy(reason)`
is the three-step deletion: RPC 1 records `former_bytes`
(`sql.databaseSize`), `deleteAll()`, writes the tombstone (small write);
RPC 2 `scrub()` creates a table, writes `zeroblob` rows to `former_bytes`,
drops it, stamps `scrubbed_at`; both retry-safe, never combined with any
other write. Cell-to-cell RPC errors within 10 s of a node restart are
retried with backoff (spike 6).

## B. Auth (lane B)

- **Clerk** (`adapters/clerk.ts`): kcal's `Verifier` extended. Token from
  `Authorization: Bearer` or the `__session` cookie; RS256 over WebCrypto,
  single-flight JWKS from `CLERK_JWKS_URL`, `iss = CLERK_ISSUER`, `exp`,
  `nbf`, `sub`; `azp`, when present, must be in `CLERK_AUTHORIZED_PARTIES`
  (comma list); claims `email | primary_email`, `name | full_name |
  username`, `picture | image_url` as Python reads them.
  `hasDormantSession(request)`: `__client_uat` present and not `0`.
  `urls()`: `${CLERK_ACCOUNTS_URL}/sign-in?redirect_url=<party[0]>/`
  (`quote_plus`), `/sign-up`, `/sign-out`, `/user`. Public vars
  (`wrangler.{staging,prod}.jsonc`, allow-listed in `wrangler.test.ts`):
  `CLERK_ISSUER`, `CLERK_JWKS_URL`, `CLERK_AUTHORIZED_PARTIES`,
  `CLERK_ACCOUNTS_URL`, `CLERK_PUBLISHABLE_KEY`. Secrets via `CELLD_VAR_`:
  `CLERK_SECRET_KEY`, `CLERK_WEBHOOK_SECRET`, `PREP_ANON_COOKIE_SECRET`,
  `PREP_KEY_ENCRYPTION_SECRET`, `PREP_VAPID_PRIVATE_KEY`,
  `PREP_FREE_INFERENCE_API_KEY`. `clerk_frontend_api_host` decoded from
  the publishable key as `_clerk_bootstrap_context` does, `null` under
  parity.
- **Resolver** (`app/auth/resolve.ts`): signed-in > dormant > cookie >
  visitor, `AnonymousFallbackProvider` transcribed. Under parity the fake
  provider (tailscale headers plus the internal token) precedes Clerk. The
  result names `subject`, `kind` (`clerk | fake | anon | pat`), claims,
  and the cookie verdict (`stale`, `refresh`, or the anon id to merge).
  The router forwards it in `x-prep-*` headers, stripped inbound.
- **Anonymous cookie** (`adapters/anonCookie.ts`, `adapters/hkdf.ts`): the
  `Signer` port is `hmacSha256` over WebCrypto; the key is
  `PREP_ANON_COOKIE_SECRET` (32 hex bytes) or HKDF-SHA256 of the master
  key with `info = "prep-anon-cookie-v1"`, no salt, 32 bytes; neither set
  means anonymous accounts are off (`/api/instant/generate` answers 503
  `not_configured` for a visitor). `domain/anonCookie` supplies parse,
  verify, refresh. `cookieHooks(req, res)` at the composition root
  applies, in Python's precedence: `x-prep-anon-cookie: mint=<id>` from
  the instant use case sets a fresh cookie and drops stale/refresh;
  `clear` (forget-device, sign-out, tombstoned) deletes; else `stale`
  deletes, else `refresh` re-mints. Bytes: `prep_anon=<v>; HttpOnly;
  Max-Age=15552000; Path=/; SameSite=lax; Secure` (`Secure` only on https),
  delete `prep_anon=""; expires=<HTTP date>; HttpOnly; Max-Age=0; Path=/;
  SameSite=lax; Secure`. `_same_origin` (`Sec-Fetch-Site`, else `Origin`
  against `Host`) guards `/forget-device` (403 cross-site) and the
  sign-out clear.
- **Routes in the router**: `/sign-in` (303 to `urls().sign_in`, 404 when
  none), `/sign-out` (Clerk: `sign_out_interstitial.html` with
  `redirect_url: "/"`; other providers 303; 404 when no sign-out URL),
  `POST /forget-device` (303 `/`), `GET /` for a dormant session renders
  `reauth.html` unless `prep_reauth_fallback=1`, 401 on identified routes
  becomes 303 to sign-in for HTML and `{ detail: "not authenticated" }`
  for JSON. `/settings/account` and its delete are 404 unless the provider
  is Clerk; the delete calls `DELETE https://api.clerk.com/v1/users/<id>`
  with the secret key and 303s to sign-out; the webhook does the wipe.
- **Webhooks** (`adapters/svix.ts`, `POST /webhooks/clerk`): svix
  `v1,<b64(HMAC-SHA256(key, "<id>.<ts>.<body>"))>` over WebCrypto, the
  key being the base64 after `whsec_`,
  `svix-id`, `svix-timestamp` within 5 minutes, any listed signature
  matching; 503 without a secret, 400 bad signature, 422 malformed.
  `user.created/updated`: `Directory.register` then `UserCell.upsert` with
  `_primary_email` and `_display_name` rules; `user.deleted`:
  `UserCell.destroy('deleted')`, `Directory.remove`, directory tombstone;
  other types 200 empty.
- **PAT** (`adapters/pat.ts`): `prep_pat_<b64u(sub)>.<b64u(32 bytes)>`;
  the router parses the owner and forwards `x-prep-pat-hash` (SHA-256 hex
  of the whole token); the owner's cell matches `api_tokens.token_hash`,
  touches `last_used_at`, else 401 `invalid or revoked token`; the legacy
  format fails the same way (decision 7.3). Missing or non-Bearer headers
  answer Python's exact `detail` strings. Mask `prep_pat_<secret[:2]>…<secret[-4:]>`;
  the reader seed's fixed secret starts `Pa` and ends `0000` so the
  settings golden holds.
- **BYOK crypto** (`adapters/byokCrypto.ts`, `Cipher` port): AES-256-GCM
  over WebCrypto, 12-byte nonce, no AAD, `base64(nonce || ct || tag)`,
  master key 32 hex bytes; `DecryptionError` on any failure; a Python-made
  ciphertext decrypts (`tests/pyoracle.ts`). Providers: `anthropic-api`,
  `openai-api`, `openrouter-api` with Python's prefixes, labels, console
  URLs, mask; `claude-subscription` is gone (7.4) and a stored row of that
  provider is ignored and offered for deletion on the settings page.
- **Merge saga**: section E.

## C. Pages (lane C)

Every HTML route of the Python inventory that is not a job page, rendered
inside `UserCell` from a `pageContext` built by the use case, same URL,
status, redirect target, `HX-Redirect` and htmx fragment shape:

- Dashboard `GET /`: `index.html` with `dashboard_overview`
  (`overview_payload`), `menu_decks`, `recent_sessions`,
  `active_trivia_sessions`, `snoozed_sessions` (merged, soonest first).
- Deck: `GET /deck/{name}` (`get_or_create`, all states of `deck.html`),
  `POST .../{delete,topic,rename,pin,retention,notifications}` with
  `redirect_back` on pin and notifications and 204 on htmx topic,
  `GET .../edit-with-ai`, `/edit-with-claude` (303 alias), `GET/POST
  .../split`, `GET .../export` (hub page; the file exports are phase 5
  except `export.csv`, D).
- Questions: `GET/POST /deck/{name}/question/new`, `GET/POST
  /question/{qid}/edit`, `POST .../{suspend,unsuspend}` (204 on htmx,
  else 303 to the deck), `POST .../improve` (runner stub, section D).
- New deck: `GET /decks/new` chooser, `GET/POST /decks/new/{srs,trivia}`
  with every `rerender(error)` branch; a generate request creates the
  deck then hits the runner stub and answers as Python does on start
  failure (500 with its message), the deck kept.
- Settings: `/settings/agent` (`byok_sections` for the three providers,
  `free_tier_configured`, connect/disconnect/use with Python's messages
  and statuses 400/503), `/settings/agent/openrouter/{start,callback}`:
  PKCE `S256` verifier stored in a `prep_or_pkce` HttpOnly cookie, 303 to
  `https://openrouter.ai/auth?callback_url=<app_base>/settings/agent/openrouter/callback&code_challenge=..&code_challenge_method=S256`,
  callback exchanges `code` at `POST https://openrouter.ai/api/v1/auth/keys`
  and stores the key as a pasted one; `/settings/{srs,editor}` GET and
  POST, `/settings/api` and `POST /settings/api/tokens`,
  `/settings/api/tokens/{id}/delete` (empty 200 on htmx), `/settings/account`,
  `/notify` and `/notify/log` (marks seen).
- Trivia play: `GET /trivia/session/{deck}` (all three branches, the
  redirect with `cards`/`done`, `session_done.html`), `POST
  .../{answer,override,regrade,abandon,snooze}`, `/trivia/{qid}` and its
  `answer/override/regrade`, `/trivia/decks/{id}/{mute,unmute,
  notifications,interval,session_size}` with `partials/notif_edit.html`.
  Grading is `grade_with_fallback` with `ai_grade` behind `AgentPort`; the
  phase-3 agent stub throws `AgentUnavailable`, so the fallback feedback
  string is Python's unreachable branch. Session refill calls the runner
  stub and proceeds, as Python does on `AgentUnavailable`.
- Study shell: `POST /study/{name}/begin`, `GET /session/{sid}`,
  `POST /session/{sid}/{abandon,snooze}`, `GET /study/{name}`,
  `GET /grading/{wid}` redirects.
- Router pages: landing for a visitor, `reauth.html`, the sign-out
  interstitial, `/privacy`, `/llms.txt` (plain text as `legal.py`).

`app/pageContext.ts` yields the nine processor values from the use case:
`user` (the profile dict), `agent_available` (`false` for anonymous, else
free tier configured or a BYOK row exists), `auth_provider` (`clerk`,
`tailscale` under parity, from the composition), `sign_in_url`,
`sign_up_url`, `sign_out_url`, `clerk_publishable_key`,
`clerk_frontend_api_host`, `notif_unseen_count`, `deck_display` as a
`{ slug: display }` map over the user's decks, `static_css_mtime`,
`app_base`. Group-bys and counts stay in `app/viewmodels/derive`.

## D. APIs (lane D)

- `/api/study/*`: `prep/study/api.py` ported whole (`_session_view`,
  `_submit`, `_record`, `begin`, `next`, `advance`, `draft`, `abandon`,
  `snooze`, `/decks/{name}/{next,submit,session}`, `POST /cards`,
  `GET /grading/{wid}`), `SessionIds` for ids, `domain/grading.grade` for
  mcq/multi/idk. Free-text answers with the agent available call
  `WorkflowRunner.start('GradeAnswer')`; `adapters/runnerStub.ts` throws
  `RunnerUnavailable`, which the use case maps to Python's
  `AgentUnavailable` branch (`selfGrade`). The `{ pending: { poll,
  workflow_id, status, error? } }` shape and `grading_landed` stay in the
  use case for phase 4; `GET /api/study/grading/{wid}` answers `failed`
  with `grading_failed` when no job row exists.
- `/api/dashboard/{overview,deck-menus}`, `/api/active-workflows-badge`
  (`cleanup_stale_terminal` then `list_for_user` over `active_workflows`,
  bucket sort, `partials/workflow_badge.html`).
- `/api/offline/snapshot` (`previous_ids` from `Directory.previousIds`,
  read once per snapshot, never per request) and `/api/offline/sync`:
  `offline/service.py` and `SyncRepo` ported, per-item savepoints via
  `transactionSync`, the ledger keyed by `client_id`, 422 on the caps and
  malformed bodies with pydantic's `detail` list shape.
- `/api/v1/decks*` and `openapi.json`: `api/routes.py` ported, bearer
  only, `decks/io.py` (`deck_to_csv`, `csv_to_deck`, `_questions_for_export`)
  ported here because four pairs and three MCP tools need it (the
  page importers stay phase 5). `runtime/openapi.json` is the recorded
  document verbatim, served at `/openapi.json`; `/docs` and `/redoc` are
  FastAPI's shells as static strings, cross-origin tags stripped under
  parity.
- `/mcp` (`app/api/mcp.ts`): stateless JSON-RPC, `initialize`,
  `notifications/initialized` (204), `tools/list` with the 17 tool
  objects byte-equal to `_TOOLS`, `tools/call`, error codes -32700,
  -32600, -32601, -32602, `_tool_error` and `_tool_text` shapes;
  `prep_export_deck_apkg` and `prep_import_apkg` answer a tool error
  until phase 5.
- `/notify/*`: prefs merge and validation with pydantic's 422 body,
  subscribe (400 without `endpoint`), unsubscribe, `vapid-public-key`
  (`PREP_VAPID_PUBLIC_KEY`, unauthenticated), `test`. `send_to_user`
  appends the log row first, then fans out through the `WebPush` port:
  `adapters/webpush.ts` builds the VAPID JWT (ES256 over WebCrypto, `aud`
  the endpoint origin, `exp` +12 h, `sub` `PREP_VAPID_SUB`), `Authorization:
  vapid t=..., k=...`, RFC 8291 `aes128gcm` (ephemeral P-256, HKDF, one
  record), `TTL: 60`; 404/410 prune, other failures count as `fail`.
  Test: the body round-trips through a subscription keypair under node
  WebCrypto and the JWT verifies with the public key.
- `/api/instant/generate` (`runtime/routes/instant.ts`, in the router):
  Python's sequence and every `kind`: body cap 16 KiB, `sanitizeTopic`,
  `PREP_CLIENT_IP_HEADER` and `limiterBucket`, identity (visitor needs the
  cookie enabled), `Limiter.reserve`, `adapters/freeTier.ts` (`AgentPort`
  over fetch to `PREP_FREE_INFERENCE_BASE_URL`, the same `messages` body
  as `openai_compat.py` so the stub keys match, timeout `generation_timeout_s`),
  `extractCards`, then for a visitor: new id from `Random.bytes(16)`,
  `Directory.register`, `UserCell.createInstantDeck` (profile row with
  `Guest`, `is_anonymous`, the deck with a `SLUG_ALPHABET` slug and cards
  at `step 0, next_due now`), `mint=<id>`; for a user `createInstantDeck`
  under the cap; `Limiter.resolve` last. A crash between steps leaves a
  reservation that expires, never an orphan.

## E. The merge saga (lane B)

`app/auth/mergeSaga.ts: mergeAnonymous(anon, target, deps)`, run by the
router when a signed-in request carries a valid cookie naming a different
id, after the target's upsert and never failing the request:

1. `precheck` (domain): the anon cell reports `{ exists, isAnonymous,
   tombstoned }`, the directory the target; the four refusals and their
   `resolved` flags as Python, `same_user` short-circuits.
2. `Directory.beginMerge` writes the audit row `started` and the marker.
3. `anon.dump()`; `domain/merge.mergeRows(before, anon, target,
   randomHex)` with the target's rows for slug de-collision;
   `target.importRows(after)` idempotent by primary key, caps enforced
   through `domain/limits`, `carryPreferences` COPY-IF-NULL.
4. `Directory.completeMerge(audit, counts)`; a thrown step leaves the
   marker and `started`, and the next request retries from step 3.
5. `anon.destroy('merged')` (A3) and the marker cleared; the cookie is
   marked `stale` only when `resolved`.

`tests/mergeSaga.test.ts` loads `merge/before.json` into two
`SqlStorageFake` cells and a fake directory, runs the saga with `randomHex`
stubbed to `fd58dd`, and compares rows per table (as multisets), the
target profile columns, `target_deck_slugs`, the audit row's counts,
`previous_ids` with `after.json`; then replays step 3 once more and
asserts the target unchanged, and kills the saga between steps 3 and 4
and asserts the retry converges. Account deletion and the reaper share
`destroy`; the reaper itself is phase 4.

## F. Gates

1. Unit, per lane (section H); `npm run typecheck`; `tests/layering.test.ts`.
2. Repositories: every repo test on `SqlStorageFake`; `migrate` twice is a
   no-op; the seed for each profile reproduces the seed JSON of
   `tests/fixtures/parity/pages/<profile>/seed.json` exactly.
3. Contracts: `tests/parity/test_ts_contracts.py` runs
   `contracts.extract(remote_app(PARITY_BASE_URL, token))` (the extractor
   takes its harness; `remote_app` seeds through `/_parity/seed`, sends
   the internal token and `X-Parity-Now` on `clock.set`, and lets
   `follow_redirects=False`) and `compare_pairs` against the corpus.
   `VOLATILE` gains the new PAT shape (`prep_pat_[A-Za-z0-9_.-]+` and the
   `…xxxx` mask) and integer pointers `response.json.decks.*.id`,
   `cards.*.deck_id`, `cards.*.question_id` on the `cookie-*` pairs (block
   ids). Acceptance: 128 of 130 pairs; the two `.apkg` calls are named in
   the test's `PHASE_5` set. Same for `offline.extract`: 13 of 13.
4. Pages: `tests/parity/test_ts_pages.py` replays each of the 36 requests
   of the `pages` corpus against the server, asserting status, headers
   and DOM equivalence of the rendered body with the golden rendered from
   the recorded context (`render_templates` path), so the use case's
   context is pinned through the DOM.
5. Merge oracle (E) and the `SeededRandom` and `Cipher` Python oracles.
6. Pixel: the eleven phase-1 flows and `study`, `trivia`, `offline` (100
   shots, both schemes) against the local node and the staging fleet,
   `PARITY_PHASE=3`, goldens untouched, one file per invocation, the
   fleet deployed from a tag on `main` per `infra/prep/DEPLOY-CELLD.md`.
   Staging carries the new secrets and vars before the deploy (operator
   repo, committed, not pushed).

## G. Out of scope

Durable jobs, the six job pages and their fragments and status routes,
alarms, the reaper, `JobCell` (phase 4); CSV/prepdeck/Anki page
importers, `.apkg` and `.prepdeck` exports, `/metrics` (phase 5);
migration and the block-0 importer (phase 6); the debug endpoints (7.6);
any visual change.

## H. Lanes, files, commands

| lane | owns | run only |
| --- | --- | --- |
| A | `app/ports.ts`, `runtime/adapters/sql/**`, `runtime/adapters/random.ts`, `runtime/cells/**` except `routes/`, `runtime/cells/seed/**`, `runtime/compose.ts` (ports wired to stubs), `tests/fakes/**`, `tests/{schema,migrate,repos.*,cells,seed,random,directory,limiter}.test.ts`, `tests/parity/oracles/harness.py` (`remote_app`), `package.json` | `cd worker && npx vitest run tests/schema.test.ts tests/migrate.test.ts tests/repos tests/cells.test.ts tests/seed.test.ts tests/random.test.ts tests/directory.test.ts tests/limiter.test.ts tests/layering.test.ts && npm run typecheck` |
| B | `app/auth/**`, `runtime/worker.ts`, `runtime/compose.ts` (after A), `runtime/adapters/{clerk,anonCookie,hkdf,pat,svix,byokCrypto}.ts`, `wrangler.*.jsonc` vars, `tests/{clerk,anonCookie,pat,svix,byokCrypto,resolve,router,cookieHooks,mergeSaga,webhooks}.test.ts`, `tests/parity/harness/contextspec.py` (token header), `docs/CELLD-REWRITE.md` 7.0 | `cd worker && npx vitest run tests/clerk.test.ts tests/anonCookie.test.ts tests/pat.test.ts tests/svix.test.ts tests/byokCrypto.test.ts tests/resolve.test.ts tests/router.test.ts tests/cookieHooks.test.ts tests/mergeSaga.test.ts tests/webhooks.test.ts tests/layering.test.ts` |
| C | `app/{decks,trivia,settings,dashboard}/**`, `app/pageContext.ts`, `app/viewmodels/**`, `runtime/cells/routes/pages.ts`, `runtime/adapters/agentStub.ts`, `tests/pages/**`, `tests/parity/test_ts_pages.py` | `cd worker && npx vitest run tests/pages tests/layering.test.ts`; `.venv/bin/pytest tests/parity/test_ts_pages.py -q` against `scripts/run-node.sh` |
| D | `app/{study,offline,notify,api,instant,badge}/**`, `runtime/cells/routes/api.ts`, `runtime/routes/{instant,openapi}.ts`, `runtime/openapi.json`, `runtime/adapters/{webpush,freeTier,runnerStub}.ts`, `tests/api/**`, `tests/parity/test_ts_contracts.py`, `tests/parity/oracles/contracts.py` (`VOLATILE`, harness parameter) | `cd worker && npx vitest run tests/api tests/layering.test.ts`; `.venv/bin/pytest tests/parity/test_ts_contracts.py -q` with `PARITY_BASE_URL`, the LLM stub on `PREP_FREE_INFERENCE_BASE_URL` |

Order: A, then B, C, D in parallel; B lands `worker.ts` with the
`instant`/`openapi` hooks called at fixed positions and `null`-returning
stubs until D replaces them. Integration, once: `cd worker && npx vitest
run && npm run typecheck`, the four pytest gates above, then the pixel
files against the local node, then merge, tag, deploy, and the pixel files
against the fleet.
