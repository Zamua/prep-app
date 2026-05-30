"""Unit tests for prep.web.durations.parse_until — preset chips +
custom (n, unit) → ISO-8601 UTC string. Pure function, no DB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prep.web.durations import FOREVER_ISO, DurationError, parse_until

_NOW = datetime(2026, 5, 30, 18, 0, 0, tzinfo=timezone.utc)


def _delta_hours(out_iso: str) -> float:
    out = datetime.fromisoformat(out_iso)
    return (out - _NOW).total_seconds() / 3600.0


def test_preset_1h_resolves_to_one_hour_later():
    assert _delta_hours(parse_until(preset="1h", custom=None, unit=None, now=_NOW)) == 1.0


def test_preset_1d_resolves_to_24h_later():
    assert _delta_hours(parse_until(preset="1d", custom=None, unit=None, now=_NOW)) == 24.0


def test_preset_1w_resolves_to_7d():
    assert _delta_hours(parse_until(preset="1w", custom=None, unit=None, now=_NOW)) == 24 * 7


def test_preset_forever_returns_sentinel():
    assert parse_until(preset="forever", custom=None, unit=None, now=_NOW) == FOREVER_ISO


def test_preset_tonight_returns_end_of_local_day():
    out = parse_until(preset="tonight", custom=None, unit=None, now=_NOW)
    # Exact time depends on the runner's timezone, but it MUST be later
    # than now and within ~24 hours.
    delta_h = _delta_hours(out)
    assert 0 < delta_h <= 24


def test_preset_tomorrow_returns_next_morning():
    out = parse_until(preset="tomorrow", custom=None, unit=None, now=_NOW)
    delta_h = _delta_hours(out)
    # Tomorrow at 8am local could be 6h..48h out depending on the
    # runner's tz + when in the day "now" is.
    assert 6 < delta_h < 48


def test_custom_hours_resolves():
    assert _delta_hours(parse_until(preset=None, custom="3", unit="hours", now=_NOW)) == 3.0


def test_custom_days_resolves():
    assert _delta_hours(parse_until(preset=None, custom="2", unit="days", now=_NOW)) == 48.0


def test_custom_weeks_resolves():
    assert _delta_hours(parse_until(preset=None, custom="1", unit="weeks", now=_NOW)) == 24 * 7


def test_preset_takes_precedence_over_custom_when_both_present():
    """Form posts may carry both — we always pick preset."""
    out = parse_until(preset="2h", custom="999", unit="hours", now=_NOW)
    assert _delta_hours(out) == 2.0


def test_unknown_preset_raises():
    with pytest.raises(DurationError):
        parse_until(preset="next-decade", custom=None, unit=None, now=_NOW)


def test_unknown_unit_raises():
    with pytest.raises(DurationError):
        parse_until(preset=None, custom="3", unit="fortnight", now=_NOW)


def test_custom_out_of_range_raises():
    with pytest.raises(DurationError):
        parse_until(preset=None, custom="0", unit="hours", now=_NOW)
    with pytest.raises(DurationError):
        parse_until(preset=None, custom="1000", unit="hours", now=_NOW)


def test_custom_non_integer_raises():
    with pytest.raises(DurationError):
        parse_until(preset=None, custom="three", unit="hours", now=_NOW)


def test_empty_form_raises():
    with pytest.raises(DurationError):
        parse_until(preset=None, custom=None, unit=None, now=_NOW)


def test_returned_string_is_iso_parseable():
    out = parse_until(preset="2h", custom=None, unit=None, now=_NOW)
    # Must round-trip back through datetime.fromisoformat
    assert datetime.fromisoformat(out) - _NOW == timedelta(hours=2)
