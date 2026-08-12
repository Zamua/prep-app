# prep — contributor entrypoints.
#
# Quick start (macOS):
#   brew bundle && mise install && make setup && make dev
#
# Linux: see CONTRIBUTING.md (one-line mise install + temporal CLI from
# GitHub releases) then `mise install && make setup && make dev`.
#
# Deploy targets (deploy-devel, deploy-prod, deploy-vps, promote,
# promote-vps, logs-*, down-*) are operator-only and live in the
# operator's PRIVATE infra repo at infra/prep/Makefile — they reference
# operator-owned paths (VPS host, sudo, /home/admin, /home/apps). Self-
# hosters: see README.md "Day-to-day" for the plain `docker compose`
# workflow; the operator's Makefile is an automation layer over that.

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
# Surface the /dev/preview/* template-fixture routes for the UI sweep.
# Never set this in prod images — prep/app.py gates registration on it.
export PREP_DEV ?= 1

.PHONY: help setup tools deps build dev run-app run-worker run-temporal \
        lint format hooks clean wipe-temporal-state test e2e ci

help:
	@echo "Local dev (no docker):"
	@echo "  make setup    — mise install + uv sync (incl. dev tools) + build worker + install hooks"
	@echo "  make dev      — start temporal + app + worker via goreman (Procfile)"
	@echo "  make build    — Go worker build only"
	@echo "  make lint     — ruff check + go vet (read-only)"
	@echo "  make format   — ruff format + gofmt (writes)"
	@echo "  make test     — pytest (python unit + integration tests)"
	@echo "  make e2e      — Playwright/httpx smoke against a DEPLOYED \$$E2E_BASE_URL (needs credentials; see the e2e section)"
	@echo "  make e2e-local— browser + offline coverage against a local server (no deploy, no credentials)"
	@echo "  make ci       — lint + test + e2e."
	@echo "  make hooks    — install pre-commit hook (idempotent; runs as part of \`make setup\`)"
	@echo "  make clean    — kill stray dev processes; preserve data"
	@echo ""
	@echo "Deploy targets are operator-only (infra/prep/Makefile in the private infra repo)."
	@echo "Self-hosters: see README.md 'Day-to-day' for plain docker-compose."

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
	$(RUN) .venv/bin/uvicorn prep.app:app --host 127.0.0.1 --port 8081 --reload

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

test: tools
	$(RUN) .venv/bin/pytest -x

# ----- e2e -----
# Drives Playwright + an httpx client against a DEPLOYED prep instance.
# Each session creates a throwaway `e2e-test-deck` via the app's HTTP
# routes, runs assertions, then deletes it, so create + delete +
# cascade are themselves under test. Tests live under tests/e2e/
# (excluded from `make test` via pyproject's norecursedirs).
#
# Credentials, because the public deploys use Clerk:
#   E2E_API_TOKEN     a `prep_pat_…` from /settings/api, for the httpx
#                     setup fixtures.
#   E2E_CLERK_EMAIL   a `+clerk_test` account and its password, for the
#   E2E_CLERK_PASSWORD  browser fixtures (signed in once per session).
# Missing either makes the deployed suites SKIP with the reason rather
# than pass while testing nothing.
#
# `make e2e-local` needs none of this: it runs the browser coverage
# that does not require a deployed worker against a local server.
#
# Pre-flight: the deployed instance has to be up. We check `/` returns
# 200 first; bail with a clear error otherwise rather than wasting
# minutes on per-test timeouts.
E2E_BASE_URL ?= https://staging.prepcards.app

e2e: tools
	@echo "→ e2e against $(E2E_BASE_URL)"
	@code=$$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 $(E2E_BASE_URL)/ 2>/dev/null || echo 000); \
	  if [ "$$code" != "200" ]; then \
	    echo "  FAIL: $(E2E_BASE_URL)/ returned $$code (expected 200). bring it up first."; exit 1; fi
	@# Pre-flight: playwright + chromium binary must be installed for the
	@# browser tests in test_browser_smoke.py. The python package alone
	@# isn't enough — the headless chromium binary lives under
	@# ~/Library/Caches/ms-playwright and needs an explicit
	@# `playwright install chromium` to download. We surface a friendly
	@# hint here instead of letting tests skip silently via the
	@# pytest.skip() wired into conftest.
	@if ! $(RUN) .venv/bin/python -c "import playwright" 2>/dev/null; then \
	  echo "  WARN: playwright not installed in venv — \`make setup\` (or \`uv sync --group dev\`)"; \
	  echo "        browser tests will skip"; \
	fi
	E2E_BASE_URL=$(E2E_BASE_URL) $(RUN) .venv/bin/pytest -x tests/e2e

# Browser + offline coverage that boots its own local server: no
# deploy, no credentials. Run it on any machine, any time.
#
# One file at a time on purpose: each file starts its own uvicorn, and
# under memory pressure a multi-file run produces false navigation
# timeouts that look like product failures.
E2E_LOCAL_FILES = \
	tests/e2e/test_local_browser_smoke.py \
	tests/e2e/test_online_study_e2e.py \
	tests/e2e/test_study_components_e2e.py \
	tests/e2e/test_offline_study_e2e.py \
	tests/e2e/test_offline_author_e2e.py \
	tests/e2e/test_offline_adoption_e2e.py \
	tests/e2e/test_offline_m5_e2e.py \
	tests/e2e/test_offline_parity.py \
	tests/e2e/test_markdown_parity.py \
	tests/e2e/test_instant_start_e2e.py

e2e-local: tools
	@for f in $(E2E_LOCAL_FILES); do \
	  echo "→ $$f"; \
	  $(RUN) .venv/bin/pytest -q $$f || exit 1; \
	done

# ----- CI bundle -----
# Lint + test (in-process) + e2e (against devel). Each step exits
# non-zero on failure. The operator's `promote` / `promote-vps` targets
# (infra/prep/Makefile) re-run these before tagging prod.
ci: lint test e2e

# Wire .githooks/ as the git hooks dir for this checkout. Idempotent.
# Contributors get this for free via `make setup`. To bypass for a
# single commit, use `git commit --no-verify`.
hooks:
	@git config core.hooksPath .githooks
	@echo "git hooks installed (.githooks/pre-commit)"

clean:
	@-pkill -f "uvicorn prep.app:app" 2>/dev/null || true
	@-pkill -f "worker-go/bin/worker" 2>/dev/null || true
	@-pkill -f "temporal server start-dev" 2>/dev/null || true
	@echo "stopped (data.sqlite + vapid keys preserved)"

wipe-temporal-state:
	rm -rf temporal-data/
