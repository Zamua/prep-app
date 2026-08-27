"""The global half of the export format (docs/PHASE-6.md A3)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from prep.migrate import layout
from prep.migrate.export import ExportError, export
from prep.migrate.snapshot import open_snapshot

from .conftest import NOW


def _directory_users(out: Path) -> list[dict]:
    return list(layout.iter_rows(layout.directory_path(out, "users")))


def test_users_split_into_the_directory_and_the_profiles(snapshot: Path, exported):
    out, manifest = exported
    rows = _directory_users(out)
    assert [list(r.keys()) for r in rows] == [list(layout.DIRECTORY_USER_COLUMNS) for _ in rows]
    assert [r["id"] for r in rows] == [u["id"] for u in manifest["users"]]
    assert [r["idx"] for r in rows] == [u["idx"] for u in manifest["users"]]

    conn = open_snapshot(snapshot)
    try:
        source = {r["tailscale_login"]: r for r in conn.execute("SELECT * FROM users")}
    finally:
        conn.close()
    for row in rows:
        origin = source[row["id"]]
        assert row["is_anonymous"] == origin["is_anonymous"]
        assert row["created_at"] == origin["created_at"]
        profile = layout.read_profile(out, row["id"])
        for cell, python in layout.PROFILE_FROM_USERS:
            assert profile[cell] == origin[python]


def test_account_merges_are_exported_verbatim_with_ids_preserved(snapshot: Path, exported):
    """It is the source of `previous_ids`: an offline device learns its
    old owner id from it, so a lost row orphans that device's queue."""
    out, manifest = exported
    rows = list(layout.iter_rows(layout.directory_path(out, "account_merges")))
    conn = open_snapshot(snapshot)
    try:
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(account_merges)")]
        expected = [
            dict(zip(columns, tuple(r), strict=True))
            for r in conn.execute("SELECT * FROM account_merges ORDER BY rowid")
        ]
    finally:
        conn.close()
    assert rows == expected
    assert manifest["globals"]["account_merges"] == len(expected)
    assert all(isinstance(r["id"], int) for r in rows)


def test_a_merge_still_started_is_counted_as_the_abort_criterion(exported, plan):
    """celld has no marker for a Python merge in flight, so one would
    never resume."""
    out, manifest = exported
    assert manifest["signals"]["account_merges_started"] == 1
    started = [
        (r["anon_user_id"], r["target_user_id"])
        for r in layout.iter_rows(layout.directory_path(out, "account_merges"))
        if r["status"] == "started"
    ]
    assert started == [plan.mid_merge]


def test_the_limiter_carries_only_the_trailing_window(snapshot: Path, exported):
    """A reset would hand every IP a fresh burst allowance at the moment
    of highest exposure."""
    out, manifest = exported
    cutoff = manifest["limiter"]["cutoff"]
    assert manifest["limiter"]["window_hours"] == 48
    assert cutoff == (NOW - timedelta(hours=48)).isoformat()

    rows = list(layout.iter_rows(layout.limiter_path(out)))
    assert rows
    assert all(r["created_at"] >= cutoff for r in rows)

    conn = open_snapshot(snapshot)
    try:
        kept = conn.execute(
            "SELECT COUNT(*) FROM instant_generations WHERE created_at >= ?", (cutoff,)
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM instant_generations").fetchone()[0]
    finally:
        conn.close()
    assert len(rows) == kept
    assert manifest["globals"]["instant_generations"] == kept
    assert manifest["signals"]["instant_generations_dropped"] == total - kept
    assert total > kept, "the fixture must have rows outside the window for the filter to show"


def test_the_window_is_a_parameter_not_a_constant(snapshot: Path, tmp_path: Path):
    out = tmp_path / "narrow"
    manifest = export(snapshot, out, now=NOW, limiter_window_hours=6)
    cutoff = manifest["limiter"]["cutoff"]
    assert cutoff == (NOW - timedelta(hours=6)).isoformat()
    assert all(r["created_at"] >= cutoff for r in layout.iter_rows(layout.limiter_path(out)))


def test_a_non_utc_timestamp_fails_the_window_rather_than_mis_filtering(
    snapshot: Path, tmp_path: Path
):
    """The filter is a string comparison, which is only an ordering while
    every row shares the `+00:00` form `db.now()` writes."""
    import shutil
    import sqlite3

    poisoned = tmp_path / "poisoned.sqlite"
    shutil.copy(snapshot, poisoned)
    conn = sqlite3.connect(poisoned)
    conn.execute(
        "UPDATE instant_generations SET created_at = ? WHERE id = (SELECT MIN(id)"
        " FROM instant_generations)",
        ("2026-08-26T13:00:00Z",),
    )
    conn.commit()
    conn.close()
    for suffix in ("-wal", "-shm"):
        poisoned.with_name(poisoned.name + suffix).unlink(missing_ok=True)

    with pytest.raises(ExportError, match="\\+00:00"):
        export(poisoned, tmp_path / "out", now=NOW)


def test_a_non_utc_now_is_normalised_before_it_reaches_the_cutoff(snapshot: Path, tmp_path: Path):
    """The cutoff is compared against `+00:00` row values as a string, so
    an offset that reached the manifest would order wrongly."""
    from datetime import timezone

    berlin = NOW.astimezone(timezone(timedelta(hours=2)))
    manifest = export(snapshot, tmp_path / "berlin", now=berlin)
    assert manifest["generated_at"] == NOW.isoformat()
    assert manifest["limiter"]["cutoff"].endswith("+00:00")
