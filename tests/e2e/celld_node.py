"""A local celld node for the e2e suite.

`LocalCelldNode` carries the surface `LocalOfflineServer` had (`base_url`,
`seed`, `start`, `stop`), so a suite names a different fixture and keeps its
assertions. What changed underneath it:

- There is no database file. Rows are seeded through `POST /_parity/seed`
  and read back through `GET /_parity/dump`, both behind `X-Internal-Token`.
- Every request the browser makes carries that token beside the tailscale
  headers: the fake provider verifies nothing else, so the token is what
  stops any caller reaching any user's cell (decision 7.0).
- A node is heavier than a uvicorn, so a run drives ONE file. Fixtures are
  lazy and at most one node is live per invocation.

`stop()` leaves the port refusing, which is what the offline suites need:
`ctx.set_offline` does not reach a service worker, so killing the server is
still the only real offline simulation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Where the node's build and deploy come from. The override exists so a run
# can point at a worker tree that is already built.
WORKER_DIR = Path(os.environ.get("PREP_E2E_WORKER_DIR") or (REPO_ROOT / "worker"))
RUN_NODE = WORKER_DIR / "scripts" / "run-node.sh"

INTERNAL_TOKEN = "parity-internal-token"
INTERNAL_TOKEN_HEADER = "x-internal-token"

# The scratch MinIO the node deploys to. Its root credential is the
# operator's, never a default here, so a box without one skips.
S3_BUCKET = os.environ.get("PREP_DEV_S3_BUCKET", "prep-dev")
MINIO_CONTAINER = os.environ.get("PREP_DEV_MINIO_CONTAINER", "celld-scratch-minio")

# The master key run-node.sh gives the node; a deterministic test value, not
# a credential. The anonymous cookie's signing key is derived from it.
MASTER_KEY = "ab" * 32

OFFLINE_E2E_LOGIN = "offline-e2e@example.com"
OFFLINE_E2E_NAME = "Offline Tester"

# Where a node keeps its working directory. Short on purpose: celld opens
# unix sockets under it, and macOS caps a socket path at 104 bytes, which the
# per-user temp directory alone can eat most of.
STATE_ROOT = Path(os.environ.get("PREP_E2E_STATE_ROOT") or "/tmp")

# Enough for the deploy plus the restart's lease expiry (6-8 s) with room for
# a cold isolate on a loaded box.
START_TIMEOUT = 90.0

# The id whose cell the readiness probe reads. Anonymous, because a probe has
# to work under every provider shape a suite deploys, and only the anonymous
# cookie is provider-independent.
PROBE_ANON_ID = "anon:" + "00" * 16


def identity_headers(login: str, name: str | None = None) -> dict[str, str]:
    headers = {"tailscale-user-login": login, INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN}
    if name:
        headers["tailscale-user-name"] = name
    return headers


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def require_scratch_storage() -> None:
    """The node needs the scratch MinIO's root credential and the celld
    binary; without either the suite has nothing to run against."""
    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        pytest.skip(
            "the local celld node needs AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY "
            "for the scratch MinIO (worker/scripts/run-node.sh)"
        )
    celld = Path(os.environ.get("CELLD_BIN") or (Path.home() / ".local" / "bin" / "celld"))
    if not celld.exists():
        pytest.skip(f"celld binary not found at {celld}; set CELLD_BIN")
    if not RUN_NODE.exists():
        pytest.skip(f"{RUN_NODE} not found; set PREP_E2E_WORKER_DIR")


class LocalCelldNode:
    """One celld node on its own port, state dir and bucket prefix.

    `vars` become `CELLD_VAR_*`, which the node reads at startup: a differing
    deploy shape (a clerk-mode landing, an opened-up limiter) is this same
    build started with a different environment. `script_env` reaches
    run-node.sh itself, for the few knobs it owns rather than passes through.
    """

    def __init__(
        self,
        name: str,
        *,
        vars: dict[str, str] | None = None,
        script_env: dict[str, str] | None = None,
    ):
        self.name = name
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.seed: dict = {}
        # The dev deploy pins the clock for the recorded corpus. e2e drives a
        # browser, which writes real timestamps: a pinned server would clamp
        # every one of them. Unpinned unless a suite says otherwise.
        self.vars: dict[str, str] = {"PREP_FAKE_NOW": "", **(vars or {})}
        self.script_env: dict[str, str] = dict(script_env or {})
        self.state_dir = STATE_ROOT / f"prep-e2e-{name}"
        self.bucket = f"{S3_BUCKET}/e2e-{name}"
        self._deployed = False

    # ---- process ------------------------------------------------------

    def _run(self, *args: str, extra: dict[str, str] | None = None) -> None:
        env = {**os.environ}
        env["PREP_DEV_PORT"] = str(self.port)
        env["PREP_DEV_STATE_DIR"] = str(self.state_dir)
        env["PREP_DEV_S3_BUCKET"] = self.bucket
        env.update(self.script_env)
        for key, value in self.vars.items():
            env[f"CELLD_VAR_{key}"] = value
        env.update(extra or {})
        proc = subprocess.run(
            ["bash", str(RUN_NODE), *args],
            cwd=str(WORKER_DIR),
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"run-node.sh {' '.join(args)} for {self.name}: exit {proc.returncode}\n"
                f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
            )

    def _clear_state(self) -> None:
        """Empty this node's bucket prefix and working directory before its
        first deploy.

        A prefix is a fleet. Every run picks a fresh port, so the node ids a
        previous run left in the log ensemble advertise addresses that are
        dead or now somebody else's, and a write that waits on their ack
        fails with `peer response has no protocol version`. The two halves go
        together: a working directory holding replicas of cells the bucket no
        longer has restores nothing, and every read answers `RestoreFailed`.
        """
        shutil.rmtree(self.state_dir, ignore_errors=True)
        subprocess.run(
            [
                "docker",
                "exec",
                MINIO_CONTAINER,
                "sh",
                "-c",
                f"mc alias set local http://127.0.0.1:9000 "
                f"'{os.environ['AWS_ACCESS_KEY_ID']}' '{os.environ['AWS_SECRET_ACCESS_KEY']}' >/dev/null && "
                f"mc rm --recursive --force 'local/{self.bucket}' >/dev/null 2>&1 || true",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def start(self, timeout: float = START_TIMEOUT) -> None:
        """Idempotent. The first call empties this node's prefix and working
        directory and deploys the session's build; a restart reuses both.

        `/healthz` answering is not enough: cells stay unreachable for the
        lease TTL after a node restart, so this waits on a real cell read.
        """
        if self._listening():
            return
        if not self._deployed:
            self._clear_state()
        self._run(extra={"SKIP_BUILD": "1", "SKIP_DEPLOY": "1" if self._deployed else "0"})
        self._deployed = True
        self._await_cells(timeout)

    def stop(self) -> None:
        self._run("stop")

    def _listening(self) -> bool:
        try:
            return httpx.get(f"{self.base_url}/healthz", timeout=1.0).status_code == 200
        except httpx.HTTPError:
            return False

    def _await_cells(self, timeout: float) -> None:
        """A cell read, not `/healthz`: the node answers liveness while its
        lease is still expiring and every cell refuses.

        The probe presents an anonymous cookie and no identity headers. An
        identity the node's provider cannot verify answers 401 from the
        router without a cell being touched, so accepting one would degrade
        this to `/healthz` on exactly the clerk-shaped nodes that need the
        wait. Every provider resolves a valid `prep_anon` to a cell.
        """
        deadline = time.time() + timeout
        cookie = {"cookie": f"prep_anon={mint_anon_cookie(PROBE_ANON_ID)}"}
        last = ""
        while time.time() < deadline:
            try:
                r = httpx.get(
                    f"{self.base_url}/api/dashboard/overview", headers=cookie, timeout=5.0
                )
                # Only a 200 proves a cell answered. Anything else is the
                # router: a lease still expiring, or a shape this probe does
                # not fit, and neither means the node is ready.
                if r.status_code == 200:
                    return
                last = f"{r.status_code} {r.text[:200]}"
            except httpx.HTTPError as e:
                last = str(e)
            time.sleep(0.25)
        raise RuntimeError(
            f"{self.name}: cells unreachable after {timeout}s ({last}); log: {self.state_dir}/node.log"
        )

    # ---- data ---------------------------------------------------------

    def seed_profile(self, user: str, profile: str) -> dict:
        """`POST /_parity/seed`, retrying a 5xx.

        A single node runs its log ensemble degraded and can answer a write
        with a refusal, which the runtime documents as retryable. A 4xx is
        the request's own fault and raises on the first answer.
        """
        for attempt in range(1, 5):
            r = httpx.post(
                f"{self.base_url}/_parity/seed",
                json={"user": user, "profile": profile},
                headers={"X-Internal-Token": INTERNAL_TOKEN},
                timeout=60.0,
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code < 500 or attempt == 4:
                raise RuntimeError(f"seed {profile!r} for {user!r}: {r.status_code} {r.text[:300]}")
            time.sleep(attempt)
        raise AssertionError("unreachable")

    def dump(self, user: str) -> dict:
        r = httpx.get(
            f"{self.base_url}/_parity/dump",
            params={"user": user},
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=60.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"dump {user!r}: {r.status_code} {r.text[:300]}")
        return r.json()

    def rows(self, user: str, table: str) -> list[dict]:
        """One table of one cell, in rowid order. The replacement for opening
        the server's database file, which a cell does not have."""
        return self.dump(user)["tables"].get(table) or []

    def profile_row(self, user: str) -> dict | None:
        return self.dump(user)["profile"]

    def dump_directory(self) -> dict[str, list[dict]]:
        """The directory cell's own tables: the enumeration rows, the merge
        audit and its markers, the tombstones."""
        r = httpx.get(
            f"{self.base_url}/_parity/dump",
            params={"cell": "directory"},
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=60.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"dump directory: {r.status_code} {r.text[:300]}")
        return r.json()["tables"]


def mint_anon_cookie(external_id: str, issued_at: int | None = None) -> str:
    """`prep_anon` for an id, signed with the key the local node derives.

    `v1.<id>.<iat>.<sig>`: 16 id bytes and the first 16 bytes of
    HMAC-SHA256 over the payload, both unpadded base64url. The key is
    HKDF-SHA256 of the master key under `prep-anon-cookie-v1`, which is what
    the node does when no explicit cookie secret is set.
    """
    raw = external_id.removeprefix("anon:")
    key = hkdf_sha256(bytes.fromhex(MASTER_KEY), b"prep-anon-cookie-v1", 32)
    payload = (
        f"v1.{_b64u(bytes.fromhex(raw))}.{issued_at if issued_at is not None else int(time.time())}"
    )
    sig = hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16]
    return f"{payload}.{_b64u(sig)}"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def hkdf_sha256(ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(b"\x00" * hashlib.sha256().digest_size, ikm, hashlib.sha256).digest()
    out, block = b"", b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def llm_stub_env(base_url: str) -> dict[str, str]:
    """run-node.sh owns the free-tier vars, so the stub's address reaches the
    node through the knob the script reads."""
    return {"PREP_DEV_LLM_BASE_URL": base_url}


def open_limiter_vars() -> dict[str, str]:
    """Several tests generate for real from one client IP. The limiter's own
    behaviour is pinned by the route tests, which can control the clock."""
    return {
        "PREP_INSTANT_BURST_LIMIT": "100",
        "PREP_INSTANT_PER_IP_PER_DAY": "100",
        "PREP_INSTANT_GLOBAL_PER_MINUTE": "100",
        "PREP_INSTANT_GLOBAL_PER_DAY": "500",
    }


def clerk_vars(base_url: str) -> dict[str, str]:
    """The public deploy shape: an unidentified visitor gets the landing and
    the provider exposes a hosted sign-in URL. Parity mode goes off with it,
    because the two providers are exclusive; nothing here navigates to the
    (unreachable) identity host."""
    return {
        "PREP_PARITY_MODE": "",
        "PREP_FAKE_NOW": "",
        "PREP_PARITY_NO_PERIODIC": "",
        "CLERK_ISSUER": "https://accounts.example.test",
        "CLERK_JWKS_URL": "https://accounts.example.test/.well-known/jwks.json",
        "CLERK_AUTHORIZED_PARTIES": base_url,
        "CLERK_ACCOUNTS_URL": "https://accounts.example.test",
        # `pk_test_` plus base64("accounts.example.test$"), the shape the
        # frontend-API host is decoded from.
        "CLERK_PUBLISHABLE_KEY": "pk_test_YWNjb3VudHMuZXhhbXBsZS50ZXN0JA==",
        "CLERK_SECRET_KEY": "sk_test_e2e_dummy",
    }
