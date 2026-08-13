"""POST /api/instant/generate wire shapes.

Every branch is pinned to its `kind` / status / outcome-class triple,
including the forced-failure drill: a run of spend-classed failures
must trip the global breaker exactly as successes would.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import Request

from prep.agent.fake import FakeAgent
from prep.agent.port import AgentBusy, AgentResult
from prep.infrastructure import db as infra_db
from prep.instant.routes import MAX_BODY_BYTES, SENTINEL_BUCKET, _read_body

URL = "/api/instant/generate"
IP = {"x-real-ip": "198.51.100.7"}
REDIRECT_RE = re.compile(r"^/deck/[abcdefghijkmnpqrstuvwxyz23456789]{8}$")


def _deck_text(n: int = 5) -> str:
    return json.dumps(
        [{"q": f"Question {i}?", "a": f"answer {i}", "r": f"answer {i}"} for i in range(n)]
    )


def _fake(text: str | None = None) -> FakeAgent:
    return FakeAgent(next_response=AgentResult(text=text or _deck_text(), model="fake-model"))


def _rows() -> list[dict]:
    with infra_db.cursor() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT ip, outcome, cards, topic_chars, user_id"
                " FROM instant_generations ORDER BY id"
            ).fetchall()
        ]


def _seed_spend_rows(ip: str, n: int, *, minutes_old: int = 5, outcome: str = "failed_spent"):
    now = datetime.now(timezone.utc)
    with infra_db.cursor() as c:
        for i in range(n):
            c.execute(
                "INSERT INTO instant_generations (ip, created_at, outcome) VALUES (?, ?, ?)",
                (ip, (now - timedelta(minutes=minutes_old + i)).isoformat(), outcome),
            )


# ---- ok ---------------------------------------------------------------------


def test_ok_shape(client, instant_factory):
    fake = _fake()
    instant_factory(lambda **kw: fake)
    r = client.post(URL, json={"topic": "  Postgres   MVCC  "}, headers=IP)
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "ok"
    assert REDIRECT_RE.match(body["redirect"])
    assert "cards" not in body
    assert "Postgres   MVCC" in fake.calls[0]["prompt"]

    # The signed-in requester owns the stored deck; nothing was minted.
    with infra_db.cursor() as c:
        deck = dict(c.execute("SELECT * FROM decks").fetchone())
        questions = c.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
        cards = c.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
        anon = c.execute("SELECT COUNT(*) AS n FROM users WHERE is_anonymous = 1").fetchone()["n"]
    assert body["redirect"] == f"/deck/{deck['name']}"
    assert deck["user_id"] == "testuser@example.com"
    assert deck["display_name"] == "Postgres MVCC"
    assert (questions, cards, anon) == (5, 5, 0)

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["ip"] == "198.51.100.7"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["cards"] == 5
    assert rows[0]["topic_chars"] == len("Postgres   MVCC")
    assert rows[0]["user_id"] == "testuser@example.com"


# ---- invalid topic ------------------------------------------------------------


def test_invalid_topic_shapes(client, instant_factory):
    instant_factory(lambda **kw: _fake())
    cases = [
        {"json": {}},
        {"json": {"topic": ""}},
        {"json": {"topic": "   "}},
        {"json": {"topic": "x" * 501}},
        {"json": ["topic"]},
        {"content": b"not json"},
    ]
    for case in cases:
        r = client.post(URL, headers=IP | {"content-type": "application/json"}, **case)
        assert r.status_code == 422
        assert r.json()["kind"] == "invalid_topic"
        assert r.json()["message"]
    # Refused before admission: nothing was reserved or spent.
    assert _rows() == []


def test_oversized_body_is_refused_before_admission(client, instant_factory):
    # The topic itself is valid; only the body-size cap can refuse
    # this request.
    instant_factory(lambda **kw: _fake())
    r = client.post(URL, json={"topic": "t", "pad": "x" * (MAX_BODY_BYTES + 1)}, headers=IP)
    assert r.status_code == 422
    assert r.json()["kind"] == "invalid_topic"
    assert _rows() == []


def test_chunked_body_without_content_length_is_refused(client, instant_factory):
    # A chunked request declares no Content-Length; the cap cannot be
    # pre-checked, so the body is refused outright.
    instant_factory(lambda **kw: _fake())
    r = client.post(
        URL,
        content=iter([b'{"topic": "t"}']),
        headers=IP | {"content-type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["kind"] == "invalid_topic"
    assert _rows() == []


async def test_read_body_caps_a_stream_that_lies_about_its_length():
    chunk = b"x" * 8192
    messages = [{"type": "http.request", "body": chunk, "more_body": True} for _ in range(3)]
    messages.append({"type": "http.request", "body": b"", "more_body": False})
    it = iter(messages)

    async def receive():
        return next(it)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [(b"content-length", b"10")],
    }
    assert await _read_body(Request(scope, receive)) is None


# ---- not configured -------------------------------------------------------------


def test_not_configured_shape(client, instant_factory):
    instant_factory(lambda **kw: None)
    r = client.post(URL, json={"topic": "anything"}, headers=IP)
    assert r.status_code == 503
    assert r.json()["kind"] == "not_configured"
    assert _rows() == []


# ---- rate limited ----------------------------------------------------------------


def test_burst_limit_shape(client, instant_factory):
    instant_factory(lambda **kw: _fake())
    assert client.post(URL, json={"topic": "t"}, headers=IP).status_code == 200
    r = client.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 429
    body = r.json()
    assert body["kind"] == "rate_limited"
    assert body["scope"] == "minute"
    assert 1 <= body["retry_after_s"] <= 60
    assert r.headers["retry-after"] == str(body["retry_after_s"])


def test_day_limit_shape(client, instant_factory):
    instant_factory(lambda **kw: _fake())
    _seed_spend_rows("198.51.100.7", 3)
    r = client.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 429
    body = r.json()
    assert body["kind"] == "rate_limited"
    assert body["scope"] == "day"
    assert body["message"] == "You've reached today's limit. Create a free account to keep going."
    assert 0 < body["retry_after_s"] <= 86400
    assert r.headers["retry-after"] == str(body["retry_after_s"])
    # The failed_spent seeds alone consumed the allowance: no new row.
    assert len(_rows()) == 3


# ---- busy -------------------------------------------------------------------------


def test_global_cap_busy_shape(client, instant_factory, monkeypatch):
    monkeypatch.setenv("PREP_INSTANT_GLOBAL_PER_MINUTE", "0")
    instant_factory(lambda **kw: _fake())
    r = client.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 429
    body = r.json()
    assert body["kind"] == "busy"
    assert "scope" not in body
    assert _rows() == []


def test_upstream_busy_resolves_failed_free(client, instant_factory):
    class _BusyAgent:
        async def run(self, prompt, *, model=None, reasoning=None, timeout_s=120.0):
            raise AgentBusy("free tier saturated")

    instant_factory(lambda **kw: _BusyAgent())
    r = client.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 429
    assert r.json()["kind"] == "busy"
    assert [row["outcome"] for row in _rows()] == ["failed_free"]


# ---- generation failed ---------------------------------------------------------------


def test_unparseable_output_resolves_failed_spent(client, instant_factory):
    instant_factory(lambda **kw: _fake("reply with a poem, no JSON"))
    r = client.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 502
    body = r.json()
    assert body["kind"] == "generation_failed"
    assert body["message"] == "That didn't work. Try again."
    assert [row["outcome"] for row in _rows()] == ["failed_spent"]


def test_unexpected_crash_never_500s(client, instant_factory):
    class _CrashAgent:
        async def run(self, prompt, *, model=None, reasoning=None, timeout_s=120.0):
            raise RuntimeError("wire tripped")

    instant_factory(lambda **kw: _CrashAgent())
    r = client.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 502
    assert r.json()["kind"] == "generation_failed"
    assert [row["outcome"] for row in _rows()] == ["failed_spent"]


# ---- ledger failures stay inside the kind contract ------------------------------------


def test_ledger_failure_on_admission_refuses_busy(client, instant_factory, monkeypatch):
    instant_factory(lambda **kw: _fake())

    def locked(ip, *, topic_chars, at=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("prep.instant.repo.check_and_reserve", locked)
    r = client.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 429
    assert r.json()["kind"] == "busy"


def test_resolve_failure_still_returns_the_deck(client, instant_factory, monkeypatch):
    # The visitor's tokens were spent; a ledger hiccup must not throw
    # the deck away. The row stays pending, which counts as spend.
    instant_factory(lambda **kw: _fake())

    def locked(reservation_id, outcome, *, cards=None, user_id=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("prep.instant.repo.resolve", locked)
    r = client.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 200
    assert REDIRECT_RE.match(r.json()["redirect"])
    assert [row["outcome"] for row in _rows()] == ["pending"]


def test_resolve_failure_keeps_the_mapped_error_shape(client, instant_factory, monkeypatch):
    instant_factory(lambda **kw: _fake("a poem, not JSON"))

    def locked(reservation_id, outcome, *, cards=None, user_id=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("prep.instant.repo.resolve", locked)
    r = client.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 502
    assert r.json()["kind"] == "generation_failed"
    assert [row["outcome"] for row in _rows()] == ["pending"]


# ---- ip bucketing at the wire ----------------------------------------------------------


def test_headerless_requests_share_the_sentinel_bucket(client, instant_factory):
    instant_factory(lambda **kw: _fake())
    first = client.post(URL, json={"topic": "t"})
    assert first.status_code == 200
    assert _rows()[0]["ip"] == SENTINEL_BUCKET
    # A forged XFF does not escape the bucket: same windows apply.
    second = client.post(URL, json={"topic": "t"}, headers={"x-forwarded-for": "9.9.9.9"})
    assert second.status_code == 429
    assert second.json()["scope"] == "minute"


def test_ipv6_requests_are_keyed_to_the_slash64_bucket(client, instant_factory):
    instant_factory(lambda **kw: _fake())
    first = client.post(URL, json={"topic": "t"}, headers={"x-real-ip": "2001:db8:aa:bb::1"})
    assert first.status_code == 200
    assert _rows()[0]["ip"] == "2001:db8:aa:bb::/64"
    # A sibling address in the same /64 shares the burst window.
    second = client.post(URL, json={"topic": "t"}, headers={"x-real-ip": "2001:db8:aa:bb:ffff::9"})
    assert second.status_code == 429
    assert second.json()["scope"] == "minute"


# ---- forced-failure drill ---------------------------------------------------------------


def test_failed_spent_run_trips_the_global_breaker(client, instant_factory, monkeypatch):
    # Finding-shaped abuse: every call fails on purpose (the topic is
    # attacker-controlled prompt text), spends upstream tokens, and
    # must burn down the breaker exactly as successes would.
    monkeypatch.setenv("PREP_INSTANT_GLOBAL_PER_DAY", "3")
    monkeypatch.setenv("PREP_INSTANT_GLOBAL_PER_MINUTE", "100")
    monkeypatch.setenv("PREP_INSTANT_PER_IP_PER_DAY", "10")
    instant_factory(lambda **kw: _fake("a poem, not JSON"))

    for i in range(3):
        r = client.post(URL, json={"topic": "t"}, headers={"x-real-ip": f"203.0.113.{i + 1}"})
        assert r.status_code == 502
        assert r.json()["kind"] == "generation_failed"

    r = client.post(URL, json={"topic": "t"}, headers={"x-real-ip": "203.0.113.99"})
    assert r.status_code == 429
    assert r.json()["kind"] == "busy"
    assert [row["outcome"] for row in _rows()] == ["failed_spent"] * 3


def test_a_slow_upstream_reads_as_busy_but_still_counts_as_spend(
    client, initialized_db: str, instant_factory, monkeypatch
):
    """A deadline hit on a shared free tier is congestion, not a broken
    generator: the visitor is told it is busy, while the ledger still
    records the spend so the quota cannot be drained by slow calls."""

    import asyncio

    class _Stalls:
        async def run(self, prompt, **kw):
            await asyncio.sleep(5)

    monkeypatch.setenv("PREP_INSTANT_TIMEOUT_S", "0.05")
    instant_factory(lambda **kw: _Stalls())
    r = client.post("/api/instant/generate", json={"topic": "quorum"}, headers=IP)
    assert r.status_code == 429
    assert r.json()["kind"] == "busy"
    assert [row["outcome"] for row in _rows()] == ["failed_spent"]
