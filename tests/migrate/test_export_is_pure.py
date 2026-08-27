"""The exporter never writes to the snapshot (docs/PHASE-6.md A0)."""

from __future__ import annotations

import ast
import shutil
import sqlite3
from pathlib import Path

import pytest

import migrate
from migrate import export as export_mod
from migrate import snapshot as snap
from migrate.export import export
from migrate.snapshot import SnapshotError, open_snapshot

from .conftest import NOW

_MIGRATE = Path(migrate.__file__).resolve().parent
READ_PATH = (_MIGRATE / "export.py", _MIGRATE / "layout.py", _MIGRATE / "snapshot.py")


@pytest.fixture
def wal_snapshot(snapshot: Path, tmp_path: Path) -> Path:
    """The same snapshot in WAL journal mode, cleanly closed so no
    sidecar is on disk. A read-write open of one of these creates `-wal`
    and `-shm` immediately, which is what makes the sidecar sample below
    decisive; `VACUUM INTO` output is `delete` mode, where nothing is
    created and the sample would prove nothing."""
    copy = tmp_path / "wal.sqlite"
    shutil.copy(snapshot, copy)
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    conn.close()
    assert not snap.has_sidecars(copy)
    return copy


def test_export_leaves_the_snapshot_byte_identical(snapshot: Path, tmp_path: Path):
    before = (snap.sha256_file(snapshot), snapshot.stat().st_size, snapshot.stat().st_mtime_ns)

    export(snapshot, tmp_path / "out", now=NOW)

    after = (snap.sha256_file(snapshot), snapshot.stat().st_size, snapshot.stat().st_mtime_ns)
    assert before == after


def test_no_sidecar_appears_while_the_export_is_running(
    wal_snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Sampled mid-export, not after: SQLite deletes `-wal` and `-shm` on
    a clean close, so a check that runs once the export has returned
    cannot tell a read-write open from a read-only one."""
    samples: list[bool] = []
    original = export_mod._export_user

    def sampling(*args, **kwargs):
        samples.append(snap.has_sidecars(wal_snapshot))
        return original(*args, **kwargs)

    monkeypatch.setattr(export_mod, "_export_user", sampling)
    export(wal_snapshot, tmp_path / "out", now=NOW)

    assert samples and not any(samples)
    assert not snap.has_sidecars(wal_snapshot)


def test_export_opens_the_snapshot_read_only_and_immutable(
    snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The invariant at its source: every connection the export makes is
    a read-only immutable URI, and it opens nothing else."""
    opened: list[str] = []
    original = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append(str(target))
        return original(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    export(snapshot, tmp_path / "out", now=NOW)

    assert opened == [f"{snapshot.resolve().as_uri()}?mode=ro&immutable=1"]


def test_the_connection_refuses_writes(snapshot: Path):
    conn = open_snapshot(snapshot)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE users SET last_seen_at = 'x'")
    finally:
        conn.close()


def test_a_live_database_is_refused(tmp_path: Path):
    """A file with a `-wal` beside it is a copy of a live database, and an
    immutable read would skip its uncheckpointed writes."""
    live = tmp_path / "live.sqlite"
    live.write_bytes(b"")
    live.with_name(live.name + "-wal").write_bytes(b"")
    with pytest.raises(SnapshotError, match="-wal"):
        open_snapshot(live)


@pytest.mark.parametrize("module", READ_PATH, ids=lambda p: p.name)
def test_the_read_path_never_imports_the_app_schema_layer(module: Path):
    """`migrate.legacy_schema.init()` runs 27 migrations against whatever
    it is pointed at. Nothing on the export path may reach it."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert "migrate.legacy_schema" not in imported
