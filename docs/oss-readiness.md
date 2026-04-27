# Open-source readiness — what's blocking, what's cosmetic

The goal: **anyone with a tailnet + a machine can clone this repo and run
one command to have prep flashcards on their network.** Audit performed
2026-04-27 against the v0.4.1 codebase.

## Blockers — required before MIT release

### 1. `DECK_CONTEXT` is hardcoded source
Both `generator.py` (Python) and `worker-go/activities/activities.go`
(Go) carry duplicate hardcoded `DECK_CONTEXT` blocks defining `cherry`
and `temporal` decks tied to the project author's interview prep.

**Fix:** move to a `decks.toml` (or `.json`) config file at the repo
root. Both runtimes read from it. Sample shipped, real config gitignored.
Schema:
```toml
[decks.cherry]
source = "cherry"        # subdir name under PREP_DECKS_DIR
topics = ["behavioral"]
focus  = "..."
```

### 2. `INTERVIEWS_DIR` hardcoded to `~/Dropbox/workspace/interviews`
`generator.py:6` and the Go worker assume this path. Other users have
different note layouts.

**Fix:** read from a `PREP_DECKS_DIR` env var, fall back to `./decks/`
inside the repo if unset.

### 3. Claude CLI is the only AI path
`grader.py` and `worker-go/activities/grading.go` shell out to
`~/.local/bin/claude`. Requires the CLI installed, on PATH, with an
active Anthropic subscription.

**Fix:** support a direct `ANTHROPIC_API_KEY` path via the official
Anthropic SDKs (`anthropic` for Python, `anthropic-sdk-go` for Go). Keep
CLI shell-out as a fallback for users who'd rather use their existing
subscription. The session-resume / prompt-caching pattern in the worker
needs to be re-implemented over the SDK's prompt cache primitive.

**Effort:** half a day. This is the largest code change in the audit.

### 4. `ecosystem.config.js` has absolute paths
`/Users/zamua/Dropbox/workspace/macmini/prep-app/...` etc.

**Fix:** rewrite to use `cwd: '.'` + relative `script` paths, OR replace
pm2 entirely with the Docker-Compose path (item 6).

### 5. `Caddyfile` lives outside the repo
The user's Caddyfile is at `~/Dropbox/workspace/macmini/Caddyfile`.

**Fix:** ship a `Caddyfile.example` snippet inside the repo with the
relevant `handle /prep* { reverse_proxy 127.0.0.1:8081 }` block,
including the LAN-listener Tailscale-header strip. Document where to
slot it into the user's caddy config.

### 6. No Dockerfile / docker-compose.yml
Current install requires manual: Python venv + pip install, Go build,
bun build (already-shipped cm-bundle is fine), Temporal devserver
install, Caddy install, pm2 install, Tailscale install + serve config.

**Fix:** containerize the four runtime services
(`prep-app`, `prep-worker`, `temporal`, `caddy`) and ship a
`docker-compose.yml`. Tailscale runs either as a sidecar (`tailscale/tailscale`
image) or on the host. Persistent volumes for data.sqlite + temporal-data.

**Effort:** 4-6 hours done well.

## Documentation / branding

### 7. README.md is personal
Currently links the author's tailnet (`example-host.ts.net`),
mac mini hostname, IP address, and decks (cherry, temporal).

**Fix:** rewrite as a public README:
- Pitch / what it is / why
- Screenshots (mobile + desktop, light + dark)
- Architecture diagram
- Install instructions (canonical "fresh machine" path)
- Configuration reference (env vars, decks.toml schema)
- "Add a deck" guide
- Contributing / dev setup
- License

### 8. No LICENSE file
Add `LICENSE` with MIT text + author copyright.

### 9. CLAUDE.md is internal working notes
Strip personal references and rename to `ARCHITECTURE.md`, or exclude
from the public repo entirely.

### 10. Branding
The "prep · a commonplace book" aesthetic with Fraunces serif is already
strong. Minimal additions:
- favicon (currently missing)
- OG card image for social shares
- Possibly: a clearer tagline that reads outside the author's context.

A real logo isn't necessary — the typographic identity carries it.

### 11. Screenshots for README
iPhone + desktop, light + dark. Reuse `ui-tools/capture.py` infra.

## Already in good shape — no work required

| Concern | Status |
|---|---|
| Tailscale auth via `Tailscale-User-Login` header | works generically for any tailnet |
| Multi-user data isolation | clean post-v0.3.x security audit |
| SQLite + WAL for storage | zero-config |
| Phosphor icons (MIT) + Fraunces (OFL) + JetBrains Mono (Apache) | all permissive, attributable in README |
| Python deps (fastapi, mistune, temporalio) | all permissive |
| Go deps (modernc.org/sqlite, temporal sdk) | all permissive |
| Schema migrations idempotent + FK-safe (post-v0.4.1) | clean |
| Workflow ID format / ownership checks | enforced end-to-end |

## Realistic path to v1.0 OSS

| Phase | Items | Effort |
|---|---|---|
| **MVP** — publishable + usable by motivated users | 1, 2, 4, 5, 7, 8, 9 + basic Dockerfile (no compose) | ~2 days |
| **One-command deploy** | docker-compose with service deps, Tailscale sidecar, `./bootstrap.sh` helper | +1 day |
| **Polish** | item 3 (ANTHROPIC_API_KEY path), item 11 (screenshots), CI (GitHub Actions: lint/test/build) | +1-2 days |

**Total:** 3-4 focused days to "fresh box → one command → working prep app on your tailnet."

## Open questions for the author

- Project name on publish — keep `prep` or pick something more distinctive that's GitHub-searchable?
- License copyright holder — personal name or project name?
- Hosting the demo — leave at `https://example-host.ts.net/prep/` (private to your tailnet) or stand up a public demo?
- Issue/discussion home — GitHub Issues, or a separate Discord/forum?
