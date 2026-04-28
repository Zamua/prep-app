# Process manifest for `make dev`. Run with `goreman start` (or any other
# Procfile runner: overmind, forego, hivemind, honcho).
#
# Each line: <name>: <command>
# All processes run from the repo root. Stdout/stderr is muxed by the
# runner. Ctrl-C cleans them all up.

# Temporal devserver — embedded SQLite-backed, listens on 127.0.0.1:7233 (gRPC)
# and :8233 (Web UI). Stores history under ./temporal-data/ (gitignored).
temporal: temporal server start-dev --db-filename ./temporal-data/temporal.db --namespace prep --log-level warn

# FastAPI app via uvicorn. Binds 127.0.0.1:8081 directly — for tailscale-served
# prod, see ARCHITECTURE.md (or CLAUDE.md). For dev, hit http://127.0.0.1:8081/
# directly (PREP_DEFAULT_USER bypasses auth — see Makefile).
app: .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8081 --reload

# Go worker — durable card generation + grading + transform workflows.
# Built by `make build`; binary lives at worker-go/bin/worker.
worker: ./worker-go/bin/worker
