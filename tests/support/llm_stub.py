"""Canned OpenAI-compatible chat-completions server for local AI flows.

Replay is keyed on the request's `messages` alone; every other field
is ignored, so a model rename never invalidates the fixture set. A hit
is served byte for byte under fixed `Server` and `Date` headers. A
miss is a 404 plus a note under `<fixtures>/missing/`, or, with
recording on, one forwarded call to the real upstream whose answer
becomes the fixture.

Standalone:

    python -m tests.support.llm_stub --port 8089 --fixtures <dir> [--record]

In-process: `LLMStub(...)`, or the `llm_stub` session fixture.
Control endpoints (POST unless noted) under `/_control/`: `hold`,
`release`, `GET held`, `latency` `{"ms"}`, `reset`, `GET requests`,
`canned` `{"content"}` (the answer served for any request with no
fixture, `null` to clear).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
MISSING_SUBDIR = "missing"

SERVER_HEADER = "prep-llm-stub"
DATE_HEADER = "Sat, 14 Mar 2026 15:00:00 GMT"

# A held request answers 503 after this so a forgotten hold cannot
# hang a run.
HOLD_TIMEOUT_S = 120.0

RECORD_ENV = "PARITY_LLM_RECORD"
UPSTREAM_ENV = {name: f"PARITY_LLM_UPSTREAM_{name}" for name in ("BASE_URL", "API_KEY", "MODEL")}


# ---- fixtures ----------------------------------------------------------------


def request_key(messages: object) -> str:
    canonical = json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fixture_path(fixtures: Path, key: str) -> Path:
    return Path(fixtures) / f"{key[:16]}.json"


def write_fixture(fixtures: Path, messages: object, body: str) -> Path:
    """Store `body`, the exact upstream response text, under the key
    of `messages`."""
    key = request_key(messages)
    path = fixture_path(fixtures, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"key": key, "messages": messages, "body": body}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_fixture(fixtures: Path, key: str) -> str | None:
    path = fixture_path(fixtures, key)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("key") != key:
        # A renamed or hand-edited file must not serve under a key it
        # was not recorded for.
        raise ValueError(f"{path} holds key {data.get('key')!r}, expected {key!r}")
    body = data.get("body")
    if not isinstance(body, str):
        raise ValueError(f"{path}: body must be the upstream response text")
    return body


def note_missing(fixtures: Path, key: str, body: object) -> Path:
    path = Path(fixtures) / MISSING_SUBDIR / f"{key[:16]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ---- recorder ----------------------------------------------------------------


@dataclass(frozen=True)
class Upstream:
    """The real endpoint a miss is forwarded to while recording."""

    base_url: str
    api_key: str
    model: str

    @classmethod
    def from_env(cls) -> Upstream:
        values = {name: (os.environ.get(var) or "").strip() for name, var in UPSTREAM_ENV.items()}
        missing = [UPSTREAM_ENV[name] for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"recording needs {', '.join(missing)}")
        return cls(values["BASE_URL"].rstrip("/"), values["API_KEY"], values["MODEL"])

    def complete(self, body: dict, *, timeout_s: float = 300.0) -> tuple[int, bytes]:
        """Forward one chat-completions body; the upstream model replaces
        whatever the caller named."""
        payload = dict(body, model=self.model)
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


# ---- server ----------------------------------------------------------------


class _State:
    def __init__(
        self,
        fixtures: Path,
        *,
        record: bool,
        upstream: Upstream | None,
        hold_timeout_s: float,
        verbose: bool,
    ):
        self.fixtures = Path(fixtures)
        self.record = record
        self.upstream = upstream
        self.hold_timeout_s = hold_timeout_s
        self.verbose = verbose
        self.lock = threading.Lock()
        self.holding = False
        self.release_event = threading.Event()
        self.held: list[str] = []
        self.latency_ms = 0
        self.requests: list[str] = []
        self.canned: str | None = None

    def hold(self) -> None:
        with self.lock:
            self.holding = True
            self.release_event = threading.Event()

    def release(self) -> None:
        with self.lock:
            self.holding = False
            self.release_event.set()

    def reset(self) -> None:
        self.release()
        with self.lock:
            self.latency_ms = 0
            self.requests.clear()
            self.canned = None


class LLMStubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: _State):
        self.state = state
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: LLMStubServer

    def version_string(self) -> str:
        return SERVER_HEADER

    def date_time_string(self, timestamp: float | None = None) -> str:
        return DATE_HEADER

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        if self.server.state.verbose:
            super().log_message(format, *args)

    # -- plumbing --

    def _read_json(self) -> object:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def _send_raw(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: object) -> None:
        self._send_raw(status, json.dumps(payload).encode("utf-8"), "application/json")

    # -- routes --

    def do_GET(self) -> None:  # noqa: N802
        state = self.server.state
        if self.path == "/_control/held":
            with state.lock:
                keys = list(state.held)
            self._send_json(200, {"count": len(keys), "keys": keys})
        elif self.path == "/_control/requests":
            with state.lock:
                keys = list(state.requests)
            self._send_json(200, {"count": len(keys), "keys": keys})
        else:
            self._send_json(404, {"error": f"no route for GET {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_json()
        except ValueError:
            self._send_json(400, {"error": "request body is not JSON"})
            return
        state = self.server.state
        if self.path == "/v1/chat/completions":
            self._completion(body)
        elif self.path == "/_control/hold":
            state.hold()
            self._send_json(200, {"holding": True})
        elif self.path == "/_control/release":
            state.release()
            self._send_json(200, {"holding": False})
        elif self.path == "/_control/latency":
            ms = body.get("ms") if isinstance(body, dict) else None
            if not isinstance(ms, (int, float)) or ms < 0:
                self._send_json(400, {"error": "latency needs a non-negative ms"})
                return
            with state.lock:
                state.latency_ms = ms
            self._send_json(200, {"ms": ms})
        elif self.path == "/_control/canned":
            content = body.get("content") if isinstance(body, dict) else None
            if content is not None and not isinstance(content, str):
                self._send_json(400, {"error": "canned needs a string content, or null"})
                return
            with state.lock:
                state.canned = content
            self._send_json(200, {"canned": content is not None})
        elif self.path == "/_control/reset":
            state.reset()
            self._send_json(200, {"reset": True})
        else:
            self._send_json(404, {"error": f"no route for POST {self.path}"})

    def _completion(self, body: object) -> None:
        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list):
            self._send_json(400, {"error": "messages must be a list"})
            return
        key = request_key(messages)
        state = self.server.state
        with state.lock:
            state.requests.append(key)
            holding = state.holding
            event = state.release_event
            if holding:
                state.held.append(key)
        if holding:
            released = event.wait(state.hold_timeout_s)
            with state.lock:
                state.held.remove(key)
            if not released:
                self._send_json(503, {"error": f"held request timed out for {key}"})
                return
        if state.latency_ms:
            time.sleep(state.latency_ms / 1000)

        try:
            text = load_fixture(state.fixtures, key)
        except ValueError as e:
            self._send_json(500, {"error": str(e)})
            return
        if text is None:
            with state.lock:
                canned = state.canned
            # A caller that pins the answer is standing in for the agent
            # itself, the way the in-process recording's fake did; nothing
            # is written to the fixture set.
            if canned is not None:
                self._send_json(200, canned_completion(canned, body))
                return
            if not (state.record and state.upstream):
                note_missing(state.fixtures, key, body)
                self._send_json(404, {"error": f"no fixture for {key}"})
                return
            status, data = state.upstream.complete(body)
            if status != 200:
                # Only a good answer becomes a fixture; the failure is
                # passed through for the caller to see.
                self._send_raw(status, data, "application/json")
                return
            text = data.decode("utf-8")
            write_fixture(state.fixtures, messages, text)
        self._send_raw(200, text.encode("utf-8"), "application/json")


# ---- clients ----------------------------------------------------------------


def canned_completion(content: str, body: object) -> dict:
    """One chat-completions answer carrying `content`, in the shape the
    OpenAI-compatible clients parse."""
    model = body.get("model") if isinstance(body, dict) else None
    return {
        "id": "stub-canned",
        "object": "chat.completion",
        "model": model or "parity-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class StubControl:
    """HTTP client for the control endpoints of a running stub, in this
    process or another."""

    def __init__(self, origin: str):
        self.origin = origin.rstrip("/")

    def _call(self, method: str, path: str, payload: object = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.origin}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def hold(self) -> None:
        self._call("POST", "/_control/hold")

    def release(self) -> None:
        self._call("POST", "/_control/release")

    def held(self) -> dict:
        return self._call("GET", "/_control/held")

    def latency(self, ms: float) -> None:
        self._call("POST", "/_control/latency", {"ms": ms})

    def canned(self, content: str | None) -> None:
        self._call("POST", "/_control/canned", {"content": content})

    def reset(self) -> None:
        self._call("POST", "/_control/reset")

    def requests(self) -> dict:
        return self._call("GET", "/_control/requests")


class LLMStub:
    """An in-process stub on an ephemeral port. Knobs go through the
    control endpoints so the fixture and a standalone process behave
    the same."""

    def __init__(
        self,
        fixtures: Path = FIXTURES_DIR,
        *,
        record: bool = False,
        upstream: Upstream | None = None,
        hold_timeout_s: float = HOLD_TIMEOUT_S,
        host: str = "127.0.0.1",
        port: int = 0,
        verbose: bool = False,
    ):
        if record and upstream is None:
            upstream = Upstream.from_env()
        self._state = _State(
            fixtures,
            record=record,
            upstream=upstream,
            hold_timeout_s=hold_timeout_s,
            verbose=verbose,
        )
        self._server = LLMStubServer((host, port), self._state)
        self._thread: threading.Thread | None = None
        host, port = self._server.server_address[:2]
        self.origin = f"http://{host}:{port}"
        self.base_url = f"{self.origin}/v1"
        self.control = StubControl(self.origin)

    def start(self) -> LLMStub:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="prep-llm-stub", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._state.release()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> LLMStub:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def fixtures(self) -> Path:
        return self._state.fixtures

    @property
    def requests(self) -> list[str]:
        with self._state.lock:
            return list(self._state.requests)

    def hold(self) -> None:
        self.control.hold()

    def release(self) -> None:
        self.control.release()

    def held(self) -> dict:
        return self.control.held()

    def latency(self, ms: float) -> None:
        self.control.latency(ms)

    def reset(self) -> None:
        self.control.reset()


class RemoteStub:
    """A stub already running in another process, addressed by origin.

    A target outside this process was pointed at its stub when it was
    deployed, so a run against that target drives that stub instead of
    starting a second one nobody calls. Only the knobs travel; the
    fixture set is the remote process's.
    """

    def __init__(self, origin: str):
        self.origin = origin.rstrip("/")
        self.base_url = f"{self.origin}/v1"
        self.control = StubControl(self.origin)

    @property
    def requests(self) -> list[str]:
        return list(self.control.requests()["keys"])

    def hold(self) -> None:
        self.control.hold()

    def release(self) -> None:
        self.control.release()

    def held(self) -> dict:
        return self.control.held()

    def latency(self, ms: float) -> None:
        self.control.latency(ms)

    def reset(self) -> None:
        self.control.reset()


@pytest.fixture(scope="session")
def llm_stub():
    """The committed fixture set on an ephemeral port. `PARITY_LLM_RECORD=1`
    turns a miss into a recording against `PARITY_LLM_UPSTREAM_*`."""
    record = os.environ.get(RECORD_ENV) == "1"
    with LLMStub(FIXTURES_DIR, record=record) as stub:
        yield stub


# ---- standalone ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="canned chat-completions server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_DIR)
    parser.add_argument("--record", action="store_true", help=f"or {RECORD_ENV}=1")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    record = args.record or os.environ.get(RECORD_ENV) == "1"
    try:
        stub = LLMStub(
            args.fixtures, record=record, host=args.host, port=args.port, verbose=args.verbose
        )
    except RuntimeError as e:
        parser.error(str(e))
    print(f"llm stub: {stub.base_url} fixtures={args.fixtures} record={record}", flush=True)
    stub.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
