"""The instant_generations limiter ledger.

Every window is exercised with injected timestamps (no sleeps). The
outcome-class rules are the load-bearing pins: pending and
failed_spent count toward every quota window, failed_free counts
toward the burst only, and an unresolved pending row keeps counting
(crashes fail closed).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from prep.infrastructure import db as infra_db
from prep.instant import repo

T0 = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
IP_A = "198.51.100.7"
IP_B = "203.0.113.9"
IP_C = "192.0.2.44"


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _reserve(ip: str, seconds: float) -> repo.Reservation | repo.Refusal:
    return repo.check_and_reserve(ip, topic_chars=10, at=_at(seconds))


def _rows() -> list[dict]:
    with infra_db.cursor() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT ip, created_at, outcome, cards FROM instant_generations ORDER BY id"
            ).fetchall()
        ]


# ---- burst window -----------------------------------------------------------


def test_burst_admits_then_refuses_with_retry_after(initialized_db):
    assert isinstance(_reserve(IP_A, 0), repo.Reservation)
    refusal = _reserve(IP_A, 30)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "minute"
    assert refusal.retry_after_s == 30
    assert isinstance(_reserve(IP_A, 61), repo.Reservation)


def test_burst_counts_failed_free_rows(initialized_db):
    gate = _reserve(IP_A, 0)
    repo.resolve(gate.id, "failed_free")
    refusal = _reserve(IP_A, 30)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "minute"


def test_pending_reservation_is_visible_to_the_next_check(initialized_db):
    # The reserved row IS what the next request's count sees: no
    # window exists between check and insert.
    assert isinstance(_reserve(IP_A, 0), repo.Reservation)
    refusal = _reserve(IP_A, 1)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "minute"


def test_concurrent_reservations_at_the_boundary_admit_exactly_one(initialized_db):
    # Check-and-reserve atomicity across two real connections: at
    # count N-1, two racing requests admit exactly one.
    barrier = threading.Barrier(2)
    results: list[repo.Reservation | repo.Refusal] = []

    def attempt():
        barrier.wait()
        results.append(repo.check_and_reserve(IP_A, topic_chars=10))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(type(r).__name__ for r in results) == ["Refusal", "Reservation"]
    assert len(_rows()) == 1


def test_burst_env_knobs(initialized_db, monkeypatch):
    monkeypatch.setenv("PREP_INSTANT_BURST_LIMIT", "2")
    monkeypatch.setenv("PREP_INSTANT_BURST_WINDOW_S", "30")
    assert isinstance(_reserve(IP_A, 0), repo.Reservation)
    assert isinstance(_reserve(IP_A, 5), repo.Reservation)
    refusal = _reserve(IP_A, 10)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "minute"
    # The tuned window ages rows out sooner than the default 60s.
    assert isinstance(_reserve(IP_A, 36), repo.Reservation)


def test_per_ip_isolation(initialized_db):
    assert isinstance(_reserve(IP_A, 0), repo.Reservation)
    refused = _reserve(IP_A, 10)
    assert isinstance(refused, repo.Refusal)
    # IP A at its burst limit never throttles IP B.
    assert isinstance(_reserve(IP_B, 10), repo.Reservation)


# ---- per-IP daily window ----------------------------------------------------


def test_daily_limit_counts_ok_and_failed_spent_and_pending(initialized_db):
    first = _reserve(IP_A, 0)
    repo.resolve(first.id, "ok", cards=12)
    second = _reserve(IP_A, 70)
    repo.resolve(second.id, "failed_spent")
    third = _reserve(IP_A, 140)
    assert isinstance(third, repo.Reservation)  # left pending

    refusal = _reserve(IP_A, 210)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "day"
    assert refusal.retry_after_s == 86400 - 210


def test_daily_limit_ignores_failed_free(initialized_db):
    for seconds in (0, 70, 140):
        gate = _reserve(IP_A, seconds)
        repo.resolve(gate.id, "failed_free")
    assert isinstance(_reserve(IP_A, 210), repo.Reservation)


def test_crashed_pending_row_keeps_counting(initialized_db, monkeypatch):
    # A request that died without resolving its row must not refund
    # the allowance: pending is spend until proven otherwise.
    monkeypatch.setenv("PREP_INSTANT_PER_IP_PER_DAY", "1")
    assert isinstance(_reserve(IP_A, 0), repo.Reservation)
    refusal = _reserve(IP_A, 70)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "day"


def test_daily_env_knob(initialized_db, monkeypatch):
    monkeypatch.setenv("PREP_INSTANT_PER_IP_PER_DAY", "5")
    for i in range(5):
        assert isinstance(_reserve(IP_A, i * 70), repo.Reservation)
    refusal = _reserve(IP_A, 5 * 70)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "day"


# ---- global per-minute cap ----------------------------------------------------


def test_global_per_minute_cap_refuses_busy(initialized_db, monkeypatch):
    monkeypatch.setenv("PREP_INSTANT_GLOBAL_PER_MINUTE", "2")
    assert isinstance(_reserve(IP_A, 0), repo.Reservation)
    assert isinstance(_reserve(IP_B, 5), repo.Reservation)
    refusal = _reserve(IP_C, 10)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "busy"
    assert refusal.retry_after_s is None


def test_global_per_minute_ignores_failed_free(initialized_db, monkeypatch):
    monkeypatch.setenv("PREP_INSTANT_GLOBAL_PER_MINUTE", "2")
    for ip, seconds in ((IP_A, 0), (IP_B, 5)):
        gate = _reserve(ip, seconds)
        repo.resolve(gate.id, "failed_free")
    assert isinstance(_reserve(IP_C, 10), repo.Reservation)


# ---- global daily breaker -----------------------------------------------------


def test_global_breaker_trips_at_the_cap(initialized_db, monkeypatch):
    monkeypatch.setenv("PREP_INSTANT_GLOBAL_PER_DAY", "2")
    a = _reserve(IP_A, 0)
    repo.resolve(a.id, "ok", cards=12)
    b = _reserve(IP_B, 70)
    repo.resolve(b.id, "ok", cards=12)
    refusal = _reserve(IP_C, 140)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "busy"


def test_breaker_counts_failed_spent_like_ok(initialized_db, monkeypatch):
    # Forced failures spend upstream tokens; a run of them must trip
    # the breaker exactly as successes would.
    monkeypatch.setenv("PREP_INSTANT_GLOBAL_PER_DAY", "2")
    for ip, seconds in ((IP_A, 0), (IP_B, 70)):
        gate = _reserve(ip, seconds)
        repo.resolve(gate.id, "failed_spent")
    refusal = _reserve(IP_C, 140)
    assert isinstance(refusal, repo.Refusal)
    assert refusal.kind == "busy"


def test_breaker_ignores_failed_free(initialized_db, monkeypatch):
    monkeypatch.setenv("PREP_INSTANT_GLOBAL_PER_DAY", "2")
    for ip, seconds in ((IP_A, 0), (IP_B, 70)):
        gate = _reserve(ip, seconds)
        repo.resolve(gate.id, "failed_free")
    assert isinstance(_reserve(IP_C, 140), repo.Reservation)


# ---- resolution ----------------------------------------------------------------


def test_resolve_updates_outcome_and_cards(initialized_db):
    gate = _reserve(IP_A, 0)
    repo.resolve(gate.id, "ok", cards=7)
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["cards"] == 7


def test_resolve_rejects_unknown_outcome(initialized_db):
    gate = _reserve(IP_A, 0)
    with pytest.raises(ValueError):
        repo.resolve(gate.id, "pending")
    with pytest.raises(ValueError):
        repo.resolve(gate.id, "exploded")


# ---- pruning --------------------------------------------------------------------


def test_rows_older_than_retention_are_pruned_on_reserve(initialized_db):
    old = repo.check_and_reserve(IP_A, topic_chars=10, at=T0 - timedelta(days=8))
    assert isinstance(old, repo.Reservation)
    assert isinstance(_reserve(IP_B, 0), repo.Reservation)
    rows = _rows()
    assert [r["ip"] for r in rows] == [IP_B]
