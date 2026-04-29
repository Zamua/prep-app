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
        deploy-stag deploy-prod promote logs-stag logs-prod down-stag down-prod

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
	@echo "Deploy (single-checkout, two stacks side-by-side):"
	@echo "  make deploy-stag           — build current working tree, deploy as 'stag' on :8082"
	@echo "  make deploy-prod           — build the tag in .prod-version, deploy as 'prod' on :8081"
	@echo "  make promote v=v0.X.Y      — write v to .prod-version, commit, push, deploy-prod"
	@echo "  make logs-stag             — tail the 'stag' stack"
	@echo "  make logs-prod             — tail the 'prod' stack"
	@echo "  make down-stag             — stop 'stag' (data volumes preserved)"
	@echo "  make down-prod             — stop 'prod' (data volumes preserved)"

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

# ----- two-stack deploy from one checkout -----
# Staging tracks main; prod is pinned to the tag in .prod-version.
# Each stack is a distinct compose project (-p stag / -p prod) with
# its own image tag (prep:staging / prep:vX.Y.Z) and named volumes
# (prep-data / prod-data). Both run on the same docker daemon.
#
# Promote = update .prod-version (the source of truth for "what is
# prod"), commit, deploy. Idempotent: re-running deploy-prod with the
# same .prod-version is a no-op (cached build, container already on
# that image).

DEPLOY_PROD_TAG := $(shell test -f .prod-version && tr -d '[:space:]' < .prod-version)
DEPLOY_BUILD_DIR := /tmp/prep-build

deploy-stag:
	@echo "→ deploy-stag (image=prep:staging, project=stag, port=8082)"
	@# Explicitly clear PREP_DEFAULT_USER — the Makefile's `export ... ?=` for
	@# `make dev` would otherwise leak into the deploy target and silently
	@# enable bypass. Real auth comes from Tailscale headers.
	env -u PREP_DEFAULT_USER IMAGE_TAG=staging \
	  docker compose --env-file deploy/staging.env -p stag up -d --build

deploy-prod:
	@if [ -z "$(DEPLOY_PROD_TAG)" ]; then \
	  echo "no .prod-version — write a tag (e.g. \`echo v0.13.3 > .prod-version\`) first"; exit 1; fi
	@if ! git rev-parse --verify "$(DEPLOY_PROD_TAG)" >/dev/null 2>&1; then \
	  echo "tag $(DEPLOY_PROD_TAG) not found locally — try \`git fetch --tags\`"; exit 1; fi
	@echo "→ deploy-prod (image=prep:$(DEPLOY_PROD_TAG), project=prod, port=8081)"
	@if [ -d $(DEPLOY_BUILD_DIR) ]; then git worktree remove --force $(DEPLOY_BUILD_DIR) 2>/dev/null; rm -rf $(DEPLOY_BUILD_DIR); fi
	git worktree add --detach $(DEPLOY_BUILD_DIR) $(DEPLOY_PROD_TAG)
	env -u PREP_DEFAULT_USER IMAGE_TAG=$(DEPLOY_PROD_TAG) \
	  docker compose \
	    -f docker-compose.yml \
	    --project-directory $(DEPLOY_BUILD_DIR) \
	    --env-file deploy/prod.env \
	    -p prod \
	    up -d --build
	git worktree remove --force $(DEPLOY_BUILD_DIR)

promote:
	@if [ -z "$(v)" ]; then echo "usage: make promote v=v0.X.Y"; exit 1; fi
	@if ! git rev-parse --verify "$(v)" >/dev/null 2>&1; then \
	  echo "tag $(v) doesn't exist — create it first: \`git tag -a $(v) && git push --tags\`"; exit 1; fi
	@echo "$(v)" > .prod-version
	git add .prod-version
	git commit -m "promote $(v) to prod"
	git push origin main
	$(MAKE) deploy-prod

logs-stag:
	docker compose -p stag logs -f --tail=200

logs-prod:
	docker compose -p prod logs -f --tail=200

down-stag:
	docker compose -p stag down

down-prod:
	docker compose -p prod down
