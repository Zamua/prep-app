# prep: contributor entrypoints.
#
# Quick start (macOS):
#   brew bundle && mise install && make setup && make test
#
# Linux: install mise (see CONTRIBUTING.md), then the same three.
#
# The application is the TypeScript worker under worker/.
#
# Deploy targets are operator-only and live in a private repo. This
# Makefile never deploys anything.

# `mise exec` puts the .tool-versions toolchain on PATH without shell
# activation. Set MISE to override.
MISE ?= mise
RUN  := $(MISE) exec --
NPM  := $(RUN) npm --prefix worker

.PHONY: help setup tools node-deps build test typecheck \
        lint format hooks dev dev-stop llm-stub ci clean

help:
	@echo "Setup:"
	@echo "  make setup       mise install + npm install + git hooks"
	@echo ""
	@echo "The worker (the application):"
	@echo "  make build       templates, icons, service worker, dist/assets"
	@echo "  make test        vitest"
	@echo "  make typecheck   tsc over the worker and its tests"
	@echo "  make lint        typecheck, read-only"
	@echo "  make format      no-op; formatting is the editor's job"
	@echo "  make dev         build, deploy and start a local celld node"
	@echo "  make dev-stop    stop the node this checkout started"
	@echo "  make llm-stub    the canned LLM a local node calls for AI flows"
	@echo ""
	@echo "  make hooks       install the pre-commit hook (part of setup)"
	@echo "  make ci          typecheck + test"
	@echo "  make clean       drop generated build output"

setup: tools node-deps hooks

tools:
	@command -v $(MISE) >/dev/null 2>&1 || { \
	  echo "mise not found: \`brew install mise\` (or curl https://mise.run | sh)"; exit 1; }
	$(MISE) install --quiet

node-deps: tools
	$(NPM) install --silent

build: node-deps
	$(NPM) run build

typecheck: node-deps
	$(NPM) run typecheck

test: node-deps
	cd worker && $(RUN) npx vitest run

lint: typecheck

format:
	@echo "nothing to run: the worker has no formatter of its own"

# A local celld node: builds, deploys to the scratch bucket, starts on
# 127.0.0.1:8791. Needs the celld binary and the scratch MinIO
# credentials; the script names what is missing and refuses.
dev: node-deps
	worker/scripts/run-node.sh

dev-stop:
	worker/scripts/run-node.sh stop

# The canned LLM a local node calls. AI flows need it running.
llm-stub: node-deps
	cd worker && $(RUN) node scripts/llm-stub.mjs --port 8089

# Wire .githooks/ as this checkout's hooks dir. Idempotent. Bypass a
# single commit with `git commit --no-verify`.
hooks:
	@git config core.hooksPath .githooks
	@echo "git hooks installed (.githooks/pre-commit)"

ci: typecheck test

clean:
	rm -rf worker/build worker/dist artifacts
	@echo "generated output removed"
