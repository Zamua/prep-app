# prep

A self-hosted spaced-repetition flashcard tool for learning anything.

You describe what you want to learn — a topic, a chapter, a paper, a
syllabus — and prep turns it into a deck of flashcards using your local
Claude subscription. You study them on a schedule. Wrong answers come
back in 10 minutes; right ones step out: 1d → 3d → 7d → 14d → 30d. The
more you remember, the longer they sleep.

It runs on a single docker host you own. Your data stays local. No
SaaS, no shared servers. AI features are opt-in: connect Claude once,
then everything generates / grades / improves through your own
subscription. Or use prep without AI at all and write the cards by hand.

> **Status:** pre-1.0, single-author. Stable for personal use; APIs
> and data shape may shift between minor versions.

---

## Use it

A 5-minute walkthrough for self-hosting on any docker-capable box
(your laptop, a mini PC, a Pi, a VPS).

### 1. Install docker

- macOS: `brew install colima && colima start` (CLI), or install
  Docker Desktop.
- Linux: your distro's `docker` + `docker compose` packages.

### 2. Get prep

```bash
git clone https://github.com/Zamua/prep-app.git prep
cd prep
docker compose up -d
```

The first `up` builds the images (~5 min); subsequent ones are <5s.
Visit <http://127.0.0.1:8082/>. You're logged in as `guest` —
that's the single-user default for a fresh deploy. To go multi-user,
keep `guest` for now and skip to step 3 (Tailscale).

For other tweaks (port, URL prefix, custom default user), copy
`.env.example` → `.env` and edit; everything in there is optional.

### 3. Reach it from anywhere — and add real auth

prep ships with one auth model: **Tailscale Serve**. Install
[Tailscale](https://tailscale.com), sign in on the docker host and on
your phone / laptop, then on the docker host:

```bash
tailscale serve --bg --https=443 --set-path=/prep http://127.0.0.1:8082
```

Now `https://<your-tailnet>.ts.net/prep/` works from any device on
your tailnet — valid TLS, no DNS, no port-forwarding. Tailscale Serve
injects per-user identity headers, so each tailnet member who visits
gets their own decks.

If multiple people on your tailnet are using the same prep instance,
also remove the `PREP_DEFAULT_USER` line from `.env` (or leave it
unset — same effect) so the bypass doesn't override the real headers.

There's no other auth path: no passwords, no OAuth, no magic links.
prep is built for single-tailnet groups; if you need broader auth,
this isn't the project for you.

### 4. Connect Claude (optional)

Without Claude, prep works as a manual flashcard SRS — you write the
cards, you self-grade after answering. That's a perfectly valid setup.

If you have a Claude subscription and want the AI features (deck
generation, AI grading of code/short answers, deck-wide transforms,
per-card refinement):

1. On any machine where you're signed into Claude Code, run
   `claude setup-token`.
2. In prep: open the user menu → **AI agent**.
3. Paste the token. Click **Connect**.

The token is valid for a year and lives in a docker volume on the
host. AI surfaces unhide on the next page load.

### 5. Day-to-day

- **`docker compose up -d`** — start
- **`docker compose down`** — stop (data preserved in volumes)
- **`docker compose logs -f`** — tail
- **Update**: `git pull && docker compose build && docker compose up -d`
- **Backup data**:
  ```
  docker run --rm -v prep-data:/src -v "$PWD":/dst alpine \
    tar -czf /dst/prep-backup-$(date +%F).tgz -C /src .
  ```

### Running staging + prod side-by-side (advanced)

If you want to iterate on `main` without touching what's deployed —
the way the project author runs it — there's a separate **`make
deploy-stag`** / **`make deploy-prod`** flow that runs two stacks on
the same docker host. Skip this section if you just want one
deploy; only worth the extra moving pieces if you'd otherwise have to
make changes to the live URL while testing them. See [Hack on it →
Two-stack deploy](#two-stack-deploy-staging--prod-from-one-checkout)
below.

---

## Hack on it

For contributors who want a fast iteration loop on the source.

### Setup (one-time, macOS)

```bash
git clone https://github.com/Zamua/prep-app.git prep
cd prep
brew bundle              # installs mise (only)
make setup               # mise pulls python+go+bun+goreman+temporal-cli; uv sync; go build
```

Linux: skip `brew bundle`, install mise via the
[one-line curl installer](https://mise.jdx.dev/), then `make setup`.

### Run

```bash
make dev                 # goreman starts temporal + uvicorn + worker
```

Open <http://127.0.0.1:8081/>. The Makefile exports
`PREP_DEFAULT_USER=dev@example.com` for `make dev` so you're
auto-logged in. `Ctrl-C` cleans up all three processes.

This is loopback-only, with `--reload` on uvicorn. Edit a Python file
and the server restarts in <1s. Templates auto-reload. Static files
need a browser hard-refresh.

### Common operations

| You change | What you do |
|---|---|
| `app.py`, `db.py`, any Python | save — uvicorn `--reload` picks it up |
| `templates/*.html` | save, refresh browser (jinja auto-reloads per request) |
| `static/style.css` or icons | save, hard-refresh browser |
| `static/cm/` (CodeMirror source) | `cd static/cm && bun run build` |
| `worker-go/**/*.go` | `Ctrl-C` make dev, `make build`, `make dev` again (~5s) |
| Add a python dep | `mise exec -- uv add <pkg>` |
| Add a go dep | `cd worker-go && mise exec -- go get <mod>` |
| Schema change | edit `db.init()` (idempotent), restart `make dev` |

There are no automated tests yet — exercise via the UI.

### Validate the docker artifact

When you want to test the actual deploy shape:

```bash
docker compose build      # multi-stage: go + bun + python:slim
docker compose up -d      # bring up prep + agent containers
docker compose logs -f    # tail
```

Reads `.env` if present. The default host port is 8082 so it doesn't
collide with `make dev`'s 8081. If 8082 is *also* in use (e.g.,
because `make deploy-stag` is already running on this host),
override with `PREP_HOST_PORT=8083 docker compose up -d`.

### Two-stack deploy (staging + prod from one checkout)

The author runs prep with two stacks side-by-side on a single Mac
mini: `staging` tracks `main`, `prod` is pinned to a git tag. Both
stacks share the docker daemon's image cache and the working tree —
no second checkout, no `git checkout` dance.

The whole flow:

```bash
# iterate on main, deploy to staging
vim ...
make deploy-stag                    # builds prep:staging, project=stag, port 8082

# happy with what's on main; tag it
git tag -a v0.14.0 -m "what's new"
git push origin --tags

# promote that tag to prod (writes the tag to .prod-version,
# commits, pushes, builds, deploys)
make promote v=v0.14.0              # builds prep:v0.14.0, project=prod, port 8081

# back to iterating on main — staging gets new bytes, prod stays
# pinned at v0.14.0 until the next promote.
```

Internals worth knowing:

- **`.prod-version`** is the source of truth for what's running in
  prod. `make deploy-prod` reads it; `make promote` updates it via a
  commit. To redeploy whatever prod is currently on (e.g., after a
  config change), just `make deploy-prod` — idempotent.
- **`deploy/staging.env` + `deploy/prod.env`** carry the per-stack
  shape (port, ROOT_PATH, namespace). Tracked in git. A local `.env`
  layers on top for personal overrides.
- **`make deploy-prod`** uses `git worktree add --detach <tag>` into
  `/tmp/prep-build`, builds from there, then removes the worktree.
  Your main working tree never moves — you can be mid-edit on main
  and `make deploy-prod` is invisible to you.
- Image tags are versioned (`prep:staging`, `prep:v0.14.0`) so
  multiple versions coexist in the daemon's image cache without
  fighting over a single `:dev` tag.
- `make logs-stag` / `make logs-prod` / `make down-stag` /
  `make down-prod` for per-stack ops.

### Repo layout

```
app.py                FastAPI routes + startup
db.py                 sqlite schema + accessors + SRS state machine
agent.py              startup probe for AI availability
notify.py             VAPID web push + scheduler
grader.py             deterministic mcq/multi/idk grading (fast path)
chat_handoff.py       "Discuss this card" handoff URL builder
icons.py              Jinja `icon('name')` global → inlined SVG
dev_preview.py        /dev/preview routes for UI sweeps
temporal_client.py    Python → Temporal client helpers
templates/            Jinja2 templates
static/               style.css, icons/, pwa/, cm-bundle.js
worker-go/            Go Temporal worker
  agent/              Client interface (ShellAgent + HTTPAgent impls)
  cmd/agent-server/   HTTP wrapper around the claude CLI
  workflows/          GradeAnswer, Transform, PlanGenerate
  activities/         side effects the workflows orchestrate
  shared/types.go     workflow input/output schemas
docker/               Dockerfile.prep + Dockerfile.agent + Procfile.docker
docker-compose.yml    canonical deploy: prep + agent containers
```

### Contributing back

Fork, branch, PR. Issues welcome with a way to reproduce on a fresh
`make dev`. What's intentionally out of scope: cloud SaaS / public
multi-tenant auth (prep is designed for personal-tailnet hosting),
ANTHROPIC_API_KEY support (we use the Claude subscription path on
purpose), mobile native apps (the PWA covers that).

## License

MIT. See [`LICENSE`](LICENSE).
