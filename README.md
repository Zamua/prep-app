# prep

Spaced-repetition flashcards. Describe a topic, get a deck. AI generates
and grades; FSRS schedules.

## Try the hosted version

[**prepcards.app**](https://prepcards.app). Free, multi-user, sign in
with Clerk. Or just start typing on the landing page: a deck you
generate before signing up is kept under an anonymous account and
merges into your real one when you sign in.

Bring your own AI key (Anthropic, OpenAI, or OpenRouter) on the AI
settings page. OpenRouter can be connected with one click instead of a
pasted key.

## What it is, technically

One TypeScript Worker running on **celld**, a self-hostable
(Apache-2.0) runtime for the Cloudflare Workers API that keeps cell
state in object storage. There is no application server, no separate
database, and no job queue:

- Per-user state is a SQLite database inside that user's durable object.
- Pages are server-rendered from nunjucks templates, inside the same
  durable object that holds the data.
- Long AI work runs as durable jobs on cell alarms, with a step ledger
  that survives eviction and restart.
- AI is a `fetch`. Every key is the user's own.

[`docs/architecture.md`](docs/architecture.md) is the tour: the four
cell classes, the enforced layering, durable work on alarms, and the
BYOK model.

## Self-host

You need a celld node with object storage behind it, and the three
runtime secrets prep needs (an anonymous-cookie secret, a BYOK
encryption key, and a VAPID keypair if you want web push).

```bash
git clone https://github.com/Zamua/prep-app.git prep
cd prep
make setup
make build
```

`make build` writes `worker/build/` and `worker/dist/assets`. Deploy that
with the wrangler config for your environment (`wrangler.prod.jsonc` is
the shape; edit its `vars` block for your own deploy) and point a celld
node at it.

**Identity is Clerk, or nothing.** Set the five `CLERK_*` vars and
sign-in works; leave them unset and the deploy runs anonymous-only,
where every visitor gets a cookie-identified account and there is no
sign-in page. Secrets are never read from the wrangler file: they arrive
at runtime as `CELLD_VAR_*`.

**AI is optional and always the user's own key.** A user adds an
Anthropic, OpenAI, or OpenRouter key on `/settings/agent`. A deploy can
additionally configure one shared OpenAI-compatible endpoint
(`PREP_FREE_INFERENCE_*`) so visitors get generation with no setup;
without it, AI features refuse with one clear message and the rest of
the app works as a manual SRS.

## Hack on it

```bash
make setup       # mise install + npm install + uv sync
make test        # vitest
make typecheck   # tsc over the worker and its tests
make ci          # lint + typecheck + test + the migration tool's suite
```

To run it for real against a local celld node:

```bash
make dev         # build, deploy, start on 127.0.0.1:8791
make dev-stop
```

`make dev` wraps `worker/scripts/run-node.sh`, whose header comments list
what it expects (a `celld` binary and an S3-compatible endpoint for cell
storage) and the env vars that redirect each. `make help` lists every
target.

Python is still in the tree for two things that are not the application:
the migration tool under `migrate/`, and the browser and pixel test
harness under `tests/`.

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before sending a PR, and
[`docs/architecture.md`](docs/architecture.md) before making a
structural change. The layering rule is enforced by
`worker/tests/layering.test.ts`, so a misplaced import fails the suite
rather than getting reviewed.

## License

MIT. See [`LICENSE`](LICENSE).
