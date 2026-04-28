# Contributing

Thanks for digging in. This doc covers everything you need to fork the
repo and have a working local instance running.

## Quick start (macOS)

```bash
git clone <repo-url> prep-app
cd prep-app
brew bundle              # mise (only)
make setup               # mise install + uv sync + go build + goreman install
make dev                 # starts temporal + uvicorn + worker via Procfile
```

Open <http://127.0.0.1:8081/>. You'll be auto-logged in as
`dev@example.com` (the `make dev` shim sets `PREP_DEFAULT_USER` so you
don't need Tailscale to develop).

`Ctrl-C` cleans up all three processes.

## Quick start (Linux)

```bash
curl https://mise.run | sh                          # mise (manages python+go+bun+temporal)
git clone <repo-url> prep-app
cd prep-app
make setup
make dev
```

## How toolchain management works

The repo pins tool versions in `.tool-versions`:

```
python 3.11
go 1.22
bun 1.1.0
aqua:temporalio/cli 1.6.2
```

[mise](https://mise.jdx.dev/) reads that file and provisions the right
versions per project — no system Python conflicts, no `goenv`/`pyenv`
juggling, no version drift between contributors. mise uses uv internally
for Python, so installs are fast.

`make setup` does the install end-to-end:
1. `mise install` — provision pinned versions of python, go, bun
2. `mise exec -- uv sync` — create the Python venv + install deps
3. `mise exec -- go build` — build the worker
4. `go install goreman` — Procfile runner used by `make dev`

The Makefile uses `mise exec --` to run commands within mise's tool
environment, so you don't need to `eval "$(mise activate <shell>)"` in
your rc file. If you do want shell activation (auto-PATH for the
pinned tools any time you `cd` into the repo), follow
<https://mise.jdx.dev/getting-started.html>.

## What's running

`make dev` uses `goreman` to start three processes from `Procfile`:

| process  | what                                     | port              |
|----------|------------------------------------------|-------------------|
| temporal | embedded Temporal devserver (SQLite)     | 7233 / 8233 (UI)  |
| app      | FastAPI via uvicorn (`--reload`)         | 8081              |
| worker   | Go Temporal worker                       | (no port, polls)  |

The worker handles long-running work — card generation, grading,
deck/card transforms — so the request thread never blocks on a
`claude -p` shell-out.

## What you need to know

### Architecture

Read `CLAUDE.md` first. It's the working architecture doc — covers the
SQLite schema, the two Temporal workflows, the auth model, the
notifications subsystem, the PWA install flow, and gotchas we've hit.

### Auth

Production runs behind Tailscale Serve, which sets a
`Tailscale-User-Login` header on every request. The app reads that as
the user identity. There is no password / OAuth / magic-link path —
auth comes for free from the tailnet, which makes the
self-hosted-personal-tool shape clean.

For dev, `make dev` sets `PREP_DEFAULT_USER=dev@example.com` so every
unauthenticated request becomes that user. Don't ship that in prod.

### Agent CLI

The app shells out to a local AI CLI (`claude -p ...`) for question
generation, grading, and transform work. **You need a working `claude`
CLI in your PATH** to exercise those code paths. Get the one bundled
with Claude Code if you don't already have it.

The CLI is configured via two env vars (defaults shown):

```
PREP_AGENT_BIN  = ~/.local/bin/claude
PREP_AGENT_ARGS = -p,--strict-mcp-config,--mcp-config,{mcp_config}
```

If you want to plug in a different agent harness (`opencode`, `aider`,
…) for your fork, override these. Comma-separated args; `{mcp_config}`
is interpolated to a JSON file path the worker writes.

### Deploy convention (staging-first, tag-based)

The author runs prep on a Mac mini with a parallel `staging` checkout
on `main` and a `prod` checkout pinned to a git tag. Develop on `main`
in staging → tag a release (`git tag -a v0.X.Y && git push origin
v0.X.Y`) → run `./deploy.sh v0.X.Y` from the prod checkout to promote.

For your fork: this convention is optional. Use whatever workflow you
like — the codebase doesn't depend on it.

## Code style

- Python: 4-space indent, type hints where they add clarity.
- Go: `gofmt` (vendored via `go fmt` on save).
- HTML/CSS/JS: 2-space indent.
- Comments: explain *why* (the non-obvious constraint, the gotcha,
  the past incident). Don't narrate *what* — the code does that.

## What to file as an issue

- Real bugs (with a way to reproduce on a fresh `make dev`).
- Missing features that fit the self-hosted-personal-tool shape.

What's intentionally out of scope (so don't expect these to land):

- Public/multi-tenant auth (passwords, OAuth, magic links). The design
  is "people on your tailnet are the user set." If you need broader
  auth, you probably want a different project.
- Mobile native apps. The PWA covers that.
- Anything that requires hosting infrastructure beyond a single box on
  someone's tailnet.

## Releasing

For your own fork, semver via git tags. The author's prod uses
`./deploy.sh v0.X.Y` to checkout a tag, rebuild, and pm2-restart; the
script lives at the repo root. Read it before depending on it.
