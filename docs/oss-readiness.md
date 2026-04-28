# OSS readiness — current plan

The eventual goal: **anyone can clone this repo, run one command, and have
prep flashcards on their network.** Two distribution targets:

- **Contributors** (technical users forking to develop): clone + `make dev`.
- **End users** (people who just want to run it): brew tap or a single
  install script that drops in a launchd / systemd service.

The first goal is the immediate priority — we want to be able to invite
a contributor who can fork and immediately get up and running.

Status against the original v0.4.1 audit, refreshed for v0.7.4:

| Original blocker | Now |
|---|---|
| `DECK_CONTEXT` hardcoded in source (Python + Go) | ⚠️ partially solved by v0.6.0 (UI deck creation persists in DB). Legacy fallback for `cherry`/`temporal` still in source — easy to delete. |
| `INTERVIEWS_DIR = ~/Dropbox/workspace/interviews` hardcoded | ⚠️ only used by the legacy fallback above. Goes when DECK_CONTEXT goes. |
| Claude CLI is the only AI path (no API key) | ✅ **deliberate design choice** — see below. Re-scoped to "support arbitrary agent CLIs". |
| `ecosystem.config.js` absolute paths | ❌ unchanged. Will be replaced by Procfile + Makefile (Phase 0). |
| `Caddyfile` outside the repo | ❌ unchanged. Phase 0 ships an example snippet inside the repo. |
| No Dockerfile / docker-compose | ❌ unchanged. Phase 1 (after Phase 0). |
| README is personal | ❌ unchanged. Sanitize Phase 0, full rewrite Phase 1. |
| No LICENSE | ❌ unchanged. MIT, Phase 0. |
| `CLAUDE.md` is internal working notes | ❌ unchanged. Sanitize-and-keep Phase 0. |
| No favicon, OG image, screenshots | ❌ unchanged. Phase 1. |
| Dead Telegram code in worker | ❌ unchanged. Drop in Phase 0. |

## Design decision — local AI CLI is the agent path

The app shells out to `claude -p ...` (or `opencode`, `aider`, …) rather
than using `ANTHROPIC_API_KEY`. This is **deliberate**, not a stopgap:

- No API-key management — uses the user's existing CLI's OAuth/keychain.
  The user already paid for their subscription; don't make them pay twice.
- Credentials stay in the CLI's keychain (macOS Keychain / libsecret) —
  never touch our env vars or our DB.
- Aligns with the local-first / self-hosted ethos.
- Agent harnesses come with web search, file tools, prompt cache built-in
  — we get all of that without re-implementing.
- Swappable: env-var-configurable means a contributor with `opencode`
  just sets `PREP_AGENT_BIN` and it works.

**Phase 0 generalization:** split the existing `CLAUDE_BIN` env var into:

```
PREP_AGENT_BIN  = ~/.local/bin/claude
PREP_AGENT_ARGS = -p,--strict-mcp-config,--mcp-config,{mcp_config}
```

(comma-separated args; `{placeholders}` interpolated). Default behavior
unchanged from today. v1 documents claude-code as the supported agent;
others land via PRs.

**Phase 0 doesn't add API-key support.** If a future user really wants it,
that's a separable PR. Don't block release on it.

## Phase 0 — contributor-ready (~1 day)

Goal: a fresh clone on a clean Mac/Linux box can be brought up by any
contributor with a single command.

**Code cleanup**

- [ ] Drop legacy `DECK_CONTEXT` + `INTERVIEWS_DIR` from `generator.py`
      and `worker-go/activities/activities.go`. Cherry/temporal decks in
      prod already have `context_prompt` set — verify before removing.
- [ ] Drop dead Telegram code from worker (`activities.go` notify code,
      `TELEGRAM_ENV` / `TELEGRAM_CHAT_ID` env vars in worker config).
      Never wired up; users don't need it.
- [ ] Generalize `CLAUDE_BIN` → `PREP_AGENT_BIN` + `PREP_AGENT_ARGS`
      env vars. Backward-compat: if `CLAUDE_BIN` is set, use it for
      `PREP_AGENT_BIN` so existing prod doesn't break.
- [ ] Sanitize `CLAUDE.md`: drop tailnet hostname, IPs, mDNS hostname,
      personal email. Keep architecture, gotchas, deploy convention.
- [ ] Strip `~/Dropbox/workspace/interviews` references everywhere.

**Process management**

- [ ] Move `ecosystem.config.js` → `prod/ecosystem.config.js.example`
      (or drop entirely once a Procfile-based path replaces it).
      Include a sanitized version showing the structure.
- [ ] Add a `Procfile` for dev — three processes: `app`, `worker`,
      `temporal`. Standard Heroku-style format; works with goreman /
      overmind / forego / hivemind.
- [ ] Add a `Makefile` with the contributor entrypoints:
      - `make setup` — venv + pip install + go build + bun install
      - `make dev` — runs the Procfile via goreman (we vendor the
        binary path or check at first run + give a clear install hint)
      - `make build` — Go build only
      - `make test` — placeholder for now (no test suite yet)
      - `make clean` — kills any stray dev processes
- [ ] Default `make dev` to bind on `127.0.0.1:8081` directly (no Caddy)
      with `PREP_DEFAULT_USER=dev@example.com` so a contributor doesn't
      need Tailscale set up to develop.

**Repo hygiene**

- [ ] `LICENSE` (MIT, "Copyright (c) 2026 Zamua").
- [ ] `Brewfile` — system deps for macOS users: `python@3.11`, `go`,
      `temporal`, `bun`, plus `caddy` and `pm2` as optional for prod-like
      setups. Linux users get an apt/dnf hint in the README.
- [ ] `.editorconfig` — basic 2-space indent for js/css/html, 4 for
      python, tabs for go. Optional; consistency for contributors.
- [ ] `CONTRIBUTING.md` — quick-start (clone, brew bundle, make setup,
      make dev), the staging-first / tag-based deploy convention,
      where to file issues, code style notes.
- [ ] Sanitized `README.md` — drop tailnet/IP refs; document the dev
      flow, the agent CLI assumption, and what's required to get a
      working local instance. Promotional / screenshots / pretty
      install instructions wait for Phase 1.

**Acceptance criteria**

A test contributor (us, or a fresh shell) can:

```bash
git clone <repo>
cd prep-app
brew bundle               # macOS — installs python, go, temporal, bun
make setup                # pip install + go build
make dev                  # all three processes up; localhost:8081
```

Open http://127.0.0.1:8081/, see decks, study, transform — all working
without touching Tailscale, Caddy, pm2, or the user's prod state.

## Phase 1 — public OSS publish (~2 days, after Phase 0)

- README v2 with screenshots, architecture diagram, install steps for
  end users.
- `Caddyfile.example` snippet for users who want a path-prefixed deploy.
- Docker Compose for self-hosting (4 services + tailscale sidecar option).
- favicon + OG card image.
- GitHub Actions CI (lint Python + Go, build, smoke-test).
- Public GitHub repo flip (public visibility, but "do not use without
  reading the README" disclaimer for the duration of v0.x).

## Phase 2 — brew tap for end users (later)

- Brew formula for macOS — installs prebuilt prep-app binary, sets up
  launchd plist, runs migration.
- GitHub Releases with prebuilt binaries for macOS arm64/x64 + Linux
  arm64/x64.
- Optional: a similar AUR / nix flake for Linux users.

## Open items intentionally deferred

- Multi-user prod deploys. Right now the auth boundary is "anyone on
  your tailnet, identified by their tailscale_login." That works for
  small groups (family, team). A real public deployment would need
  invite tokens, password fallback, or SSO — out of scope for v1.
- Mobile native apps. PWA is good enough.
- Card-level analytics / charts. Reviews + SRS state are stored;
  user-facing stats UI is Phase 2+.
- Real test suite. Phase 0 has no automated tests; Phase 1 should
  add at least a Python pytest harness for routes + a Go test for
  the worker activities.

## Decisions

Confirmed during planning chat (2026-04-28):

- Same repo (`prep-app/`); sanitize in place rather than fork.
- Keep `CLAUDE.md` filename; sanitize contents.
- Keep `prep` as the project name for now.
- License copyright: real name (Zamua, per git author).
- Dev process manager: Procfile-based, default to goreman (Go binary,
  no extra runtime needed; users with `overmind` etc. can use that
  too — the Procfile is portable).
- Agent CLI: claude-code is the documented v1 agent. Generalize the
  env vars so others can be plugged in via PR.
