"""The parity target (docs/PARITY-GATE.md C7): a local uvicorn in the
section 0 env, or a remote base URL, either way seeded through
`POST /_parity/seed`."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from tests.e2e.conftest import _TEST_KEY_ENCRYPTION_SECRET, _free_port
from tests.parity.harness.constants import (
    PARITY_BUILD_ID,
    PARITY_INTERNAL_TOKEN,
    PARITY_NOW_ISO,
    REPO_ROOT,
)

BASE_URL_ENV = "PARITY_BASE_URL"
INTERNAL_TOKEN_ENV = "PARITY_INTERNAL_TOKEN"


def seed(base_url: str, user: str, profile: str, *, token: str | None = None) -> dict:
    token = token or os.environ.get(INTERNAL_TOKEN_ENV) or PARITY_INTERNAL_TOKEN
    r = httpx.post(
        f"{base_url}/_parity/seed",
        json={"user": user, "profile": profile},
        headers={"X-Internal-Token": token},
        timeout=60.0,
    )
    if r.status_code != 200:
        raise RuntimeError(f"seed {profile!r} for {user!r}: {r.status_code} {r.text[:300]}")
    return r.json()


def parity_env(db_path: Path, llm_base_url: str) -> dict[str, str]:
    env = {**os.environ}
    env.pop("PREP_DEFAULT_USER", None)
    env.pop("PREP_AUTH_MODE", None)
    env.update(
        {
            "PREP_DB_PATH": str(db_path),
            "PREP_KEY_ENCRYPTION_SECRET": _TEST_KEY_ENCRYPTION_SECRET,
            "PREP_FAKE_NOW": PARITY_NOW_ISO,
            "PARITY_FREEZE_NOW": PARITY_NOW_ISO,
            "PREP_PARITY_MODE": "1",
            "PREP_BUILD_ID": PARITY_BUILD_ID,
            "PREP_INTERNAL_TOKEN": PARITY_INTERNAL_TOKEN,
            "PREP_PLACEHOLDER_INDEX": "0",
            "PREP_FREE_INFERENCE_BASE_URL": llm_base_url,
            "PREP_FREE_INFERENCE_API_KEY": "parity-free-tier-key",
            "PREP_FREE_INFERENCE_MODEL": "parity-model",
        }
    )
    return env


class ParityTarget:
    """A base URL plus the seed call. Local instances own a uvicorn."""

    def __init__(self, base_url: str, *, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def seed(self, user: str, profile: str) -> dict:
        return seed(self.base_url, user, profile, token=self.token)


class LocalParityServer(ParityTarget):
    def __init__(self, db_path: Path, llm_base_url: str):
        self.db_path = Path(db_path)
        self.llm_base_url = llm_base_url
        self.port = _free_port()
        super().__init__(f"http://127.0.0.1:{self.port}", token=PARITY_INTERNAL_TOKEN)
        self._proc: subprocess.Popen | None = None

    def start(self, timeout: float = 45.0) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.parity.harness.serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=REPO_ROOT,
            env=parity_env(self.db_path, self.llm_base_url),
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"parity server exited during startup ({self._proc.returncode})")
            try:
                if httpx.get(f"{self.base_url}/healthz", timeout=1.0).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError("parity server did not become healthy in time")

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=10)
        self._proc = None
