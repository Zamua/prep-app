# Contributing

Thanks for digging in.

The README's "Hack on it" section covers the contributor quickstart
end-to-end (setup, run, common operations, repo layout). This file
is just a few extra notes for people sending PRs.

## Code style

- Python: 4-space indent, type hints where they add clarity. Linted
  + formatted with `ruff` (config in `pyproject.toml`). Entities
  use pydantic v2.
- Go: `gofmt` + `go vet`.
- HTML/CSS/JS: 2-space indent.
- Comments: explain *why* (the non-obvious constraint, the gotcha,
  the past incident). Don't narrate *what* — the code does that.

`make setup` installs a pre-commit hook that runs `ruff format --check`,
`ruff check`, `gofmt -l`, AND `pytest -x` against staged python/go.
Run `make format` to fix drift, `make lint` for a read-only check
across the whole tree, `make test` for the pytest suite.
`git commit --no-verify` bypasses the hook for one-off emergencies.

## Architecture

Code is organized DDD-style: one package per bounded context
(`prep/decks/`, `prep/study/`, `prep/notify/`, `prep/agent/`,
`prep/auth/`), each with `entities.py` / `repo.py` / `service.py` /
`routes.py`. Pure domain logic lives in `prep/domain/`;
infrastructure adapters in `prep/infrastructure/`.
[`docs/architecture.md`](docs/architecture.md) is the deep-dive
(layering rules, end-to-end flow walkthroughs, schema, deploy
shape); [`CLAUDE.md`](CLAUDE.md) is the terser version oriented at
AI agents.

A few rules to keep the boundary clean:
- `prep/domain/` imports nothing from bounded contexts, repos, or
  infrastructure. Pure stdlib + pydantic only.
- Routes call services (or repos for trivial reads); they don't
  reach into temporal_client or sqlite directly.
- Repos return entities, not raw dicts.

If you're adding a new domain concept, ask: which bounded context
does it belong in? If the answer is "more than one", that's a sign
the cross-cut belongs in `prep/web/` or wants a new context.

## What to file as an issue

- Real bugs (with a way to reproduce on a fresh `make dev`).
- Missing features that fit the self-hosted-personal-tool shape.

What's intentionally out of scope (so don't expect these to land):

- Public / multi-tenant auth (passwords, OAuth, magic links). prep
  is designed for personal-tailnet hosting.
- ANTHROPIC_API_KEY support. We use the Claude subscription path
  on purpose; users opt in by pasting a `claude setup-token` into
  the UI.
- Mobile native apps. The PWA covers that.
- Cloud SaaS / hosted multi-user.

## Releasing (author convention)

For your fork, use whatever workflow you like — the codebase doesn't
depend on it. The bundled flow is two compose stacks (`stag` +
`prod`) running side-by-side on a single docker host, both driven
from one checkout. The README's "Two-stack deploy" section walks
through the mechanics; the short version:

- `make deploy-stag` builds current `main` and brings up the staging
  stack on `:8082`.
- `make promote v=v0.X.Y` writes `v0.X.Y` to `.prod-version`,
  commits, pushes, builds *that tag* in a temporary git worktree,
  and brings up the prod stack on `:8081`.
- `make deploy-prod` (no `v=`) idempotently redeploys whatever tag
  `.prod-version` already pins.

Tags are semver: `v0.X.Y`. Bump minor for features, patch for
fixes. Pre-1.0 we're permissive about the minor/patch boundary.
