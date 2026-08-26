"""A scratch app for the DB-backed oracles: fresh sqlite, the parity
env, the real FastAPI app under `TestClient`, and every source of
randomness the corpora would otherwise see replaced by a seeded one.

Runs inside pytest (where `prep` is already imported under another
environment) and standalone alike: nothing here relies on import
order, only on module attributes being re-pointed for the duration
of the context.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import random
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.parity.oracles import (
    PARITY_BUILD_ID,
    PARITY_INTERNAL_TOKEN,
    PARITY_USER,
    PARITY_USER_NAME,
    ClockPin,
    pin_clock,
)

MASTER_KEY = "11" * 32
FREE_TIER_STUB = "http://127.0.0.1:9/v1"

PARITY_ENV = {
    "PREP_KEY_ENCRYPTION_SECRET": MASTER_KEY,
    "PREP_INTERNAL_TOKEN": PARITY_INTERNAL_TOKEN,
    "PREP_BUILD_ID": PARITY_BUILD_ID,
    "PREP_PLACEHOLDER_INDEX": "0",
    "PREP_FREE_INFERENCE_BASE_URL": FREE_TIER_STUB,
    "PREP_FREE_INFERENCE_API_KEY": "parity-free-tier-key",
    "PREP_FREE_INFERENCE_MODEL": "parity-model",
    "TEMPORAL_HOST_PORT": "127.0.0.1:0",
    "PREP_CLIENT_IP_HEADER": "x-real-ip",
    "PREP_PARITY_MODE": "1",
}
UNSET_ENV = ("PREP_DEFAULT_USER", "PREP_AUTH_MODE", "PREP_ANON_COOKIE_SECRET", "ROOT_PATH")


class SeededSecrets:
    """Stand-in for the `secrets` module: the same shapes, drawn from a
    seeded generator so ids and tokens repeat run to run."""

    def __init__(self, seed: int):
        self._rng = random.Random(seed)

    def token_bytes(self, n: int) -> bytes:
        return bytes(self._rng.getrandbits(8) for _ in range(n))

    def token_hex(self, n: int) -> str:
        return self.token_bytes(n).hex()

    def token_urlsafe(self, n: int) -> str:
        return base64.urlsafe_b64encode(self.token_bytes(n)).decode("ascii").rstrip("=")

    def choice(self, seq):
        return self._rng.choice(seq)


class SessionIds:
    def __init__(self):
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return hashlib.sha1(f"parity-session-{self.n}".encode()).hexdigest()[:16]


@dataclass
class Harness:
    client: Any
    db_path: Path
    clock: ClockPin
    tmp: Path
    recorded: list[dict] = field(default_factory=list)

    @staticmethod
    def headers(login: str = PARITY_USER, name: str = PARITY_USER_NAME) -> dict[str, str]:
        return {"Tailscale-User-Login": login, "Tailscale-User-Name": name}

    def seed(self, user: str, profile: str) -> dict:
        """Wipe `user` and insert the named profile, in process."""
        from prep.dev.parity_seed import seed

        return seed(user, profile)

    def call(
        self,
        name: str,
        method: str,
        path: str,
        *,
        headers: dict | None = None,
        json_body: Any = None,
        data: Any = None,
        content: bytes | str | None = None,
        note: str | None = None,
    ) -> Any:
        """Issue one request, record `{name, request, response}`, and
        return the response."""
        kwargs: dict[str, Any] = {"headers": headers or {}}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if content is not None:
            kwargs["content"] = content
        response = self.client.request(method, path, **kwargs)
        self.recorded.append(
            {
                "name": name,
                "note": note,
                "request": {
                    "method": method,
                    "path": path,
                    "headers": dict(kwargs["headers"]),
                    "json": json_body,
                    "form": data,
                    "text": content.decode() if isinstance(content, bytes) else content,
                },
                "response": describe_response(response),
            }
        )
        return response


def describe_response(response) -> dict:
    content_type = response.headers.get("content-type", "")
    body: Any
    if "application/json" in content_type:
        body = response.json()
        text = None
    else:
        body = None
        text = response.text
    return {
        "status": response.status_code,
        "content_type": content_type,
        "set_cookie": response.headers.get_list("set-cookie"),
        "location": response.headers.get("location"),
        "json": body,
        "text": text,
    }


def _set_env(values: dict[str, str], unset: tuple[str, ...]) -> dict[str, str | None]:
    saved: dict[str, str | None] = {}
    for key, value in values.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    for key in unset:
        saved[key] = os.environ.get(key)
        os.environ.pop(key, None)
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@contextlib.contextmanager
def scratch_app(*, seed: int = 20260314, raise_server_exceptions: bool = True) -> Iterator[Harness]:
    """The app against a fresh sqlite under the parity env, seeded
    randomness, and the anonymous-cookie provider over Tailscale
    headers. Everything re-pointed is restored on exit.
    `raise_server_exceptions=False` lets a deliberate 500 render its
    page instead of surfacing in the caller."""
    tmp = Path(tempfile.mkdtemp(prefix="prep-parity-"))
    db_path = tmp / "parity.sqlite"
    saved_env = _set_env(
        {
            **PARITY_ENV,
            "PREP_DB_PATH": str(db_path),
            "PREP_VAPID_KEYS_PATH": str(tmp / "vapid-keys.json"),
            "PREP_VAPID_PEM_PATH": str(tmp / "vapid-private.pem"),
        },
        UNSET_ENV,
    )
    with pin_clock() as clock:
        from prep.infrastructure import db as db_mod

        previous_db_path = db_mod.DB_PATH
        db_mod.DB_PATH = db_path

        import prep.app as app_mod
        from prep import agent as agent_mod
        from prep.api import repo as api_repo
        from prep.auth import anon_cookie
        from prep.auth import merge as merge_mod
        from prep.auth.providers import set_provider
        from prep.auth.providers.anon import AnonymousFallbackProvider
        from prep.auth.providers.tailscale import TailscaleProvider
        from prep.instant import repo as instant_repo
        from prep.notify import push
        from prep.study import repo as study_repo
        from prep.web import templates as templates_mod

        patched = [
            (instant_repo, "secrets", SeededSecrets(seed)),
            (merge_mod, "secrets", SeededSecrets(seed + 1)),
            (api_repo, "secrets", SeededSecrets(seed + 2)),
            (study_repo, "_new_session_id", SessionIds()),
            (push, "_KEYS_PATH", tmp / "vapid-keys.json"),
            (push, "_KEY_PEM_PATH", tmp / "vapid-private.pem"),
            (templates_mod, "_BUILD_TOKEN", PARITY_BUILD_ID),
            # No deploy-wide agent under the parity env; the probe flag is
            # process state another test may have flipped.
            (agent_mod, "is_available", False),
        ]
        originals = [(mod, attr, getattr(mod, attr)) for mod, attr, _ in patched]
        for mod, attr, value in patched:
            setattr(mod, attr, value)
        anon_cookie._resolve_secret.cache_clear()
        set_provider(AnonymousFallbackProvider(TailscaleProvider()))

        from fastapi.testclient import TestClient

        try:
            with TestClient(
                app_mod.app,
                base_url="https://parity.example.test",
                follow_redirects=False,
                raise_server_exceptions=raise_server_exceptions,
            ) as client:
                yield Harness(client=client, db_path=db_path, clock=clock, tmp=tmp)
        finally:
            set_provider(None)
            for mod, attr, value in originals:
                setattr(mod, attr, value)
            anon_cookie._resolve_secret.cache_clear()
            db_mod.DB_PATH = previous_db_path
            _restore_env(saved_env)


def table_rows(db_path: Path, table: str, column: str, user_id: str) -> list[dict]:
    """Every row of `table` owned by `user_id`, in primary-key order,
    as plain dicts."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f'SELECT * FROM "{table}" WHERE "{column}" = ? ORDER BY rowid', (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def all_rows(db_path: Path, table: str) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()]
    finally:
        conn.close()


def jsonable(value: Any) -> Any:
    """Round-trip through JSON so sets and dataclasses in recorded
    payloads become plain data."""
    return json.loads(json.dumps(value, default=_default, sort_keys=True))


def _default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value)


# ---- a remote target -------------------------------------------------------


class RemoteClockPin:
    """`clock.set(at)` for a remote server: the instant travels as the
    `X-Parity-Now` header on every later call."""

    def __init__(self, at):
        self.at = at

    def set(self, at) -> None:
        self.at = at

    def unix(self) -> int:
        return int(self.at.timestamp())


@dataclass
class RemoteHarness:
    """The `Harness` surface over HTTP against a TypeScript server: seeds
    through `POST /_parity/seed`, sends the internal token and the
    request clock, never follows redirects."""

    client: Any
    token: str
    clock: RemoteClockPin
    recorded: list[dict] = field(default_factory=list)

    def headers(self, login: str = PARITY_USER, name: str = PARITY_USER_NAME) -> dict[str, str]:
        return {**Harness.headers(login, name), "X-Internal-Token": self.token}

    def seed(self, user: str, profile: str) -> dict:
        response = self.client.post(
            "/_parity/seed",
            json={"user": user, "profile": profile},
            headers={"X-Internal-Token": self.token, **self._clock_header()},
        )
        response.raise_for_status()
        return response.json()

    def _clock_header(self) -> dict[str, str]:
        return {"X-Parity-Now": self.clock.at.isoformat().replace("+00:00", "Z")}

    def call(
        self,
        name: str,
        method: str,
        path: str,
        *,
        headers: dict | None = None,
        json_body: Any = None,
        data: Any = None,
        content: bytes | str | None = None,
        note: str | None = None,
    ) -> Any:
        sent = {**(headers or {})}
        kwargs: dict[str, Any] = {"headers": {**sent, **self._clock_header()}}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if content is not None:
            kwargs["content"] = content
        response = self.client.request(method, path, **kwargs)
        self.recorded.append(
            {
                "name": name,
                "note": note,
                "request": {
                    "method": method,
                    "path": path,
                    "headers": sent,
                    "json": json_body,
                    "form": data,
                    "text": content.decode() if isinstance(content, bytes) else content,
                },
                "response": describe_response(response),
            }
        )
        return response


@contextlib.contextmanager
def remote_app(base_url: str, token: str = PARITY_INTERNAL_TOKEN) -> Iterator[RemoteHarness]:
    """The harness against a running parity server at `base_url`."""
    import httpx

    from tests.parity.harness.constants import PARITY_NOW

    with httpx.Client(base_url=base_url, follow_redirects=False, timeout=30.0) as client:
        yield RemoteHarness(client=client, token=token, clock=RemoteClockPin(PARITY_NOW))
