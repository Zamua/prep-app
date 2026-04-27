# prep-app — working notes

Interview-prep flashcard tool with Claude-generated questions and SM-2-style spaced
repetition. Manual-only for now (user invokes "Add more" or "Study" via the browser);
no scheduler is wired up yet.

## Deploy convention — staging-first, tag-based

**This is the definitive flow. Every change goes through staging.**

Two parallel checkouts of the same repo:

```
~/Dropbox/workspace/macmini/prep-app/          ← prod, detached HEAD at a tag (v0.X.Y)
~/Dropbox/workspace/macmini/prep-app-staging/  ← staging, on branch `main` (rolling)
```

| concern              | prod                              | staging                                   |
|----------------------|-----------------------------------|-------------------------------------------|
| URL                  | https://example-host.ts.net/prep/         | https://example-host.ts.net/prep-staging/    |
| FastAPI port         | 8081                              | 8082                                      |
| pm2 services         | `prep-app`, `prep-worker`         | `prep-app-staging`, `prep-worker-staging` |
| data.sqlite          | live user data                    | seeded copy; refresh as needed            |
| Temporal namespace   | `prep`                            | `prep-staging`                            |
| Temporal devserver   | shared (port 7233 + Web UI 8233) — Web UI shows both namespaces |

**The flow:**

1. Develop in `prep-app-staging/` on `main`. Commit + push as you go.
   • Refresh on phone, verify on `/prep-staging/`.
   • Staging always reflects `origin/main` HEAD.
2. When staging is good, **promote by tagging from main:**
   ```
   cd ~/Dropbox/workspace/macmini/prep-app-staging
   git tag -a v0.X.Y -m "release notes"
   git push origin v0.X.Y
   ```
3. **Deploy that tag to prod:**
   ```
   cd ~/Dropbox/workspace/macmini/prep-app
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

**Rollback:** `cd ~/Dropbox/workspace/macmini/prep-app && ./deploy.sh v0.<previous>`. One command.

**Versioning:** semver — bump minor for features (v0.2.0), patch for fixes (v0.2.1),
major when breaking. Pre-1.0 we're permissive about minor-vs-patch boundaries.

**Initial release: v0.1.0** — sessions + CodeMirror + Telegram channel + UI sweep + skeletons (the cumulative state on 2026-04-27 when staging was bootstrapped).

**Staging data refresh** (when you want a fresh copy of prod data):
```
cp ~/Dropbox/workspace/macmini/prep-app/data.sqlite \
   ~/Dropbox/workspace/macmini/prep-app-staging/data.sqlite
pm2 restart prep-app-staging
```

## Architecture

```
browser ──→ Tailscale Serve :443 (or Caddy :8000 path /prep/) ──→ uvicorn 127.0.0.1:8081 ──→ FastAPI app (Python)
                                                                                                │
                                                                                                ├─ db.py            → data.sqlite (SQLite, WAL)
                                                                                                ├─ grader.py        → deterministic for MCQ/multi; `claude -p` for code/short
                                                                                                └─ temporal_client  → starts/queries/cancels GenerateCardsWorkflow
                                                                                                                          │
                                                                                  Temporal devserver 127.0.0.1:7233/8233 (pm2: prep-temporal, SQLite-backed)
                                                                                                                          │
                                                                                  prep-worker (Go, pm2) ───── activities → claude --session-id / --resume + sqlite write
```

**Card generation is durable.** When you click "Generate N", FastAPI starts a `GenerateCardsWorkflow` on Temporal and 303-redirects you to `/generation/{wid}` which polls the workflow's `getProgress` query every 2s. The Go worker (`worker-go/`) executes activities: `PrimeClaudeSession` (one-time) → `GenerateNextCard × N` (resumes the same Claude session for prompt-cache wins) → `InsertCard × N` → `Cleanup`. Per-card retries (3x exp backoff), per-card failure isolation, idempotency via `(workflowID, index)` keys recorded in the `questions_idempotency` table. **No Telegram notification on completion** — the in-app status page is the only progress UI (per user preference).

**Code/short answer grading is also durable.** Same pattern: POST `/study/{deck}` for `code`/`short` answers (not idk) starts a `GradeAnswerWorkflow` and 303-redirects to `/grading/{wid}`. Two activities: `GradeFreeText` (claude -p shell-out, returns Verdict) → `RecordReview` (DB write + SRS step advance, ported to Go). Idempotency via the workflow ID stored in `grading_idempotency`. The polling page at `/grading/{wid}` re-renders as the existing `result.html` once the workflow completes, so the post-grade UI is identical regardless of whether grading went through the synchronous path (mcq/multi/idk) or the workflow path. Workflow ID format: `grade-<deck>-q<qid>-<rand>` so the polling page can parse the deck + qid back without a side table.

**Worker `claude` invocations MUST use `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`.** Without disabling MCPs, each spawned `claude` would re-load the user's full plugin config and try to start its own Telegram MCP child. Telegram allows exactly one `getUpdates` poller per bot token, so the worker spawns race for the slot with the channel-mode Claude's MCP and one of them dies — usually the channel-mode one. Hit on 2026-04-26: a 5-card batch spawned 6 bun MCPs in 46 seconds and took down the user's Telegram MCP for the rest of the session. **DO NOT use `--bare` for this purpose** — `--bare` ALSO skips OAuth/keychain reads and breaks subscription auth (`Not logged in · Please run /login`). Tried it, regressed, reverted same day. `--strict-mcp-config` only suppresses MCPs and leaves auth alone. See `worker-go/activities/activities.go` comments.

**Session IDs are minted inside the activity, not derived from workflow ID.** Originally I derived `--session-id` from the workflow ID for determinism, but Claude registers a session ID before fully creating the session — so a half-failed prime attempt left the ID in Claude's registry and retries collided with `Session ID X is already in use`. Fix: `PrimeClaudeSession` mints a fresh UUID per attempt and returns it; the workflow stores the returned ID and passes it to subsequent activities. Temporal's at-least-once delivery means a successful call's ID is recorded in workflow history and re-used on replay.

The legacy synchronous `generator.py` is still on disk but unused by the FastAPI app. Kept for ad-hoc CLI use (`.venv/bin/python generator.py cherry 5`).

- pm2 process name: **prep-app** (defined in `~/Dropbox/workspace/macmini/ecosystem.config.js`).
- Caddy route: **`/prep/`** → `127.0.0.1:8081`. Defined in `~/Dropbox/workspace/macmini/Caddyfile`. Uses `handle /prep*` (NOT `handle_path /prep/*`) — `handle_path` strips the prefix before forwarding, but Starlette's `StaticFiles` mount only resolves correctly when the prefix is present in the request path. Stripping it produced 404s on `/prep/static/style.css`. Don't regress.
- uvicorn launched with `ROOT_PATH=/prep` so generated URLs include the prefix.
- DB lives at `data.sqlite` in this directory. Backed up via Dropbox (whole project is in Dropbox).
- **Primary URL:** `https://example-host.ts.net/prep/` — HTTPS, no port, valid Let's Encrypt cert, reachable on the user's tailnet from anywhere. Set up via `tailscale serve --bg --https=443 http://127.0.0.1:8000` after enabling Serve at the tailnet level (one-click admin toggle, already done). Tailscale Serve persists across reboots — config lives in tailscaled state, re-applies on daemon start. Inspect with `tailscale serve status`; turn off with `tailscale serve --https=443 off`.
- **LAN URLs:** `http://example-host.local:8000/prep/` (mDNS) or `http://192.0.2.27:8000/prep/` (IP). Caddy binds `:8000` on all interfaces so the same listener handles LAN + tailnet traffic.
- **mDNS gotcha:** the hostname is `example-host.local` (NOT `Mac.local` / `Mac.lan` — that was a wrong guess that propagated through earlier docs and never actually resolved). Verify with `scutil --get LocalHostName`.

## Decks

A "deck" = one company/role the user is prepping for. Decks are declared in
`generator.DECK_CONTEXT` — keyed by deck name, value gives:
- `source`: subdir name under `~/Dropbox/workspace/interviews/`
- `topics`: shared topic dirs to also pull into the generation prompt
- `focus`: a sentence telling Claude what to bias generation toward

To add a new deck: add an entry to `DECK_CONTEXT`, restart the pm2 process
(`pm2 restart prep-app`), then click "Generate" in the deck view. Decks declared
in `DECK_CONTEXT` are auto-seeded into the DB at app startup so they appear in
the index even before any questions exist.

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
- **Apply ecosystem changes:** `pm2 reload ~/Dropbox/workspace/macmini/ecosystem.config.js && pm2 save`
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

- `app.py` — FastAPI routes
- `db.py` — sqlite schema + accessors + SRS state machine
- `generator.py` — claude-p question generator
- `grader.py` — deterministic + claude-p grader
- `templates/` — Jinja2 HTML
- `static/style.css` — dark theme
- `data.sqlite` — the database (committed via Dropbox)
- `requirements.txt` — pinned deps
- `.venv/` — virtualenv (NOT in Dropbox sync ideally, but we don't gitignore here)

## Future (not built yet)

- Hourly trigger via local launchd plist → POST a "create session" endpoint and
  send the user a Telegram link. Wait until the manual flow has been used for a
  bit before wiring this up.
- Generation cost cap / per-deck rate limit.
- Optional code-execution sandbox so `code` answers can actually be run, not just
  graded by Claude.
- Per-deck topic filter on the "Add more" form (e.g., generate only behavioral
  cards for cherry).
