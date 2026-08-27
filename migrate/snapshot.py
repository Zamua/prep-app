"""The one place a snapshot is opened.

A snapshot is a self-contained SQLite file produced from the live
database by `VACUUM INTO`, which folds the WAL in and writes nothing to
the source. Every migration tool reads it through `open_snapshot`, and
`open_snapshot` cannot write: `mode=ro&immutable=1` plus
`PRAGMA query_only = 1`.

`immutable=1` is also why the sidecar check below is not optional. It
tells SQLite the file cannot change, so any `-wal` beside it is ignored
rather than replayed, and the read would silently miss the newest
committed rows.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

_READ_CHUNK = 1 << 20


class SnapshotError(RuntimeError):
    """The file is not usable as a snapshot."""


def open_snapshot(path: Path | str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise SnapshotError(f"no snapshot at {resolved}")
    sidecars = [s for s in ("-wal", "-shm") if resolved.with_name(resolved.name + s).exists()]
    if sidecars:
        raise SnapshotError(
            f"{resolved.name} has {', '.join(sidecars)} beside it, so it is a live database, "
            "not a VACUUM INTO snapshot; an immutable read would skip its uncheckpointed writes"
        )
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def has_sidecars(path: Path | str) -> bool:
    """True when a `-wal` or `-shm` sits beside the file. A read-write
    open creates them even when no row changes, so their absence after a
    tool ran is an independent witness that it never opened one."""
    resolved = Path(path)
    return any(resolved.with_name(resolved.name + s).exists() for s in ("-wal", "-shm"))
