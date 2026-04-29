# Contributing

Thanks for digging in.

The README's "Hack on it" section covers the contributor quickstart
end-to-end (setup, run, common operations, repo layout). This file
is just a few extra notes for people sending PRs.

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

- Public / multi-tenant auth (passwords, OAuth, magic links). prep
  is designed for personal-tailnet hosting.
- ANTHROPIC_API_KEY support. We use the Claude subscription path
  on purpose; users opt in by pasting a `claude setup-token` into
  the UI.
- Mobile native apps. The PWA covers that.
- Cloud SaaS / hosted multi-user.

## Releasing (author convention)

For your fork, use whatever workflow you like — the codebase doesn't
depend on it. The author runs prep with two compose stacks on a
single Mac mini:

- `prep-app-staging/` on `main` — develop here, verify, tag from here
- `prep-app/` at a tag (detached HEAD) — prod, promote by checking
  out the new tag and `docker compose build && up -d`

Tags are semver: `v0.X.Y`. Bump minor for features, patch for
fixes. Pre-1.0 we're permissive about the minor/patch boundary.
