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
git clone https://github.com/zamua/prep prep
cd prep
docker compose up -d
```

The first `up` builds the images (~5 min); subsequent ones are <5s.
Visit <http://127.0.0.1:8082/>. You're logged in as `guest` — that's
the default identity for a single-user deploy. To change anything
(port, URL prefix, identity), copy `.env.example` → `.env` and edit.
Otherwise skip it.

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

---

## Hack on it

For contributors who want a fast iteration loop on the source.

### Setup (one-time, macOS)

```bash
git clone https://github.com/zamua/prep prep
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

Open <http://127.0.0.1:8081/>. You're auto-logged in as
`dev@example.com` via `PREP_DEFAULT_USER`. `Ctrl-C` cleans up all
three processes.

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
make docker-build        # multi-stage: go + bun + python:slim
make docker-up           # bring up the prep + agent containers
make docker-logs         # tail
```

Reads the same `.env` as the user-mode quickstart. If you ran
`make dev` first, the docker stack uses port 8082 to avoid colliding
with `make dev`'s 8081.

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
