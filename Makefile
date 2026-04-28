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

.PHONY: help setup tools deps build install-goreman dev run-app run-worker run-temporal test clean wipe-temporal-state \
        artifact promote install-staging install-prod

help:
	@echo "make setup    — mise install + uv sync + build worker + install goreman"
	@echo "make dev      — start temporal + app + worker via goreman (Procfile)"
	@echo "make build    — Go worker build only"
	@echo "make test     — placeholder; no test suite yet"
	@echo "make clean    — kill stray dev processes; preserve data"
	@echo "make wipe-temporal-state — reset temporal devserver state (data.sqlite untouched)"
	@echo ""
	@echo "Deploy pipeline (artifact-based, see DEPLOY.md):"
	@echo "  make artifact REF=<sha-or-tag>    — build a deployable artifact"
	@echo "  make promote ENV=<env> REF=<id>   — point staging|prod at an artifact"
	@echo "  make install-staging              — one-time launchd setup for staging"
	@echo "  make install-prod                 — one-time launchd setup for prod"

setup: tools deps build install-goreman

tools:
	@command -v $(MISE) >/dev/null 2>&1 || { \
	  echo "mise not found — \`brew install mise\` (or curl https://mise.run | sh)"; exit 1; }
	$(MISE) install --quiet

deps: tools
	$(RUN) uv sync --quiet

build: $(WORKER)

$(WORKER): $(shell find worker-go -name '*.go' 2>/dev/null) worker-go/go.mod tools
	cd worker-go && $(RUN) go build -o bin/worker .

install-goreman: tools
	@$(RUN) sh -c 'command -v goreman >/dev/null 2>&1' || { \
	  echo "Installing goreman via mise's go..."; \
	  $(RUN) go install github.com/mattn/goreman@latest; \
	}

dev: tools install-goreman
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

test:
	@echo "(no test suite yet — see docs/oss-readiness.md Phase 1 backlog)"

clean:
	@-pkill -f "uvicorn app:app" 2>/dev/null || true
	@-pkill -f "worker-go/bin/worker" 2>/dev/null || true
	@-pkill -f "temporal server start-dev" 2>/dev/null || true
	@echo "stopped (data.sqlite + vapid keys preserved)"

wipe-temporal-state:
	rm -rf temporal-data/

# ----- artifact-based deploy pipeline -----
# See DEPLOY.md for the full story. Short version:
#   work in this repo on main → tag when satisfied → build artifact from
#   that tag → promote staging or prod to it. Artifacts are immutable
#   directories under ~/Library/prep/artifacts/ with their own .venv +
#   built worker + bundled cm. Data lives in ~/Library/prep/data/<env>/
#   so it survives any promote.

artifact:
	@[ -n "$(REF)" ] || { echo "usage: make artifact REF=<sha-or-tag>"; exit 1; }
	REF=$(REF) FORCE=$(FORCE) scripts/build.sh

promote:
	@[ -n "$(ENV)" ] && [ -n "$(REF)" ] || { echo "usage: make promote ENV=<staging|prod> REF=<artifact-id>"; exit 1; }
	ENV=$(ENV) REF=$(REF) scripts/promote.sh

install-staging:
	scripts/install-launchd.sh staging

install-prod:
	scripts/install-launchd.sh prod
