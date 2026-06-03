# prep

Spaced-repetition flashcards. Describe a topic, get a deck. AI generates and
grades; FSRS schedules.

> Pre-1.0, single-author. Stable for personal use; data shape and APIs may
> shift between minor versions.

## Try the hosted version

[**prepcards.app**](https://prepcards.app). Free, multi-user, Clerk sign-in.
Bring your own AI key (Claude subscription, Anthropic API, OpenAI, or
OpenRouter) on the AI settings page.

## Or self-host

Same code, different auth model. The `PREP_AUTH_MODE` env var switches
between them.

| | Hosted (prepcards.app) | Self-host |
|---|---|---|
| Auth | Clerk | Tailscale, or single-user |
| AI key | Per-user, BYOK | Per-user BYOK, *or* one process-wide token |
| Domain | prepcards.app | Your own |

### Quickstart

```bash
git clone https://github.com/Zamua/prep-app.git prep
cd prep
docker compose up -d
```

Open <http://127.0.0.1:8082>. Default user is `guest`.

To go multi-user via your tailnet:

```bash
tailscale serve --bg --https=443 --set-path=/prep http://127.0.0.1:8082
```

Then drop `PREP_DEFAULT_USER` from `.env` so the real Tailscale identity
flows through.

### AI (optional)

Prep works as a manual SRS without AI. To enable generation and grading,
open the user menu, choose **AI agent**, and add credentials for one of:

- **Claude subscription**: run `claude setup-token`, paste the output. Bills
  your Max plan.
- **Anthropic API**, **OpenAI**, or **OpenRouter**: paste the API key.

Self-hosted single-user installs can also set one process-wide subscription
token, at the bottom of the same page.

### Day-to-day

```bash
docker compose up -d        # start
docker compose down         # stop (data preserved)
docker compose logs -f      # tail
git pull && docker compose build && docker compose up -d   # update
```

Backup:

```bash
docker run --rm -v prep-data:/src -v "$PWD":/dst alpine \
  tar -czf /dst/prep-$(date +%F).tgz -C /src .
```

## Hack on it

```bash
brew bundle && make setup       # mise, python, go, bun, temporal-cli, deps
make dev                        # goreman: temporal + uvicorn + worker
```

Open <http://127.0.0.1:8081>. Python and jinja auto-reload; hard-refresh
for static. Tests: `make test`.

Architecture: [`docs/architecture.md`](docs/architecture.md). DDD layout,
FSRS scheduler, BYOK adapter precedence, single-container deploy shape.

### Two-stack deploy

Run staging plus prod side-by-side on one docker host:

```bash
make deploy-stag                       # prep:staging on :8082
git tag -a v0.X.Y && git push --tags
make promote v=v0.X.Y                  # pins .prod-version, builds, :8081
```

`.prod-version` tracks what's in prod. `make deploy-prod` redeploys the
pinned tag.

## License

MIT. See [`LICENSE`](LICENSE).
