"""The job stack a phase-4 pixel flow needs behind the Python target.

The Python app owns no job state: `/plan`, `/transform`, `/trivia/gen`
and `/api/study/grading` all read their progress back out of Temporal.
A local target therefore has to run the Procfile's other two processes,
a Temporal devserver and the Go worker, or every phase-4 screen renders
`gone`. The worker reaches the LLM through the app's own
`/api/agent/run`, so the canned stub covers the worker's calls too.

Started only when the run asks for phase 4 or later and the target is
local; a remote target runs its own.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from tests.e2e.conftest import _free_port
from tests.parity.harness.constants import REPO_ROOT

NAMESPACE = "prep"
WORKER_BIN = REPO_ROOT / "worker-go" / "bin" / "worker"
LOG_DIR_ENV = "PARITY_JOBS_LOG_DIR"


def _sinks(name: str):
    """Both streams to a file under `PARITY_JOBS_LOG_DIR`, else dropped."""
    directory = (os.environ.get(LOG_DIR_ENV) or "").strip()
    if not directory:
        return subprocess.DEVNULL, subprocess.DEVNULL
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    handle = (path / f"{name}.log").open("w")
    return handle, subprocess.STDOUT


class MissingTool(RuntimeError):
    """A binary the stack needs is not installed."""


def _wait_for_port(host: str, port: int, *, timeout_s: float, what: str) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(0.2)
    raise RuntimeError(f"{what} did not open {host}:{port} within {timeout_s}s")


def build_worker() -> Path:
    """The Go worker binary, built if `make build` has not run."""
    if WORKER_BIN.is_file():
        return WORKER_BIN
    if shutil.which("go") is None:
        raise MissingTool("go is not installed; the phase-4 flows need the Go worker")
    subprocess.run(
        ["go", "build", "-o", str(WORKER_BIN), "."],
        cwd=REPO_ROOT / "worker-go",
        check=True,
    )
    return WORKER_BIN


class JobsStack:
    """A Temporal devserver plus the Go worker, both scoped to one run."""

    def __init__(self, db_path: Path, state_dir: Path):
        self.db_path = Path(db_path)
        self.state_dir = Path(state_dir)
        self.port = _free_port()
        self.host_port = f"127.0.0.1:{self.port}"
        self._temporal: subprocess.Popen | None = None
        self._worker: subprocess.Popen | None = None

    @property
    def env(self) -> dict[str, str]:
        """What the app and the worker both need to find this server."""
        return {"TEMPORAL_HOST_PORT": self.host_port, "TEMPORAL_NAMESPACE": NAMESPACE}

    def start_temporal(self, timeout_s: float = 90.0) -> None:
        if shutil.which("temporal") is None:
            raise MissingTool("the temporal CLI is not installed; the phase-4 flows need it")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporal_out, temporal_err = _sinks("temporal")
        self._temporal = subprocess.Popen(
            [
                "temporal",
                "server",
                "start-dev",
                "--namespace",
                NAMESPACE,
                "--port",
                str(self.port),
                "--db-filename",
                str(self.state_dir / "temporal.db"),
                "--headless",
                "--log-level",
                "error",
            ],
            cwd=REPO_ROOT,
            stdout=temporal_out,
            stderr=temporal_err,
        )
        _wait_for_port("127.0.0.1", self.port, timeout_s=timeout_s, what="temporal devserver")

    def start_worker(self, *, app_base_url: str, internal_token: str) -> None:
        binary = build_worker()
        env = {**os.environ, **self.env}
        env.update(
            {
                "PREP_DB_PATH": str(self.db_path),
                "PREP_AGENT_URL": f"{app_base_url.rstrip('/')}/api/agent",
                "PREP_INTERNAL_TOKEN": internal_token,
            }
        )
        # A CLI on the box would otherwise be picked up as the agent and
        # spend real tokens; the app's endpoint is the only agent here.
        env.pop("PREP_AGENT_BIN", None)
        env.pop("CLAUDE_BIN", None)
        worker_out, worker_err = _sinks("worker")
        self._worker = subprocess.Popen(
            [str(binary)], cwd=REPO_ROOT, env=env, stdout=worker_out, stderr=worker_err
        )

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        cmd = ["temporal", *args, "--address", self.host_port, "--namespace", NAMESPACE]
        return subprocess.run(cmd, capture_output=True, text=True)

    def _cli_ok(self, *args: str) -> None:
        done = self._cli(*args)
        if done.returncode != 0:
            raise RuntimeError(
                f"temporal {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}"
            )

    def abandon_workflow(self, workflow_id: str, reason: str = "parity") -> None:
        """Terminate a running workflow and delete its execution.

        Terminating alone is not enough: a query replays a closed
        workflow's history and answers from the handler it re-registers,
        so the progress dict survives. Deleting the execution is what
        makes the query fail, which is the `gone` the partial renders.
        """
        self._cli_ok("workflow", "terminate", "--workflow-id", workflow_id, "--reason", reason)
        self._cli_ok("workflow", "delete", "--workflow-id", workflow_id, "--yes")
        # Deletion is a background task on the server; block until the
        # execution is really unreachable so the caller can rely on the
        # next poll rendering `gone`.
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._cli("workflow", "describe", "--workflow-id", workflow_id).returncode != 0:
                return
            time.sleep(0.5)
        raise RuntimeError(f"{workflow_id} still exists 60s after delete")

    def stop(self) -> None:
        for proc in (self._worker, self._temporal):
            if proc is None or proc.poll() is not None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        self._worker = None
        self._temporal = None
