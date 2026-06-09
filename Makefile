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
# Surface the /dev/preview/* template-fixture routes for the UI sweep.
# Never set this in prod images — prep/app.py gates registration on it.
export PREP_DEV ?= 1

.PHONY: help setup tools deps build dev run-app run-worker run-temporal \
        lint format hooks clean wipe-temporal-state test e2e ci \
        deploy-devel deploy-prod promote logs-devel logs-prod down-devel down-prod \
        deploy-vps promote-vps logs-vps

help:
	@echo "Local dev (no docker):"
	@echo "  make setup    — mise install + uv sync (incl. dev tools) + build worker + install hooks"
	@echo "  make dev      — start temporal + app + worker via goreman (Procfile)"
	@echo "  make build    — Go worker build only"
	@echo "  make lint     — ruff check + go vet (read-only)"
	@echo "  make format   — ruff format + gofmt (writes)"
	@echo "  make test     — pytest (python unit + integration tests)"
	@echo "  make e2e      — Playwright/httpx smoke against \$$E2E_BASE_URL (defaults to devel)"
	@echo "  make ci       — lint + test + e2e. Used by promote; also fine to run by hand."
	@echo "  make hooks    — install pre-commit hook (idempotent; runs as part of \`make setup\`)"
	@echo "  make clean    — kill stray dev processes; preserve data"
	@echo ""
	@echo "Deploy (single-checkout, two stacks side-by-side):"
	@echo "  make deploy-devel           — build current working tree, deploy as 'devel' on :8082"
	@echo "  make deploy-prod           — build the tag in .prod-version, deploy as 'prod' on :8081"
	@echo "  make promote v=v0.X.Y      — write v to .prod-version, commit, push, deploy-prod"
	@echo "  make logs-devel             — tail the 'devel' stack"
	@echo "  make logs-prod             — tail the 'prod' stack"
	@echo "  make down-devel             — stop 'devel' (data volumes preserved)"
	@echo "  make down-prod             — stop 'prod' (data volumes preserved)"
	@echo ""
	@echo "Public VPS (prepcards.app):"
	@echo "  make deploy-vps            — deploy the tag in .vps-version to the Hetzner VPS"
	@echo "  make promote-vps v=v0.X.Y  — write v to .vps-version, commit, push, deploy-vps"
	@echo "  make logs-vps              — tail the prepcards.app container"

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
# Drives Playwright + an httpx client against a deployed prep instance
# (devel by default; override target with `E2E_BASE_URL=...`). Each
# session creates a throwaway `e2e-test-deck` via the app's HTTP routes,
# runs assertions, then deletes it — so create + delete + cascade are
# themselves under test. Tests live under tests/e2e/ (excluded from
# `make test` via pyproject's norecursedirs).
#
# Pre-flight: the deployed instance has to be up. We check `/` returns
# 200 first; bail with a clear error otherwise rather than wasting
# minutes on per-test timeouts.
E2E_BASE_URL ?= https://macmini.trout-chimera.ts.net/prep-devel

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

# ----- CI bundle -----
# What `make promote` runs before tagging prod: lint + test (in-process)
# + e2e (against devel). Each step exits non-zero on failure so promote
# halts cleanly.
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

# ----- two-stack deploy from one checkout -----
# Staging tracks main; prod is pinned to the tag in .prod-version.
# Each stack is a distinct compose project (-p devel / -p prod) with
# its own image tag (prep:devel / prep:vX.Y.Z) and named volumes
# (prep-data / prod-data). Both run on the same docker daemon.
#
# Promote = update .prod-version (the source of truth for "what is
# prod"), commit, deploy. Idempotent: re-running deploy-prod with the
# same .prod-version is a no-op (cached build, container already on
# that image).

DEPLOY_PROD_TAG := $(shell test -f .prod-version && tr -d '[:space:]' < .prod-version)
DEPLOY_BUILD_DIR := /tmp/prep-build

# COMPOSE_BAKE=true switches `docker compose --build` from the legacy
# serial buildx-classic path to `docker buildx bake`, which builds the
# images in the compose project in parallel using a single buildkit
# session. On this two-image project (prep + agent) the parallelism
# saves a measurable chunk of wall time on every deploy — both images
# share the same builder-go base layer, so bake can dedup the work.
# Both deploy-devel and deploy-prod inherit this.
export COMPOSE_BAKE := true

deploy-devel:
	@echo "→ deploy-devel (image=prep:devel, project=devel, port=8082)"
	@# Pass PREP_DEFAULT_USER='' explicitly. compose's ${VAR-guest}
	@# (single dash) treats this as "set to empty" → no bypass → real
	@# Tailscale auth required. Without this, the Makefile's `make dev`
	@# export of dev@example.com would leak through.
	@# --wait blocks until both services pass their healthcheck (see
	@# the `healthcheck:` blocks in docker-compose.yml). Surfaces boot
	@# failures here instead of in an after-the-fact `make logs-devel`.
	@# Auto-source local .env (gitignored secrets like PREP_INTERNAL_TOKEN
	@# + CLAUDE_CODE_OAUTH_TOKEN) if it exists. Layered ON TOP of
	@# deploy/devel.env so per-machine values override the tracked
	@# deploy shape. Without this, `make deploy-devel` from a fresh
	@# shell fails compose's ${VAR:?} guards.
	set -a; [ -f .env ] && . ./.env; set +a; \
	PREP_DEFAULT_USER= PREP_DEV= IMAGE_TAG=devel \
	  docker compose --env-file deploy/devel.env -p devel up -d --build --wait --remove-orphans

deploy-prod:
	@if [ -z "$(DEPLOY_PROD_TAG)" ]; then \
	  echo "no .prod-version — write a tag (e.g. \`echo v0.13.3 > .prod-version\`) first"; exit 1; fi
	@if ! git rev-parse --verify "$(DEPLOY_PROD_TAG)" >/dev/null 2>&1; then \
	  echo "tag $(DEPLOY_PROD_TAG) not found locally — try \`git fetch --tags\`"; exit 1; fi
	@echo "→ deploy-prod (image=prep:$(DEPLOY_PROD_TAG), project=prod, port=8081)"
	@if [ -d $(DEPLOY_BUILD_DIR) ]; then git worktree remove --force $(DEPLOY_BUILD_DIR) 2>/dev/null; rm -rf $(DEPLOY_BUILD_DIR); fi
	git worktree add --detach $(DEPLOY_BUILD_DIR) $(DEPLOY_PROD_TAG)
	@# Same .env-sourcing trick as deploy-devel — secrets (PREP_INTERNAL_TOKEN,
	@# CLAUDE_CODE_OAUTH_TOKEN) live in the workspace .env, not the prod
	@# git worktree. Without this, compose's ${VAR:?} guards fail.
	set -a; [ -f .env ] && . ./.env; set +a; \
	PREP_DEFAULT_USER= PREP_DEV= IMAGE_TAG=$(DEPLOY_PROD_TAG) \
	  docker compose \
	    -f docker-compose.yml \
	    --project-directory $(DEPLOY_BUILD_DIR) \
	    --env-file deploy/prod.env \
	    -p prod \
	    up -d --build --wait --remove-orphans
	git worktree remove --force $(DEPLOY_BUILD_DIR)

promote:
	@if [ -z "$(v)" ]; then echo "usage: make promote v=v0.X.Y"; exit 1; fi
	@if ! git rev-parse --verify "$(v)" >/dev/null 2>&1; then \
	  echo "tag $(v) doesn't exist — create it first: \`git tag -a $(v) && git push --tags\`"; exit 1; fi
	@# Pre-flight: lint + python tests + e2e against devel. Run BEFORE
	@# mutating .prod-version so a failure exits cleanly (no half-bumped
	@# .prod-version, no orphaned tag pointing at the wrong commit). The
	@# pre-commit hook also runs lint+test at commit time; promote re-runs
	@# them so a contributor that bypassed the hook doesn't ship a broken
	@# build. Catches the failure modes where a stranded tag, or a
	@# runtime dep missing from the image (a kind of bug e2e would catch
	@# but the unit suite wouldn't), reaches prod.
	@echo "→ pre-flight: redeploy devel from tag $(v) so e2e runs against the same code we'll ship"
	$(MAKE) deploy-devel-from-tag v=$(v)
	@echo "→ pre-flight: lint + python tests"
	$(MAKE) lint
	$(MAKE) test
	@echo "→ pre-flight: e2e against devel"
	$(MAKE) e2e
	@echo "→ promoting $(v) to prod"
	@echo "$(v)" > .prod-version
	git add .prod-version
	git commit -m "promote $(v) to prod"
	git push origin main
	$(MAKE) deploy-prod

# Internal helper: build + bring up devel from a specific tag's
# commit (rather than the working tree). Used by `make promote` so e2e
# verifies exactly the bytes we're about to ship to prod, not the
# tree the contributor happens to have checked out. Mirrors the prod
# build path (git worktree at the tag, build from there). Cleans up
# the worktree on success or failure.
.PHONY: deploy-devel-from-tag
deploy-devel-from-tag:
	@if [ -z "$(v)" ]; then echo "usage: make deploy-devel-from-tag v=v0.X.Y"; exit 1; fi
	@if ! git rev-parse --verify "$(v)" >/dev/null 2>&1; then \
	  echo "tag $(v) not found locally"; exit 1; fi
	@if [ -d $(DEPLOY_BUILD_DIR) ]; then git worktree remove --force $(DEPLOY_BUILD_DIR) 2>/dev/null; rm -rf $(DEPLOY_BUILD_DIR); fi
	git worktree add --detach $(DEPLOY_BUILD_DIR) $(v)
	@# Source local .env from the workspace (where secrets live) so
	@# compose's ${VAR:?} guards pass — the worktree itself has no .env.
	set -a; [ -f .env ] && . ./.env; set +a; \
	PREP_DEFAULT_USER= PREP_DEV= IMAGE_TAG=devel \
	  docker compose \
	    -f docker-compose.yml \
	    --project-directory $(DEPLOY_BUILD_DIR) \
	    --env-file deploy/devel.env \
	    -p devel \
	    up -d --build --wait --remove-orphans
	git worktree remove --force $(DEPLOY_BUILD_DIR)

logs-devel:
	docker compose -p devel logs -f --tail=200

logs-prod:
	docker compose -p prod logs -f --tail=200

down-devel:
	docker compose -p devel down

down-prod:
	docker compose -p prod down

# ----- prepcards.app on a remote VPS -----
# The public-internet prep deploy. Runs on a remote host (reverse
# proxy at the host terminates TLS for prepcards.app and forwards to
# the prep container on :8082).
#
# Deployment shape (operator-managed pieces are NOT in this repo;
# only the app-side Makefile + docker-compose.yml are tracked here):
#
#   $VPS_PROJECT        : git checkout of this repo on the VPS
#   $OPS_DEPLOY_DIR/.env: secrets (CLERK_*, PREP_INTERNAL_TOKEN,
#                         PREP_KEY_ENCRYPTION_SECRET, ...), 0600.
#                         Operator-managed; loaded via --env-file.
#   $OPS_DEPLOY_DIR/compose.yml: VPS-only compose overlay (reverse-
#                         proxy labels, ports reset, etc.). Operator-
#                         managed; layered on top of the tracked
#                         docker-compose.yml.
#
# The OPS_DEPLOY_DIR convention keeps operator config (which can
# differ per deploy + holds secrets) out of the public app repo. See
# the shared infra repo's APP-PATTERN.md.
#
# Deploy model mirrors the local two-stack flow:
#   .vps-version is the source of truth for "what's running on prepcards.app".
#   `make deploy-vps` is idempotent: re-running on the same version is a
#   no-op (cached build, container already on that image).
#   `make promote-vps v=v0.X.Y` bumps the pin, commits + pushes, deploys.
#
# Why git pull + build on the VPS (rather than build locally + push image):
# the VPS already has docker + buildkit set up, the prep image's build
# context isn't huge, and avoiding a private registry keeps ops simple.
# Trade-off accepted: build CPU runs on the VPS during deploy.

VPS_HOST       ?= vps
VPS_PROJECT    ?= /home/apps/projects/prep
DEPLOY_VPS_TAG := $(shell test -f .vps-version && tr -d '[:space:]' < .vps-version)

# Operator-side compose + env for the VPS deploy. Lives OUTSIDE this
# repo (in an operator-managed infra checkout). See the shared infra
# repo's APP-PATTERN.md. Override by setting OPS_DEPLOY_DIR=... when
# invoking make; the default below matches the canonical layout.
OPS_DEPLOY_DIR ?= /home/admin/infra/prep

# Wraps an SSH'd command. The remote shell is bash so we can chain
# with && / use heredocs cleanly. -o BatchMode=yes refuses interactive
# password prompts — if the alias isn't keyed up, surface a fast fail
# rather than hanging on a TTY prompt that wouldn't work anyway.
SSH_VPS := ssh -o BatchMode=yes $(VPS_HOST)

deploy-vps:
	@if [ -z "$(DEPLOY_VPS_TAG)" ]; then \
	  echo "no .vps-version — write a tag (e.g. \`echo v0.39.0 > .vps-version\`) first"; exit 1; fi
	@if ! git rev-parse --verify "$(DEPLOY_VPS_TAG)" >/dev/null 2>&1; then \
	  echo "tag $(DEPLOY_VPS_TAG) not found locally — try \`git fetch --tags\`"; exit 1; fi
	@echo "→ deploy-vps (tag=$(DEPLOY_VPS_TAG), host=$(VPS_HOST), path=$(VPS_PROJECT))"
	@# git fetch + checkout the tag on the VPS. Done as the `apps` user
	@# (the owner of the project dir, non-sudoer per the security split).
	@# The container itself runs as root under sudo'd docker compose since
	@# apps isn't in the docker group.
	$(SSH_VPS) "sudo -u apps -H bash -c 'cd $(VPS_PROJECT) && git fetch --tags --force && git checkout $(DEPLOY_VPS_TAG)'"
	@# Build + up. --wait blocks until the healthcheck passes so a boot
	@# failure surfaces here instead of in a follow-up logs tail.
	@# Detect host arch on the VPS (uname -m is x86_64 or aarch64 →
	@# remap to amd64/arm64 the way Docker expects) and pass it as a
	@# build-arg so the temporal CLI fetch URL inside the Dockerfile
	@# resolves to the right binary. The Dockerfile defaults to arm64
	@# (Mac mini is M-series); without this override the VPS amd64 host
	@# downloads an arm64 temporal CLI binary → Exec format error.
	@# Build the new image (with arch arg) then roll the running service
	@# over via the docker-rollout plugin. Per infra/APP-PATTERN.md
	@# "Deploy strategy": single replica + rolling overlap, zero-downtime
	@# via traefik active healthcheck. prep's startup is slow-ish
	@# (~30-60s for temporal devserver + worker to come up) so we use
	@# -w 90 to wait long enough for the new container's /healthz to
	@# flip green before stopping the old.
	@# docker rollout doesn't accept --project-directory; cd into the
	@# project dir so relative -f paths resolve. -p prep so the new
	@# container ends up in the same compose project as the existing
	@# prep-prep-1 (without -p, compose picks "prep-app-staging" from
	@# the dir name and the new container lands in a different project
	@# space with no traefik_proxy network attachment).
	$(SSH_VPS) 'ARCH=$$(uname -m | sed -e s/x86_64/amd64/ -e s/aarch64/arm64/); echo "→ target arch: $$ARCH"; sudo docker compose --env-file $(OPS_DEPLOY_DIR)/.env -f $(VPS_PROJECT)/docker-compose.yml -f $(OPS_DEPLOY_DIR)/compose.yml --project-directory $(VPS_PROJECT) build --build-arg TARGETARCH=$$ARCH && cd $(VPS_PROJECT) && sudo docker rollout -p prep -f docker-compose.yml -f $(OPS_DEPLOY_DIR)/compose.yml --env-file $(OPS_DEPLOY_DIR)/.env -w 90 prep'
	@# Smoke check: hit the prepcards.app health surface from the VPS so
	@# we verify nginx → container path, not just container health.
	@$(SSH_VPS) "curl -sS -o /dev/null -w 'prepcards.app / → %{http_code}\\n' --max-time 10 https://prepcards.app/ || true"

promote-vps:
	@if [ -z "$(v)" ]; then echo "usage: make promote-vps v=v0.X.Y"; exit 1; fi
	@if ! git rev-parse --verify "$(v)" >/dev/null 2>&1; then \
	  echo "tag $(v) doesn't exist — create it first: \`git tag -a $(v) && git push --tags\`"; exit 1; fi
	@# Same pre-flight as `make promote`: rebuild devel from the tag,
	@# lint + test + e2e before mutating the VPS pin. Catches anything
	@# the contributor's working-tree dropped on the floor.
	@echo "→ pre-flight: redeploy devel from tag $(v) so e2e runs against the same code we'll ship"
	$(MAKE) deploy-devel-from-tag v=$(v)
	@echo "→ pre-flight: lint + python tests"
	$(MAKE) lint
	$(MAKE) test
	@echo "→ pre-flight: e2e against devel"
	$(MAKE) e2e
	@echo "→ promoting $(v) to prepcards.app"
	@echo "$(v)" > .vps-version
	git add .vps-version
	git commit -m "promote $(v) to prepcards.app"
	git push origin main
	$(MAKE) deploy-vps

logs-vps:
	$(SSH_VPS) "sudo docker compose --env-file $(OPS_DEPLOY_DIR)/.env -f $(VPS_PROJECT)/docker-compose.yml -f $(OPS_DEPLOY_DIR)/compose.yml --project-directory $(VPS_PROJECT) logs -f --tail=200"
