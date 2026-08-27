"""The per-user half of the export format."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from migrate import layout
from migrate.export import DROPPED_BYOK_PROVIDER, _columns, _select, export
from migrate.snapshot import open_snapshot

from .conftest import NOW


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="ascii").split("\n") if line]


def test_the_directory_holds_one_file_per_user_per_table(exported):
    out, manifest = exported
    for user in manifest["users"]:
        directory = layout.user_dir(out, user["id"])
        assert directory.name == user["dir"]
        assert layout.profile_path(out, user["id"]).is_file()
        assert sorted(p.name for p in directory.glob("*.ndjson")) == sorted(
            f"{t}.ndjson" for t in layout.DATA_TABLES
        )


def test_a_user_directory_name_decodes_back_to_the_id(exported):
    out, manifest = exported
    for user in manifest["users"]:
        assert layout.user_id_from_dir(user["dir"]) == user["id"]
        assert "/" not in user["dir"] and "=" not in user["dir"]


def test_idx_ranks_by_created_at_then_login_and_starts_at_one(snapshot: Path, exported):
    _, manifest = exported
    conn = open_snapshot(snapshot)
    try:
        expected = [
            r[0]
            for r in conn.execute(
                "SELECT tailscale_login FROM users ORDER BY created_at, tailscale_login"
            )
        ]
    finally:
        conn.close()
    assert [u["id"] for u in manifest["users"]] == expected
    assert [u["idx"] for u in manifest["users"]] == list(range(1, len(expected) + 1))


def test_a_re_export_is_byte_identical(snapshot: Path, exported, tmp_path: Path):
    """Idempotence of the export is what lets the operator re-run after a
    partial import without re-deriving anything."""
    out, _ = exported
    again = tmp_path / "again"
    export(snapshot, again, now=NOW)
    first = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    second = {p.relative_to(again): p.read_bytes() for p in again.rglob("*") if p.is_file()}
    assert first == second


def test_a_user_dropped_between_two_exports_is_pruned(snapshot: Path, tmp_path: Path):
    out = tmp_path / "out"
    export(snapshot, out, now=NOW)
    stale = layout.user_dir(out, "user_gone@example.test")
    stale.mkdir(parents=True)
    (stale / "decks.ndjson").write_text("{}\n", encoding="ascii")

    export(snapshot, out, now=NOW)
    assert not stale.exists()


def test_no_exported_row_carries_an_owner_column(exported):
    out, manifest = exported
    for user in manifest["users"]:
        for table in layout.DATA_TABLES:
            for row in layout.iter_rows(layout.table_path(out, user["id"], table)):
                assert not (layout.USER_COLUMNS & row.keys())


def test_column_order_is_the_python_schema_minus_the_owner(snapshot: Path, exported):
    out, manifest = exported
    conn = open_snapshot(snapshot)
    try:
        for table in layout.DATA_TABLES:
            expected = [
                r["name"]
                for r in conn.execute(f'PRAGMA table_info("{table}")')
                if r["name"] not in layout.USER_COLUMNS
            ]
            for user in manifest["users"]:
                for line in _lines(layout.table_path(out, user["id"], table)):
                    assert list(json.loads(line).keys()) == expected
    finally:
        conn.close()


def test_rows_are_in_rowid_order_and_nothing_is_lost(snapshot: Path, exported):
    """Per user, per table: the export equals the snapshot's own scoped
    read. The counts in the manifest are the verifier's expectation, so
    they have to be the counts on disk."""
    out, manifest = exported
    conn = open_snapshot(snapshot)
    try:
        for user in manifest["users"]:
            for table in layout.DATA_TABLES:
                columns = [c for c in _columns(conn, table) if c not in layout.USER_COLUMNS]
                expected = [
                    dict(zip(columns, tuple(r), strict=True))
                    for r in conn.execute(_select(table, columns), (user["id"],))
                ]
                actual = list(layout.iter_rows(layout.table_path(out, user["id"], table)))
                assert actual == expected
                assert user["counts"][table] == len(expected)
    finally:
        conn.close()


def test_every_user_scoped_row_lands_with_exactly_one_user(snapshot: Path, exported):
    _, manifest = exported
    totals = {t: sum(u["counts"][t] for u in manifest["users"]) for t in layout.DATA_TABLES}
    conn = open_snapshot(snapshot)
    try:
        for table, exported_rows in totals.items():
            in_snapshot = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if table == "grading_idempotency":
                # A row whose question is gone names no user; there is no FK
                # to have cascaded it away.
                in_snapshot -= manifest["signals"]["grading_idempotency_orphans"]
            assert exported_rows == in_snapshot, table
    finally:
        conn.close()


CELL_UNIQUE_KEYS = {
    "decks": ("name",),
    "questions": ("id",),
    "cards": ("question_id",),
    "reviews": ("id",),
    "study_sessions": ("id",),
    "study_session_answers": ("session_id", "question_id"),
    "trivia_sessions": ("id",),
    "trivia_queue": ("question_id",),
    "offline_sync_idempotency": ("client_id",),
    "grading_idempotency": ("idempotency_key",),
    "push_subscriptions": ("endpoint",),
    "byok_credentials": ("provider",),
    "api_tokens": ("token_hash",),
    "notifications_log": ("id",),
}


@pytest.mark.parametrize("table,key", sorted(CELL_UNIQUE_KEYS.items()))
def test_one_users_rows_satisfy_the_cells_unique_keys(exported, table: str, key: tuple[str, ...]):
    """The cell drops the owner column, so Python's `UNIQUE (user_id,
    name)` becomes `UNIQUE (name)`. A collision would not raise on
    import: `INSERT OR IGNORE` would drop the row in silence."""
    out, manifest = exported
    for user in manifest["users"]:
        keys = [
            tuple(row[c] for c in key)
            for row in layout.iter_rows(layout.table_path(out, user["id"], table))
        ]
        assert len(keys) == len(set(keys)), (user["id"], table)


def test_active_workflows_is_reset_not_exported(exported, plan):
    out, manifest = exported
    assert "active_workflows" not in layout.DATA_TABLES
    assert manifest["reset_tables"] == ["active_workflows"]
    assert manifest["signals"]["active_workflows_reset"] > 0
    for user in manifest["users"]:
        assert not (layout.user_dir(out, user["id"]) / "active_workflows.ndjson").exists()


def test_a_session_left_grading_is_copied_and_counted(exported, plan):
    """`current_grading_workflow_id` copies as-is; the session resolves to
    `gone` and the study loop recovers, as it does for a terminated
    execution today."""
    out, manifest = exported
    assert manifest["signals"]["study_sessions_grading"] == 1
    rows = list(layout.iter_rows(layout.table_path(out, plan.grading_user, "study_sessions")))
    grading = [r for r in rows if r["state"] == "grading"]
    assert len(grading) == 1
    assert grading[0]["current_grading_workflow_id"]


def test_the_subscription_credential_is_exported_and_counted(exported, plan):
    """The export stays a faithful copy of the snapshot; dropping the row
    is the importer's policy (decision 7.4)."""
    out, manifest = exported
    providers = [
        r["provider"]
        for r in layout.iter_rows(
            layout.table_path(out, plan.subscription_user, "byok_credentials")
        )
    ]
    assert DROPPED_BYOK_PROVIDER in providers
    assert manifest["signals"]["byok_claude_subscription"] == 1


def test_api_tokens_are_copied_verbatim(exported, plan):
    out, _ = exported
    rows = list(layout.iter_rows(layout.table_path(out, plan.pat_user, "api_tokens")))
    assert len(rows) == 1
    assert rows[0]["token_hash"] and rows[0]["key_prefix"]


def test_push_subscriptions_survive_with_their_keys(exported, plan):
    """RFC 8291 encryption uses the subscription's own p256dh/auth; the
    migration does not touch them."""
    out, _ = exported
    rows = list(layout.iter_rows(layout.table_path(out, plan.push_user, "push_subscriptions")))
    assert len(rows) == 2
    assert all(r["p256dh"] and r["auth"] and r["endpoint"] for r in rows)


def test_a_user_with_no_rows_still_gets_a_profile_and_empty_tables(exported, plan):
    out, manifest = exported
    entry = next(u for u in manifest["users"] if u["id"] == plan.empty_anonymous)
    assert set(entry["counts"].values()) == {0}
    for table in layout.DATA_TABLES:
        assert layout.table_path(out, entry["id"], table).read_bytes() == b""
    profile = layout.read_profile(out, entry["id"])
    assert profile["is_anonymous"] == 1


def test_the_profile_carries_last_seen_at_verbatim(snapshot: Path, exported):
    """It is the anonymous reaper's only input: resetting it to now would
    spare every idle account, and to the epoch would delete them all."""
    out, manifest = exported
    conn = open_snapshot(snapshot)
    try:
        source = {
            r["tailscale_login"]: r["last_seen_at"]
            for r in conn.execute("SELECT tailscale_login, last_seen_at FROM users")
        }
    finally:
        conn.close()
    for user in manifest["users"]:
        assert layout.read_profile(out, user["id"])["last_seen_at"] == source[user["id"]]


def test_the_profile_keys_are_the_cell_column_order(exported):
    out, manifest = exported
    expected = [cell for cell, _ in layout.PROFILE_FROM_USERS]
    for user in manifest["users"]:
        assert list(layout.read_profile(out, user["id"]).keys()) == expected


def test_text_is_written_as_ascii_and_decodes_back_unchanged(exported, plan):
    """A prompt with a non-ASCII character, a quote, a newline and a tab.
    The file stays pure ASCII so a lone surrogate could not break the
    encode, and JSON decoding restores the exact string."""
    from migrate.synth import AWKWARD_TEXT

    out, _ = exported
    path = layout.table_path(out, plan.heavy, "questions")
    path.read_text(encoding="ascii")  # raises if a byte escaped ASCII
    assert any(r["prompt"] == AWKWARD_TEXT for r in layout.iter_rows(path))


def test_a_blob_fails_the_export_rather_than_being_coerced(snapshot: Path, tmp_path: Path):
    import shutil
    import sqlite3

    poisoned = tmp_path / "poisoned.sqlite"
    shutil.copy(snapshot, poisoned)
    conn = sqlite3.connect(poisoned)
    conn.execute(
        "UPDATE questions SET rubric = ? WHERE id = (SELECT MIN(id) FROM questions)",
        (sqlite3.Binary(b"\x00\x01"),),
    )
    conn.commit()
    conn.close()
    for suffix in ("-wal", "-shm"):
        poisoned.with_name(poisoned.name + suffix).unlink(missing_ok=True)

    from migrate.export import ExportError

    with pytest.raises(ExportError, match="BLOB"):
        export(poisoned, tmp_path / "out", now=NOW)


def test_the_manifest_fingerprints_the_schema_it_read(snapshot: Path, exported):
    out, manifest = exported
    assert manifest["tool_version"] == layout.TOOL_VERSION
    assert set(manifest["schema"]) == set(layout.PYTHON_TABLES)
    assert len(manifest["schema_fingerprint"]) == 64
    assert manifest["snapshot"]["bytes"] == snapshot.stat().st_size
    assert "path" not in manifest["snapshot"]
    assert manifest["empty_tables"] == list(layout.EMPTY_TABLES)


def test_the_manifest_is_written_last(snapshot: Path, tmp_path: Path, monkeypatch):
    """A crashed export must not leave a manifest claiming the directory
    is complete."""
    out = tmp_path / "out"
    export(snapshot, out, now=NOW)
    assert layout.manifest_path(out).is_file()

    import migrate.export as export_mod

    def boom(*args, **kwargs):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(export_mod, "_export_user", boom)
    with pytest.raises(RuntimeError, match="interrupted"):
        export(snapshot, out, now=NOW)
    assert not layout.manifest_path(out).exists()
