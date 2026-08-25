"""The clock seam: PREP_FAKE_NOW parsing, the provider lifecycle, and
the scan that keeps every direct wall-clock read out of prep/."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prep.infrastructure import clock

PREP_ROOT = Path(__file__).resolve().parent.parent.parent / "prep"
FAKE = "2026-03-14T15:00:00Z"
FAKE_DT = datetime(2026, 3, 14, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_clock(monkeypatch):
    monkeypatch.delenv(clock.ENV_FAKE_NOW, raising=False)
    clock.reset_clock()
    yield
    clock.reset_clock()


# ---- parsing --------------------------------------------------------------


def test_parse_z_suffix():
    assert clock.parse_fake_now(FAKE) == FAKE_DT


def test_parse_offset_normalizes_to_utc():
    parsed = clock.parse_fake_now("2026-03-14T11:00:00-04:00")
    assert parsed == FAKE_DT
    assert parsed.tzinfo == timezone.utc


def test_parse_naive_means_utc():
    parsed = clock.parse_fake_now("2026-03-14T15:00:00")
    assert parsed == FAKE_DT
    assert parsed.tzinfo == timezone.utc


def test_parse_malformed_names_the_variable():
    with pytest.raises(ValueError, match="PREP_FAKE_NOW"):
        clock.parse_fake_now("yesterday")


# ---- providers ------------------------------------------------------------


def test_system_clock_is_aware_utc():
    before = datetime.now(timezone.utc)
    got = clock.SystemClock().now()
    assert got.tzinfo == timezone.utc
    assert abs(got - before) < timedelta(seconds=5)


def test_unset_env_resolves_system_clock():
    assert isinstance(clock.get_clock(), clock.SystemClock)


def test_env_resolves_fixed_clock(monkeypatch):
    monkeypatch.setenv(clock.ENV_FAKE_NOW, FAKE)
    assert clock.now() == FAKE_DT
    assert clock.now_iso() == "2026-03-14T15:00:00+00:00"
    assert clock.now_iso(timespec="seconds") == "2026-03-14T15:00:00+00:00"
    assert clock.unix() == FAKE_DT.timestamp()


def test_fixed_clock_coerces_naive_to_utc():
    fixed = clock.FixedClock(datetime(2026, 3, 14, 15, 0))
    assert fixed.now() == FAKE_DT


def test_set_and_reset(monkeypatch):
    clock.set_clock(clock.FixedClock(FAKE_DT))
    assert clock.now() == FAKE_DT
    clock.reset_clock()
    assert isinstance(clock.get_clock(), clock.SystemClock)


def test_env_is_read_once_until_reset(monkeypatch):
    clock.get_clock()
    monkeypatch.setenv(clock.ENV_FAKE_NOW, FAKE)
    assert isinstance(clock.get_clock(), clock.SystemClock)
    clock.reset_clock()
    assert isinstance(clock.get_clock(), clock.FixedClock)


def test_fixed_clock_warns_at_resolution(monkeypatch):
    """The `prep` logger has propagate=False; attach below it."""
    monkeypatch.setenv(clock.ENV_FAKE_NOW, FAKE)
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    log = logging.getLogger("prep")
    handler = _Capture()
    log.addHandler(handler)
    try:
        clock.get_clock()
    finally:
        log.removeHandler(handler)
    assert any("PREP_FAKE_NOW" in m for m in messages), messages


# ---- the scan -------------------------------------------------------------

_DIRECT_READS = re.compile(r"datetime\.now\(|utcnow\(|time\.time\(|time\.strftime\(|date\.today\(")


def test_no_direct_clock_calls():
    """clock.py is the only file under prep/ that reads the wall clock."""
    offenders = sorted(
        str(p.relative_to(PREP_ROOT))
        for p in PREP_ROOT.rglob("*.py")
        if p.name != "clock.py" and _DIRECT_READS.search(p.read_text())
    )
    assert offenders == []
