# prep-app — working notes

Interview-prep flashcard tool with Claude-generated questions and SM-2-style spaced
repetition. Multi-user via Tailscale identity. Web push notifications for digest /
when-ready triggers (per-user opt-in). Installs as a PWA on iOS / Android.

Current tag: see `git describe --tags` in either checkout.

Tag highlights so far:
- v0.1.0 — sessions + CodeMirror + skeletons + UI sweep (initial bootstrap)
- v0.2.0 — UX polish, button gaps, device-aware sessions
- v0.3.0 — multi-user via Tailscale identity, masthead user chip
- v0.3.1 — security: strip Tailscale headers on LAN listener, workflow-route auth
- v0.3.2 — drop boot-seed pollution (no auto-resurrected `owner@local`)
- v0.3.3 — gate legacy-user upsert behind needs-migration check
- v0.4.0 — discuss-this-card popover, Phosphor Light icon system
- v0.4.1 — **FK-safe schema rebuild** (the v0.3.0 migration cascaded and wiped
  prod questions/cards/reviews; this is the fix — see "Schema migrations" below)
- v0.5.0 — notifications (VAPID push, per-user prefs, scheduler, PWA install)
- v0.5.x — settings UX iterations: conditional quiet hours, per-device toggle,
  inline async-button states, opt-in quiet hours w/ native time pickers
- v0.5.6 — clean error pages

## Deploy convention — staging-first, tag-based

The author runs prep with a parallel staging+prod checkout convention.
This is the definitive flow for the canonical deploy. Forks can use
whatever workflow they like — the codebase doesn't depend on it.

Two parallel checkouts of the same repo:

```
<deploy-root>/prep-app/          ← prod, detached HEAD at a tag (v0.X.Y)
<deploy-root>/prep-app-staging/  ← staging, on branch `main` (rolling)
```

| concern              | prod                              | staging                                   |
|----------------------|-----------------------------------|-------------------------------------------|
| URL                  | tailnet `/prep/`                  | tailnet `/prep-staging/`                  |
| FastAPI port         | 8081                              | 8082                                      |
| pm2 services         | `prep-app`, `prep-worker`         | `prep-app-staging`, `prep-worker-staging` |
| data.sqlite          | live user data                    | seeded copy; refresh as needed            |
| Temporal namespace   | `prep`                            | `prep-staging`                            |
| Temporal devserver   | shared (port 7233 + Web UI 8233) — Web UI shows both namespaces |

**The flow:**

1. Develop in `prep-app-staging/` on `main`. Commit + push as you go.
   • Verify on `/prep-staging/` against your tailnet.
   • Staging always reflects `origin/main` HEAD.
2. When staging is good, **promote by tagging from main:**
   ```
   cd <deploy-root>/prep-app-staging
   git tag -a v0.X.Y -m "release notes"
   git push origin v0.X.Y
   ```
3. **Deploy that tag to prod:**
   ```
   cd <deploy-root>/prep-app
   ./deploy.sh v0.X.Y
   ```
   `deploy.sh` does: `git fetch --tags && git checkout <tag>`, reinstalls Python deps,
   rebuilds the Go worker + cm-bundle, restarts pm2 services. Prints `git describe`
   at the end for confirmation.

**Hotfixes** (when prod has a bug and main has unreleased work you don't want shipping):
1. From the current prod tag: `git checkout -b hotfix-foo v0.X.Y`
2. Fix, push the branch, optionally test in staging by `git checkout` of that branch
3. Tag `v0.X.(Y+1)`, push, deploy
4. Merge hotfix back into `main` so it doesn't get lost on the next promotion

**Rollback:** `cd <deploy-root>/prep-app && ./deploy.sh v0.<previous>`. One command.

**Versioning:** semver — bump minor for features (v0.2.0), patch for fixes (v0.2.1),
major when breaking. Pre-1.0 we're permissive about minor-vs-patch boundaries.

**Initial release: v0.1.0** — sessions + CodeMirror + Telegram channel + UI sweep + skeletons (the cumulative state on 2026-04-27 when staging was bootstrapped).

**Staging data refresh** (when you want a fresh copy of prod data):
```
cp <deploy-root>/prep-app/data.sqlite \
   <deploy-root>/prep-app-staging/data.sqlite
pm2 restart prep-app-staging
```

## Architecture

```
browser ──→ Tailscale Serve :443 (or Caddy :8000 path /prep/) ──→ uvicorn 127.0.0.1:8081 ──→ FastAPI app (Python)
                                                                                                │
                                                                                                ├─ db.py            → data.sqlite (SQLite, WAL)
                                                                                                ├─ grader.py        → deterministic for MCQ/multi; `claude -p` for code/short
                                                                                                ├─ chat_handoff.py  → result-page "Discuss" popover URL builder
                                                                                                ├─ icons.py         → Jinja `icon('name')` global, inlines static/icons/*.svg
                                                                                                ├─ notify.py        → VAPID push + scheduler (asyncio task, 5-min tick)
                                                                                                └─ temporal_client  → starts/queries/cancels GenerateCardsWorkflow
                                                                                                                          │
                                                                                  Temporal devserver 127.0.0.1:7233/8233 (pm2: prep-temporal, SQLite-backed)
                                                                                                                          │
                                                                                  prep-worker (Go, pm2) ───── activities → claude --session-id / --resume + sqlite write

push services (Apple / Mozilla / FCM) ←── pywebpush ←── notify.scheduler tick (per user, per pref mode)
```

**Card generation is durable.** When you click "Apply" on a deck-scope transform, FastAPI starts a `GenerateCardsWorkflow` (or `TransformWorkflow`) on Temporal and 303-redirects you to a polling page. The Go worker (`worker-go/`) executes activities: `PrimeClaudeSession` (one-time) → `GenerateNextCard × N` (resumes the same Claude session for prompt-cache wins) → `InsertCard × N` → `Cleanup`. Per-card retries (3× exp backoff), per-card failure isolation, idempotency via `(workflowID, index)` keys recorded in the `questions_idempotency` table.

**Code/short answer grading is also durable.** Same pattern: POST `/study/{deck}` for `code`/`short` answers (not idk) starts a `GradeAnswerWorkflow` and 303-redirects to `/grading/{wid}`. Two activities: `GradeFreeText` (claude -p shell-out, returns Verdict) → `RecordReview` (DB write + SRS step advance, ported to Go). Idempotency via the workflow ID stored in `grading_idempotency`. The polling page at `/grading/{wid}` re-renders as the existing `result.html` once the workflow completes, so the post-grade UI is identical regardless of whether grading went through the synchronous path (mcq/multi/idk) or the workflow path. Workflow ID format: `grade-<deck>-q<qid>-<rand>` so the polling page can parse the deck + qid back without a side table.

**Worker `claude` invocations use `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`.** This loads NO MCP servers for the spawn. Without it, each invocation re-loads the user's full plugin config and tries to start every MCP child — fine in isolation, but if any of those MCPs is a singleton (e.g., a service that holds a network slot), parallel spawns from the worker race for that slot and break things. The user's bot-style MCPs were the specific case that motivated this; the general principle is "don't drag the user's whole agent environment into a one-shot generation call." **DO NOT use `--bare` for this purpose** — `--bare` ALSO skips OAuth/keychain reads and breaks subscription auth (`Not logged in · Please run /login`). `--strict-mcp-config` only suppresses MCPs and leaves auth intact. See `worker-go/activities/activities.go`.

**Session IDs are minted inside the activity, not derived from workflow ID.** Originally I derived `--session-id` from the workflow ID for determinism, but Claude registers a session ID before fully creating the session — so a half-failed prime attempt left the ID in Claude's registry and retries collided with `Session ID X is already in use`. Fix: `PrimeClaudeSession` mints a fresh UUID per attempt and returns it; the workflow stores the returned ID and passes it to subsequent activities. Temporal's at-least-once delivery means a successful call's ID is recorded in workflow history and re-used on replay.

The legacy synchronous `generator.py` is still on disk but unused by the FastAPI app. Kept for ad-hoc CLI use (`.venv/bin/python generator.py <user> <deck> <count>`).

**Prod ops surface** (the canonical deploy; forks may differ):

- pm2 manages the `prep-app` and `prep-worker` services.
- Caddy fronts the app at `/prep/` → `127.0.0.1:8081`. Uses `handle /prep*` (NOT `handle_path /prep/*`) — `handle_path` strips the prefix before forwarding, but Starlette's `StaticFiles` mount only resolves correctly when the prefix is present in the request path. Stripping it produced 404s on `/prep/static/style.css`. Don't regress.
- uvicorn launched with `ROOT_PATH=/prep` so generated URLs include the prefix.
- DB lives at `data.sqlite` in the repo dir. Per-deployment, gitignored.
- **Primary URL:** `https://<your-tailnet-hostname>/prep/` — HTTPS via Tailscale Serve, valid LetsEncrypt cert via Tailscale's MagicDNS, reachable on your tailnet from anywhere. Set up via `tailscale serve --bg --https=443 http://127.0.0.1:8000` after enabling Serve at the tailnet level (one-click admin toggle in Tailscale's web console). Tailscale Serve persists across reboots — config lives in tailscaled state, re-applies on daemon start. Inspect with `tailscale serve status`; turn off with `tailscale serve --https=443 off`.
- **LAN access:** Caddy binds `:8000` on all interfaces so the same listener handles LAN + tailnet traffic. Tailscale identity headers are stripped from non-loopback ingress (see security gotcha below).

## Multi-user (Tailscale identity)

Auth is **header-based**: Tailscale Serve sets `Tailscale-User-Login` (the user's
tailnet email) on every request that comes through `:443`. The FastAPI dependency
`current_user(request)` reads this header, calls `db.upsert_user(...)` to track
the user, and falls back to the `PREP_DEFAULT_USER` env var if no header is
present. The bundled `make dev` sets `PREP_DEFAULT_USER=dev@example.com` so
contributors don't need Tailscale running just to develop. Prod typically
has it unset, so a header-less request 401s.

All user-owned tables (`decks`, `questions`, `study_sessions`, `cards`,
`reviews`, `push_subscriptions`) carry `user_id` and every db.py accessor takes
`user_id` as the first argument. Cross-user IDOR via guessed IDs is blocked:
`db.get_question(uid, qid)` returns None for someone else's qid even if it
exists. Worker activities also scope SQL by user_id.

**LAN-listener header strip — security gotcha.** Caddy `:8000` is bound on all
interfaces (LAN-accessible). Tailscale Serve forwards to the same Caddy listener
on 127.0.0.1, so they share the underlying upstream. To prevent a LAN client
from forging `Tailscale-User-Login`, the Caddyfile strips that header from any
request whose remote_ip isn't loopback:

```
@external not remote_ip 127.0.0.1/32 ::1/128
request_header @external -Tailscale-User-Login
request_header @external -Tailscale-User-Name
request_header @external -Tailscale-User-Profile-Pic
```

Hit on 2026-04-27 — a verified curl spoof became any user. Don't regress.
See full audit: `docs/oss-readiness.md` and v0.3.1 commit body.

## Schema migrations

`db.init()` is idempotent — it runs on every app boot and brings the schema up
to current. Older DBs missing columns/tables get them added in place.

**FK CASCADE gotcha (v0.4.1 fix).** The decks table is rebuilt during the
multi-user migration to add `UNIQUE(user_id, name)`. The naive pattern —
`DROP TABLE decks; ALTER TABLE decks_new RENAME TO decks;` — cascades through
`questions.deck_id REFERENCES decks(id) ON DELETE CASCADE` and silently wipes
all questions, cards, and reviews. v0.3.0 shipped this and lost a real prod
DB to it. The fix (per https://sqlite.org/lang_altertable.html#otheralter):

```python
c.commit()
c.execute("PRAGMA foreign_keys = OFF")  # MUST be outside any transaction
try:
    c.executescript("BEGIN; ...rebuild...; COMMIT;")
    orphans = c.execute("PRAGMA foreign_key_check").fetchall()
    if orphans: raise RuntimeError(...)
finally:
    c.execute("PRAGMA foreign_keys = ON")
```

Any future table rebuild that drops a referenced table follows the same pattern.

## Notifications

Web Push (VAPID) for "your cards are due" pings. Lives in `notify.py`.

- VAPID keypair stored on disk: `vapid-private.pem` (gitignored) +
  `vapid-keys.json` (public-only metadata). Generated on first call to
  `notify.public_key_b64()`. Browser uses the public key as
  `applicationServerKey` for PushManager subscribe.
- **`pywebpush`'s `vapid_private_key` arg expects either a Vapid01 instance or a
  file path to a PEM**, NOT a PEM string. Hit this on 2026-04-27 with a silent
  catch-all `except Exception` swallowing `ASN.1 parsing error: invalid length`.
  Always pass the Vapid01 object (`Vapid01.from_file(...)`).
- `push_subscriptions` table: `(endpoint PK, user_id FK, p256dh, auth, ...)`.
  One row per device. Subscriptions whose push service returns 404/410 are
  pruned automatically by `notify.send_to_user` (browser uninstalled / user
  revoked permission).
- Per-user prefs in `users.notification_prefs` JSON: `mode` (off|digest|when-ready),
  `digest_hour`, `tz`, `threshold`, `quiet_hours_enabled`, `quiet_start_hour`,
  `quiet_end_hour`, plus scheduler state (`last_digest_date`,
  `last_when_ready_at`).
- Scheduler is an asyncio task launched by `@app.on_event("startup")` →
  `notify.start_scheduler()`. Wakes every 5 minutes, walks
  `db.list_users_with_push_subs()`, evaluates per-mode + quiet-hours rules,
  fires via `notify.send_to_user(...)`. Idempotency via `last_*` fields in
  the same prefs JSON.
- Settings page at `/notify` (linked from user-indicator panel). Uses
  `<input type="time" step="3600">` for hour pickers (native iOS wheel,
  12-hour locale-aware). Quiet hours are opt-in (checkbox) and only apply
  to when-ready mode — daily digest fires at the chosen hour regardless.

## PWA install

The app is installable as a PWA on iOS/Android.

- Manifest at `/manifest.json` (FastAPI route, dynamic — scope/start_url
  follow ROOT_PATH so prep and prep-staging both install correctly).
- Service worker at `/sw.js` (also a route, NOT served from /static/sw.js,
  because the SW's URL determines its scope and we want the whole app
  prefix to be in scope).
- iOS-specific meta tags in `base.html`: `apple-touch-icon`,
  `apple-mobile-web-app-capable`, `apple-mobile-web-app-status-bar-style`,
  `apple-mobile-web-app-title`. Without these, iOS scrapes the `<title>`
  for the home-screen label and screenshots the page for the icon — both
  ugly. The title meta gives a short fixed label ("prep" or "prep
  (staging)") regardless of the current page's `<title>`.
- Icons rendered from the in-app brand-mark "P" via headless chromium —
  see `/tmp/gen_icon.py` history. PNGs at 180/192/512/1024 in
  `static/pwa/`. iOS prefers PNG `apple-touch-icon` over the manifest's
  `icons[]`, so we set both.
- **Claude iOS app universal-link gotcha.** The Claude app's
  apple-app-site-association at `claude.ai/.well-known/...` does NOT claim
  `/new`, so `claude.ai/new?q=...` opens in Safari rather than the app.
  ChatGPT's manifest does claim root, so chatgpt.com handoff works.
  Workaround: the discuss popover offers a "Copy prompt" option that puts
  the markdown on clipboard so the user can paste into the Claude app
  manually.

## Discuss popover

`chat_handoff.py` builds prefilled-message URLs for Claude / ChatGPT and
returns them as a dict the result.html template embeds. Each provider's
URL convention is hardcoded (`claude.ai/new?q=…`, `chatgpt.com/?q=…`).
Long fields are truncated to ~4KB so the URL doesn't blow past mobile
browser caps.

## Icon system

Phosphor Light SVGs (MIT-licensed, https://phosphoricons.com) live in
`static/icons/*.svg`. `icons.py` exposes a Jinja global `icon(name,
class_=...)` that inlines the SVG with `fill="currentColor"` so they
theme via CSS color. To add a new icon:

```sh
curl -o static/icons/<name>.svg \
  https://raw.githubusercontent.com/phosphor-icons/core/main/assets/light/<name>-light.svg
```

(Note the `-light` suffix in the source path — Phosphor's repo organizes
by weight.) Then reference as `{{ icon('foo') }}` in any template. The
icons cache lazily on first call and a missing icon renders empty rather
than breaking the page.

## Error pages

`templates/error.html` + three exception handlers in `app.py`
(StarletteHTTPException, RequestValidationError, generic Exception)
render literary-styled error pages for browser clients while preserving
JSON `{"detail": "..."}` for API clients. The handler dispatches via
`Accept: application/json` header AND a small allowlist of /notify/*
paths that the front-end JS expects to parse as JSON.

## Decks

A "deck" = one company/role the user is prepping for. Decks are declared in
`generator.DECK_CONTEXT` — keyed by deck name, value gives:
- `source`: subdir name under `~/Dropbox/workspace/interviews/`
- `topics`: shared topic dirs to also pull into the generation prompt
- `focus`: a sentence telling Claude what to bias generation toward

The index page enumerates DECK_CONTEXT directly and merges in DB stats per
logged-in user. Configured-but-not-yet-materialized decks render as 0/0; the
row gets created the first time the user navigates to /deck/{name} (which
calls `get_or_create_deck`). v0.3.2 dropped the previous boot-seed block
that pre-created decks for an `owner@local` placeholder user — that was
producing fixture pollution on every restart.

To add a new deck: add an entry to `DECK_CONTEXT`, restart the pm2 process
(`pm2 restart prep-app`), and the deck will appear on the index for every
user. Worker also has a duplicate `DeckContext` map in
`worker-go/activities/activities.go` that needs the same entry — the OSS
readiness doc (`docs/oss-readiness.md`) flags this duplication.

## SRS ladder

Stored in `db.INTERVAL_LADDER_MINUTES`:
`10min → 1d → 3d → 7d → 14d → 30d`, capped.

- Wrong → step resets to 0 (10 min).
- Right → step += 1 (or stays at the cap).
- "I don't know" → graded as Wrong, with feedback "Marked as 'I don't know'".
- "Suspend" → removes the card from rotation (`questions.suspended=1`) without
  affecting other cards. Used for genuinely broken/ambiguous cards. Distinct from
  "wrong" so the SRS isn't polluted by retiring bad questions.

## Generation contract

`generator.generate(deck_name, count)` reads:
1. The deck's source dir (markdown/code/etc files, capped at 30 files × 8KB each).
2. The shared topic dirs from `DECK_CONTEXT[deck].topics`.
3. Every existing prompt for the deck, so Claude can avoid duplicates.

It then shells out to `claude -p` (path: `$CLAUDE_BIN`, defaults to
`~/.local/bin/claude`) and parses a JSON array. Each question becomes one row in
`questions` plus one row in `cards` with `next_due = now()` so it shows up
immediately as due.

If `rubric` comes back as a list, db.add_question normalizes it to a bullet-pointed
string. If `answer` for a `multi` question comes back as a list, it's JSON-encoded
for storage; the grader decodes it back.

## Ops

- **Start/stop:** `pm2 start prep-app` / `pm2 restart prep-app` / `pm2 logs prep-app`
- **Apply ecosystem changes:** `pm2 reload <deploy-root>/ecosystem.config.js && pm2 save`
- **Reload Caddy:** `brew services restart caddy` (NOT `reload` — see ops gotcha).
- **DB peek:** `sqlite3 data.sqlite '.schema'` then `SELECT * FROM questions LIMIT 5;`

### Ops gotcha — `brew services reload caddy` quietly leaves Caddy stopped

Hit during initial setup on 2026-04-26: `brew services reload caddy` completed
"successfully" but Caddy ended up not running, with `launchctl` reporting `state =
not running`. `brew services start caddy` then failed with launchd I/O error 5
(service was already loaded but not active). Recovery:
`launchctl kickstart -p gui/501/homebrew.mxcl.caddy`. Until that bug is
understood, prefer `brew services restart caddy` over `reload`, and verify with
`pgrep -fl caddy` after.

## Files

- `app.py` — FastAPI routes + exception handlers + startup hook
- `db.py` — sqlite schema, idempotent migrations, accessors, SRS state machine
- `generator.py` — claude-p question generator (CLI fallback)
- `grader.py` — deterministic + claude-p grader
- `chat_handoff.py` — discuss-popover URL builder
- `notify.py` — VAPID push + scheduler
- `icons.py` — Jinja `icon('name')` global, inlines static/icons/*.svg
- `dev_preview.py` — `/dev/preview/<template>/<fixture>` routes for UI sweeps
- `temporal_client.py` — Temporal workflow start/query/cancel helpers
- `worker-go/` — Go Temporal worker (durable card generation + grading)
- `templates/` — Jinja2 HTML (base, index, deck, study, result, session,
  generation, grading, notify_settings, error, …)
- `static/style.css` — single stylesheet, light/dark themed via CSS vars
- `static/pwa/` — Phosphor-aesthetic "P" PWA icons (180/192/512/1024 PNG)
- `static/icons/` — Phosphor Light SVG icon set
- `static/sw.js` — service worker (push event handler + tap-to-open)
- `static/cm/` + `static/cm-bundle.js` — CodeMirror 6 build (committed)
- `data.sqlite` — the database. **Gitignored** (`.gitignore`); synced via
  Dropbox so a fresh machine pulls it down. WAL files (-shm/-wal) also gitignored.
- `vapid-private.pem` + `vapid-keys.json` — VAPID keypair, gitignored. Per-deployment.
- `push-subscriptions.json.archived-pre-v0.5` — staging-only artifact from the
  v0.5.0 cutover when subs moved file → DB. Safe to delete.
- `docs/oss-readiness.md` — gap list for going MIT-licensed open source.
- `requirements.txt` — pinned Python deps
- `.venv/` — virtualenv (gitignored)

## Future (not built yet)

- Generation cost cap / per-deck rate limit.
- Optional code-execution sandbox so `code` answers can actually be run, not just
  graded by Claude.
- Per-deck topic filter on the "Add more" form (e.g., generate only behavioral
  cards for cherry).
- ANTHROPIC_API_KEY direct SDK path for users without the Claude CLI (called
  out in `docs/oss-readiness.md` as the biggest blocker for MIT-license
  release).
- Decks-from-config-file: move `DECK_CONTEXT` out of `generator.py` and
  `worker-go/activities/activities.go` into a single `decks.toml` so users
  don't have to edit source to add a deck.
