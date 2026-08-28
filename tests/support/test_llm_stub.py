"""The canned LLM: key stability, byte- and header-stable replay, the
miss note, hold until release, latency, and recording against a fake
upstream."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.support.llm_stub import (
    DATE_HEADER,
    SERVER_HEADER,
    LLMStub,
    Upstream,
    fixture_path,
    llm_stub,  # noqa: F401  (session fixture)
    request_key,
    write_fixture,
)

PARITY_INSTANT_TOPIC = "Postgres MVCC"

_MESSAGES = [{"role": "user", "content": "hi"}]
# sha256 of '[{"content":"hi","role":"user"}]'
_MESSAGES_KEY = "4e79873118cd9be7a1f0308b9cd772950c5410c74ca3fe1ba2626cba009a9237"
_BODY = '{"choices": [{"message": {"content":  "canned"}}],\n "usage": {"prompt_tokens": 1}}'


def _post(stub: LLMStub, messages: list, **extra) -> httpx.Response:
    return httpx.post(
        f"{stub.base_url}/chat/completions",
        json={"model": "anything", "messages": messages, **extra},
        timeout=10,
    )


@pytest.fixture
def stub(tmp_path):
    with LLMStub(tmp_path / "llm") as s:
        yield s


# ---- key ----------------------------------------------------------------


def test_key_is_the_sha256_of_the_canonical_messages():
    assert request_key(_MESSAGES) == _MESSAGES_KEY


def test_key_ignores_field_order_and_is_ascii_agnostic():
    assert request_key([{"content": "hi", "role": "user"}]) == _MESSAGES_KEY
    accented = [{"role": "user", "content": "café"}]
    assert request_key(accented) == request_key(json.loads(json.dumps(accented)))
    assert request_key(accented) != request_key([{"role": "user", "content": "cafe"}])


def test_only_messages_shape_the_key(stub):
    write_fixture(stub.fixtures, _MESSAGES, _BODY)
    a = _post(stub, _MESSAGES, model="one", max_tokens=1, temperature=0.9)
    b = _post(stub, _MESSAGES, model="two", stream=False)
    assert (a.status_code, b.status_code) == (200, 200)
    assert stub.requests == [_MESSAGES_KEY, _MESSAGES_KEY]


# ---- replay ---------------------------------------------------------------


def test_replay_is_byte_and_header_stable(stub):
    write_fixture(stub.fixtures, _MESSAGES, _BODY)
    first = _post(stub, _MESSAGES)
    second = _post(stub, _MESSAGES)
    for resp in (first, second):
        assert resp.status_code == 200
        assert resp.content == _BODY.encode("utf-8")
        assert set(resp.headers.keys()) == {"server", "date", "content-type", "content-length"}
        assert resp.headers["server"] == SERVER_HEADER
        assert resp.headers["date"] == DATE_HEADER
        assert resp.headers["content-type"] == "application/json"
        assert resp.headers["content-length"] == str(len(_BODY.encode("utf-8")))
    assert dict(first.headers) == dict(second.headers)


def test_fixture_file_layout(stub):
    path = write_fixture(stub.fixtures, _MESSAGES, _BODY)
    assert path == fixture_path(stub.fixtures, _MESSAGES_KEY)
    assert path.name == f"{_MESSAGES_KEY[:16]}.json"
    data = json.loads(path.read_text())
    assert data == {"key": _MESSAGES_KEY, "messages": _MESSAGES, "body": _BODY}


def test_renamed_fixture_is_refused(stub):
    path = write_fixture(stub.fixtures, [{"role": "user", "content": "other"}], _BODY)
    path.rename(fixture_path(stub.fixtures, _MESSAGES_KEY))
    resp = _post(stub, _MESSAGES)
    assert resp.status_code == 500
    assert "holds key" in resp.json()["error"]


def test_malformed_requests_are_400(stub):
    bad = httpx.post(f"{stub.base_url}/chat/completions", content=b"not json", timeout=10)
    assert bad.status_code == 400
    no_messages = httpx.post(f"{stub.base_url}/chat/completions", json={"model": "x"}, timeout=10)
    assert no_messages.status_code == 400
    assert stub.requests == []


# ---- miss -----------------------------------------------------------------


def test_miss_is_404_and_notes_the_request(stub):
    resp = _post(stub, _MESSAGES, model="m")
    assert resp.status_code == 404
    assert resp.json() == {"error": f"no fixture for {_MESSAGES_KEY}"}
    note = stub.fixtures / "missing" / f"{_MESSAGES_KEY[:16]}.json"
    assert json.loads(note.read_text()) == {"model": "m", "messages": _MESSAGES}
    assert not fixture_path(stub.fixtures, _MESSAGES_KEY).exists()


# ---- hold ------------------------------------------------------------------


def _wait_until(pred, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not pred():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_hold_blocks_until_release(stub):
    write_fixture(stub.fixtures, _MESSAGES, _BODY)
    stub.hold()
    results: list[httpx.Response] = []
    worker = threading.Thread(target=lambda: results.append(_post(stub, _MESSAGES)))
    worker.start()
    _wait_until(lambda: stub.held()["count"] == 1)
    assert stub.held() == {"count": 1, "keys": [_MESSAGES_KEY]}
    assert worker.is_alive() and results == []

    stub.release()
    worker.join(timeout=5)
    assert results[0].status_code == 200
    assert results[0].content == _BODY.encode("utf-8")
    assert stub.held() == {"count": 0, "keys": []}
    # Release disarms: the next request flows.
    assert _post(stub, _MESSAGES).status_code == 200


def test_forgotten_hold_answers_503(tmp_path):
    with LLMStub(tmp_path / "llm", hold_timeout_s=0.05) as s:
        write_fixture(s.fixtures, _MESSAGES, _BODY)
        s.hold()
        resp = _post(s, _MESSAGES)
    assert resp.status_code == 503
    assert "timed out" in resp.json()["error"]


# ---- latency ---------------------------------------------------------------


def test_latency_delays_the_answer_and_reset_clears_it(stub):
    write_fixture(stub.fixtures, _MESSAGES, _BODY)
    stub.latency(200)
    started = time.monotonic()
    assert _post(stub, _MESSAGES).status_code == 200
    assert time.monotonic() - started >= 0.2

    stub.reset()
    started = time.monotonic()
    assert _post(stub, _MESSAGES).status_code == 200
    assert time.monotonic() - started < 0.2
    assert stub.requests == [_MESSAGES_KEY]


def test_reset_releases_a_hold(stub):
    write_fixture(stub.fixtures, _MESSAGES, _BODY)
    stub.hold()
    results: list[httpx.Response] = []
    worker = threading.Thread(target=lambda: results.append(_post(stub, _MESSAGES)))
    worker.start()
    _wait_until(lambda: stub.held()["count"] == 1)
    stub.reset()
    worker.join(timeout=5)
    assert results[0].status_code == 200
    assert stub.requests == []


# ---- record ----------------------------------------------------------------


class _FakeUpstream:
    """An in-process chat-completions endpoint that records what it saw."""

    def __init__(self, status: int = 200):
        self.status = status
        self.calls: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length))
                fake.calls.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                text = json.dumps(
                    {"choices": [{"message": {"content": f"upstream #{len(fake.calls)}"}}]}
                )
                if fake.status != 200:
                    text = json.dumps({"error": {"message": "upstream says no"}})
                data = text.encode("utf-8")
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address[:2]
        self.upstream = Upstream(f"http://{host}:{port}/v1", "upstream-key", "upstream-model")
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_record_forwards_a_miss_once_then_replays(tmp_path):
    with (
        _FakeUpstream() as fake,
        LLMStub(tmp_path / "llm", record=True, upstream=fake.upstream) as s,
    ):
        first = _post(s, _MESSAGES, model="caller-model", max_tokens=7)
        assert first.status_code == 200
        assert first.json()["choices"][0]["message"]["content"] == "upstream #1"

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["path"] == "/v1/chat/completions"
        assert call["authorization"] == "Bearer upstream-key"
        assert call["body"] == {"model": "upstream-model", "messages": _MESSAGES, "max_tokens": 7}

        stored = json.loads(fixture_path(s.fixtures, _MESSAGES_KEY).read_text())
        assert stored["key"] == _MESSAGES_KEY
        assert stored["messages"] == _MESSAGES
        assert stored["body"] == first.text

        second = _post(s, _MESSAGES)
        assert second.content == first.content
        assert len(fake.calls) == 1
        assert not (s.fixtures / "missing").exists()


def test_record_passes_an_upstream_failure_through_without_storing(tmp_path):
    with (
        _FakeUpstream(status=429) as fake,
        LLMStub(tmp_path / "llm", record=True, upstream=fake.upstream) as s,
    ):
        resp = _post(s, _MESSAGES)
        assert resp.status_code == 429
        assert resp.json()["error"]["message"] == "upstream says no"
        assert not fixture_path(s.fixtures, _MESSAGES_KEY).exists()


def test_record_without_upstream_config_is_refused(tmp_path, monkeypatch):
    for var in ("PARITY_LLM_UPSTREAM_BASE_URL", "PARITY_LLM_UPSTREAM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PARITY_LLM_UPSTREAM_MODEL", "m")
    with pytest.raises(RuntimeError, match="PARITY_LLM_UPSTREAM_BASE_URL"):
        LLMStub(tmp_path / "llm", record=True)


def test_ci_never_records(tmp_path, monkeypatch):
    # No PARITY_LLM_RECORD: a miss stays a miss even with upstream env set.
    monkeypatch.setenv("PARITY_LLM_UPSTREAM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("PARITY_LLM_UPSTREAM_API_KEY", "k")
    monkeypatch.setenv("PARITY_LLM_UPSTREAM_MODEL", "m")
    with LLMStub(tmp_path / "llm") as s:
        assert _post(s, _MESSAGES).status_code == 404
