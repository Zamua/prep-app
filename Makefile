# prep: contributor entrypoints.
#
# Quick start (macOS):
#   brew bundle && mise install && make setup && make test
#
# Linux: install mise (see CONTRIBUTING.md), then the same three.
#
# The application is the TypeScript worker under worker/. Python remains
# for the migration tool (migrate/) and the browser test harness
# (tests/); `make setup` provisions both.
#
# Deploy targets are operator-only and live in a private repo. This
# Makefile never deploys anything.

# `mise exec` puts the .tool-versions toolchain on PATH without shell
# activation. Set MISE to override.
MISE ?= mise
RUN  := $(MISE) exec --
NPM  := $(RUN) npm --prefix worker
PY   := $(RUN) .venv/bin/python

.PHONY: help setup tools node-deps py-deps build test typecheck \
        test-migrate lint format hooks dev dev-stop llm-stub e2e parity \
        ci clean

help:
	@echo "Setup:"
	@echo "  make setup       mise install + npm install + uv sync + git hooks"
	@echo ""
	@echo "The worker (the application):"
	@echo "  make build       templates, icons, service worker, dist/assets"
	@echo "  make test        vitest"
	@echo "  make typecheck   tsc over the worker and its tests"
	@echo "  make dev         build, deploy and start a local celld node"
	@echo "  make dev-stop    stop the node this checkout started"
	@echo "  make llm-stub    the canned LLM a local node calls for AI flows"
	@echo ""
	@echo "Python tools:"
	@echo "  make test-migrate  the migration tool's suite (migrate/)"
	@echo "  make e2e           browser suites against PARITY_BASE_URL"
	@echo "  make parity        pixel flows against PARITY_BASE_URL"
	@echo ""
	@echo "Both:"
	@echo "  make lint        ruff format-check + check, read-only"
	@echo "  make format      ruff format + fix (writes)"
	@echo "  make hooks       install the pre-commit hook (part of setup)"
	@echo "  make ci          lint + typecheck + test + test-migrate"
	@echo "  make clean       drop generated build output"

setup: tools node-deps py-deps hooks

tools:
	@command -v $(MISE) >/dev/null 2>&1 || { \
	  echo "mise not found: \`brew install mise\` (or curl https://mise.run | sh)"; exit 1; }
	$(MISE) install --quiet

node-deps: tools
	$(NPM) install --silent

py-deps: tools
	$(RUN) uv sync --group dev --quiet

# ----- the worker -----

build: node-deps
	$(NPM) run build

typecheck: node-deps
	$(NPM) run typecheck

test: node-deps
	cd worker && $(RUN) npx vitest run

# A local celld node: builds, deploys to the scratch bucket, starts on
# 127.0.0.1:8791. Needs the celld binary and the scratch MinIO
# credentials; the script names what is missing and refuses.
dev: node-deps
	worker/scripts/run-node.sh

dev-stop:
	worker/scripts/run-node.sh stop

# The canned LLM the parity corpus was recorded against. A local node is
# configured to call it, so AI flows need it running.
llm-stub: py-deps
	$(PY) -m tests.parity.llm_stub --port 8089

# ----- python tools -----

# The migration tool exports a pre-cutover snapshot, imports it and
# verifies the two agree. Tier 3 bundles worker/domain/fsrs, so the
# worker's node_modules has to be installed.
test-migrate: py-deps node-deps
	$(PY) -m pytest tests/migrate

# Browser suites against a running target. PARITY_BASE_URL names it; the
# suites skip with the reason when it is unset.
e2e: py-deps
	$(PY) -m pytest tests/e2e

# The pixel goldens under tests/parity/goldens/. PARITY_PHASE selects
# which flows run; `all` is every phase. One file per invocation on
# purpose: each holds a browser session for its whole scope.
PARITY_PHASE ?= all

parity: py-deps
	@for f in tests/parity/test_flows_*.py; do \
	  echo "-> $$f"; \
	  PARITY_PHASE=$(PARITY_PHASE) $(PY) -m pytest -q $$f || exit 1; \
	done

# ----- both -----

lint: py-deps
	$(RUN) .venv/bin/ruff format --check .
	$(RUN) .venv/bin/ruff check .

format: py-deps
	$(RUN) .venv/bin/ruff format .
	$(RUN) .venv/bin/ruff check --fix .

# Wire .githooks/ as this checkout's hooks dir. Idempotent. Bypass a
# single commit with `git commit --no-verify`.
hooks:
	@git config core.hooksPath .githooks
	@echo "git hooks installed (.githooks/pre-commit)"

ci: lint typecheck test test-migrate

clean:
	rm -rf worker/build worker/dist artifacts
	@echo "generated output removed"
