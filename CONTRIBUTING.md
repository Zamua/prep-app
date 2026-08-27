# Contributing

Thanks for digging in.

The whole application is the TypeScript Worker under `worker/`. Client
JS, CSS and icons live in `static/`, which the worker's build reads as
an input.

## Getting set up

```bash
make setup       # mise install + npm install + uv sync + git hooks
make test        # vitest
make typecheck   # tsc over the worker and its tests
make ci          # lint + typecheck + test + the migration tool's suite
```

On macOS, `brew bundle` first. On Linux, install mise
(`curl https://mise.run | sh`) and then run the same targets.
`make help` lists everything.

`make dev` builds, deploys and starts a local celld node so you can click
through the real app; `make dev-stop` stops it. It wraps
`worker/scripts/run-node.sh`, whose header comments list what it expects
(a `celld` binary, an S3-compatible endpoint for cell storage) and the
env vars that redirect each piece. AI flows against a local node call
`make llm-stub`, the canned LLM the parity corpus was recorded against.

`make setup` installs a pre-commit hook. Staged TypeScript under
`worker/` gates on `npm run typecheck` plus the whole vitest suite, which
runs in seconds. `git commit --no-verify` bypasses it; use that sparingly.

**Python is still in the tree, and it is not the application.** It covers
the migration tool under `migrate/` and the browser and pixel test
harness under `tests/`. New application code is TypeScript under
`worker/`.

## Code style

- TypeScript, 2-space indent, ES modules with `.js` import specifiers.
- HTML/CSS/client JS: 2-space indent. No bundler, no framework.
- Python (`migrate/`, `tests/`): 4-space indent, formatted and linted
  with `ruff`. `make format` fixes drift, `make lint` is the read-only
  check.
- Comments explain *why*: the non-obvious invariant, the constraint the
  type system cannot express, the failure mode being guarded against.
  They describe the code as it is now. History belongs in the commit
  message.

## Architecture

Read [`docs/architecture.md`](docs/architecture.md) before making a
structural change. The short version:

```
worker/domain/     pure. No I/O, no framework, no clock of its own.
worker/app/        use cases and ports. Policy, not plumbing.
worker/runtime/    the entry worker, the four cells, and the adapters.
                   compose.ts is the composition root.
```

The dependency rule is **runtime -> app -> domain, and nothing imports
upward**. It is not a convention: `worker/tests/layering.test.ts`
enforces it and will fail your PR if you break it. Specifically:

- `domain/` may not import from `app/`, `runtime/`, `cloudflare:` or
  `node:`. A domain function that needs the time takes it as a
  parameter.
- `app/` may not contain `fetch(`, `new Response`, `.sql.exec`,
  `DurableObject` or `nunjucks`. Use cases call ports; adapters do the
  I/O.
- Only `runtime/compose.ts` may import from `runtime/adapters/`. Cells
  and the router receive what they need through ports.
- Only the nunjucks adapter may touch nunjucks or the compiled
  templates.

If you are adding a new concept, ask which layer owns it. If the answer
is "more than one", it usually wants a port in `app/` and an adapter in
`runtime/`.

## Tests

vitest, colocated by layer under `worker/tests/`. Write them as you go:
red, green, refactor for new logic; a characterization test first when
you are changing behavior that has none.

Run the tests you affected while iterating, and `make ci` before you open
a PR. It must be green.

Domain modules have oracle tests pinned against recorded corpora under
`tests/fixtures/parity/`. Those corpora are read-only inputs: if one is
wrong, say so in the PR rather than editing the fixture to match your
change.

## What to file as an issue

- Real bugs, with a way to reproduce against a local node.
- Missing features that fit the shape: a personal spaced-repetition
  tool you can also self-host.

Intentionally out of scope, so do not expect these to land:

- **Per-user Claude-subscription credentials.** BYOK is API keys only
  (Anthropic, OpenAI, OpenRouter). A Claude Code OAuth token is rejected
  by the Messages API, and the one sanctioned path for it bundles and
  spawns a large executable per call. `docs/architecture.md` has the
  reasoning.
- **Deploy and operator tooling in this repo.** This repo holds
  application code only. Compose files, cluster manifests, deploy
  targets and secrets live in a separate private repo by design.
- **A second runtime.** The app targets the Cloudflare Workers API. A
  port to a general-purpose server would mean giving up the per-user
  cell, which is where the isolation guarantee comes from.
- **Native mobile apps.** The PWA covers that.

## Releasing (author convention)

For your fork, use whatever workflow you like; the codebase does not
depend on it. Tags are semver: `v0.X.Y`. Bump minor for features, patch
for fixes. Pre-1.0 the minor/patch boundary is permissive.
