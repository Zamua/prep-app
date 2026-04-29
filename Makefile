# prep — contributor entrypoints.
#
# Quick start (macOS):
#   brew bundle && mise install && make setup && make dev
#
# Linux: see CONTRIBUTING.md (one-line mise install + temporal CLI from
# GitHub releases) then `mise install && make setup && make dev`.

# `mise exec` runs commands with .tool-versions tools on PATH without
# requiring shell activation. Set MISE_BIN to override (e.g., for users
# not on macOS Homebrew).
MISE     ?= mise
RUN      := $(MISE) exec --
WORKER   := worker-go/bin/worker

# Dev-bypass user: `make dev` boots with this set so a contributor sees a
# working app immediately on http://127.0.0.1:8081/ without needing
# Tailscale Serve installed. For a real auth flow, unset and set up
# Tailscale (see CLAUDE.md).
export PREP_DEFAULT_USER ?= dev@example.com

.PHONY: help setup tools deps build dev run-app run-worker run-temporal \
        lint format hooks clean wipe-temporal-state \
        docker-up docker-down docker-build docker-logs

help:
	@echo "Local dev (no docker):"
	@echo "  make setup    — mise install + uv sync (incl. dev tools) + build worker + install hooks"
	@echo "  make dev      — start temporal + app + worker via goreman (Procfile)"
	@echo "  make build    — Go worker build only"
	@echo "  make lint     — ruff check + go vet (read-only)"
	@echo "  make format   — ruff format + gofmt (writes)"
	@echo "  make hooks    — install pre-commit hook (idempotent; runs as part of \`make setup\`)"
	@echo "  make clean    — kill stray dev processes; preserve data"
	@echo ""
	@echo "Docker compose (canonical deploy shape):"
	@echo "  make docker-build   — build prep + agent images"
	@echo "  make docker-up      — bring the stack up in detached mode"
	@echo "  make docker-down    — stop the stack (data volumes preserved)"
	@echo "  make docker-logs    — tail compose logs"

setup: tools deps build hooks

tools:
	@command -v $(MISE) >/dev/null 2>&1 || { \
	  echo "mise not found — \`brew install mise\` (or curl https://mise.run | sh)"; exit 1; }
	$(MISE) install --quiet

deps: tools
	$(RUN) uv sync --group dev --quiet

build: $(WORKER)

$(WORKER): $(shell find worker-go -name '*.go' 2>/dev/null) worker-go/go.mod tools
	cd worker-go && $(RUN) go build -o bin/worker .

dev: tools
	@mkdir -p temporal-data
	$(RUN) goreman start

# Helpers if you want to run one process at a time (e.g. for debugging):
run-app: tools
	$(RUN) .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8081 --reload

run-worker: build
	$(WORKER)

run-temporal:
	@mkdir -p temporal-data
	temporal server start-dev --db-filename ./temporal-data/temporal.db --namespace prep --log-level warn

# ----- lint / format -----
# `make lint` is read-only — fails if drift exists. CI / pre-commit hook
# territory. `make format` rewrites files in place.

lint: tools
	$(RUN) .venv/bin/ruff format --check .
	$(RUN) .venv/bin/ruff check .
	cd worker-go && $(RUN) go vet ./...
	@cd worker-go && bad=$$($(RUN) gofmt -l .); \
	  if [ -n "$$bad" ]; then echo "gofmt drift in:"; echo "$$bad"; exit 1; fi

format: tools
	$(RUN) .venv/bin/ruff format .
	$(RUN) .venv/bin/ruff check --fix .
	cd worker-go && $(RUN) gofmt -w .

# Wire .githooks/ as the git hooks dir for this checkout. Idempotent.
# Contributors get this for free via `make setup`. To bypass for a
# single commit, use `git commit --no-verify`.
hooks:
	@git config core.hooksPath .githooks
	@echo "git hooks installed (.githooks/pre-commit)"

clean:
	@-pkill -f "uvicorn app:app" 2>/dev/null || true
	@-pkill -f "worker-go/bin/worker" 2>/dev/null || true
	@-pkill -f "temporal server start-dev" 2>/dev/null || true
	@echo "stopped (data.sqlite + vapid keys preserved)"

wipe-temporal-state:
	rm -rf temporal-data/

# ----- docker compose deploy -----
# Canonical deploy shape as of v0.13.0. Two services: prep (app +
# temporal devserver + go worker) and agent (claude wrapper). Volumes
# preserved across `docker compose down` — only `down -v` wipes data.
# `docker compose up` works without an .env file (compose has inline
# ${VAR:-default} for every override); see README.md for tweaks.

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=200
