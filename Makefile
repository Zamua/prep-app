# prep — contributor entrypoints.
#
# Quick start (macOS):
#   brew bundle && make setup && make dev
#
# Linux: see CONTRIBUTING.md (curl-install uv, apt-install go, etc.) then
# `make setup && make dev`.

UV     ?= uv
VENV   := .venv
GO     ?= go
WORKER := worker-go/bin/worker
GOREMAN ?= goreman

# Dev-bypass user: `make dev` boots with this set so a contributor sees a
# working app immediately on http://127.0.0.1:8081/ without needing
# Tailscale Serve installed. For a real auth flow, unset and set up
# Tailscale (see CLAUDE.md).
export PREP_DEFAULT_USER ?= dev@example.com

.PHONY: help setup deps build dev run-app run-worker run-temporal test clean wipe-temporal-state

help:
	@echo "make setup    — uv sync (Python + venv + deps) + build Go worker"
	@echo "make dev      — start temporal + app + worker via goreman (Procfile)"
	@echo "make build    — Go worker build only"
	@echo "make test     — placeholder; no test suite yet"
	@echo "make clean    — kill stray dev processes; preserve data"
	@echo "make wipe-temporal-state — reset temporal devserver state (data.sqlite untouched)"

setup: deps build

deps:
	@command -v $(UV) >/dev/null 2>&1 || { \
	  echo "uv not found — \`brew install uv\` (or see https://github.com/astral-sh/uv)"; exit 1; }
	$(UV) sync --quiet

build: $(WORKER)

$(WORKER): $(shell find worker-go -name '*.go' 2>/dev/null) worker-go/go.mod
	cd worker-go && $(GO) build -o bin/worker .

dev:
	@command -v $(GOREMAN) >/dev/null 2>&1 || { \
	  echo "goreman not found — \`brew install goreman\` (or any Procfile runner: overmind, forego, hivemind)"; exit 1; }
	@mkdir -p temporal-data
	$(GOREMAN) start

# Helpers if you want to run one process at a time (e.g. for debugging):
run-app:
	$(VENV)/bin/uvicorn app:app --host 127.0.0.1 --port 8081 --reload

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
