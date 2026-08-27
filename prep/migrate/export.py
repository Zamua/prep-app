"""The exporter: a pure read of a snapshot into a diffable directory.

    python -m prep.migrate.export --snapshot <path> --out <dir>

Nothing is written outside `--out`, and the snapshot is opened read-only
and immutable (`prep.migrate.snapshot`). This module never imports
`prep.infrastructure.db`, whose `init()` would run 27 migrations against
the file it is supposed to be reading; `tests/migrate/test_export_is_pure.py`
pins that as source, not as convention.

Layout, orderings and table dispositions: `prep.migrate.layout` and
docs/PHASE-6.md A.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

from prep.infrastructure import clock
from prep.migrate import layout
from prep.migrate.snapshot import open_snapshot, sha256_file


class ExportError(RuntimeError):
    """The snapshot holds something the export format cannot carry, or
    something the cell schema has no home for. Loud beats coerced."""


# The FROM/WHERE that scopes one table to one user, the row aliased `t`.
# Tables with no owner column reach the user through their parent.
# `grading_idempotency` is the one join with no declared foreign key, so a
# row whose question is gone belongs to nobody; those are counted as
# orphans rather than attributed to a guess.
_SCOPE: dict[str, str] = {
    "decks": "FROM decks t WHERE t.user_id = ?",
    "questions": "FROM questions t WHERE t.user_id = ?",
    "cards": "FROM cards t JOIN questions q ON q.id = t.question_id WHERE q.user_id = ?",
    "reviews": "FROM reviews t JOIN questions q ON q.id = t.question_id WHERE q.user_id = ?",
    "grading_idempotency": (
        "FROM grading_idempotency t JOIN questions q ON q.id = t.question_id WHERE q.user_id = ?"
    ),
    "offline_sync_idempotency": "FROM offline_sync_idempotency t WHERE t.user_id = ?",
    "study_sessions": "FROM study_sessions t WHERE t.user_id = ?",
    "study_session_answers": (
        "FROM study_session_answers t JOIN study_sessions s ON s.id = t.session_id"
        " WHERE s.user_id = ?"
    ),
    "trivia_sessions": "FROM trivia_sessions t WHERE t.user_id = ?",
    "trivia_queue": (
        "FROM trivia_queue t JOIN questions q ON q.id = t.question_id WHERE q.user_id = ?"
    ),
    "notifications_log": "FROM notifications_log t WHERE t.user_id = ?",
    "push_subscriptions": "FROM push_subscriptions t WHERE t.user_id = ?",
    "byok_credentials": "FROM byok_credentials t WHERE t.user_id = ?",
    "api_tokens": "FROM api_tokens t WHERE t.user_id = ?",
}

DROPPED_BYOK_PROVIDER = "claude-subscription"


@dataclass(frozen=True)
class UserPlan:
    """One user's place in the export. `idx` starts at 1: block 0 is the
    parity seed's, and a re-export of the same snapshot assigns the same
    number, which is what makes the import converge on a re-run."""

    id: str
    idx: int
    is_anonymous: int
    created_at: str


# ---- reading --------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not info:
        raise ExportError(f"snapshot has no table {table}")
    return [r["name"] for r in info]


def schema_fingerprint(conn: sqlite3.Connection) -> tuple[dict, str]:
    """The column names per table, and a sha256 over the full
    `PRAGMA table_info` (types, nullability, defaults, primary keys) of
    every Python table. The manifest carries the names because they are
    what a reader wants; the digest is what catches a type change."""
    names: dict[str, list[str]] = {}
    detail: dict[str, list] = {}
    for table in sorted(layout.PYTHON_TABLES):
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not info:
            raise ExportError(f"snapshot has no table {table}")
        names[table] = [r["name"] for r in info]
        detail[table] = [
            [r["name"], r["type"], int(r["notnull"]), r["dflt_value"], int(r["pk"])] for r in info
        ]
    canonical = json.dumps(detail, sort_keys=True, separators=(",", ":"))
    return names, sha256(canonical.encode("utf-8")).hexdigest()


def plan_users(conn: sqlite3.Connection) -> list[UserPlan]:
    rows = conn.execute(
        "SELECT tailscale_login, is_anonymous, created_at FROM users"
        " ORDER BY created_at, tailscale_login"
    ).fetchall()
    return [
        UserPlan(
            id=r["tailscale_login"],
            idx=n,
            is_anonymous=int(r["is_anonymous"] or 0),
            created_at=r["created_at"],
        )
        for n, r in enumerate(rows, start=1)
    ]


# ---- writing --------------------------------------------------------------


def _encode(table: str, columns: Sequence[str], row: Sequence) -> str:
    obj = {}
    for name, value in zip(columns, row, strict=True):
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise ExportError(
                f"{table}.{name} holds a BLOB; the Python schema declares none, "
                "so a new one must be given a disposition rather than coerced"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ExportError(f"{table}.{name} holds {value!r}, which JSON cannot carry")
        obj[name] = value
    # ensure_ascii keeps every file pure ASCII, so a lone surrogate in a TEXT
    # column survives as an escape instead of failing the utf-8 encode.
    return json.dumps(obj, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _write_ndjson(path: Path, table: str, columns: Sequence[str], rows: Iterable[Sequence]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="ascii", newline="\n") as fh:
        for row in rows:
            fh.write(_encode(table, columns, row))
            fh.write("\n")
            written += 1
    return written


def _stream(conn: sqlite3.Connection, sql: str, params: tuple) -> Iterator[tuple]:
    cur = conn.execute(sql, params)
    while batch := cur.fetchmany(1000):
        yield from (tuple(r) for r in batch)


def _quoted(columns: Sequence[str], alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f'{prefix}"{c}"' for c in columns)


def _select(table: str, columns: Sequence[str]) -> str:
    return f"SELECT {_quoted(columns, 't')} {_SCOPE[table]} ORDER BY t.rowid"


def _prune_stale_users(out: Path, keep: set[str]) -> None:
    """A user dropped between two exports must not linger and get
    replayed; the directory is the export, not an accumulation."""
    root = layout.users_root(out)
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.is_dir() and child.name not in keep:
            shutil.rmtree(child)


# ---- the export -----------------------------------------------------------


def export(
    snapshot: Path | str,
    out: Path | str,
    *,
    now: datetime | None = None,
    limiter_window_hours: int = layout.LIMITER_WINDOW_HOURS,
) -> dict:
    """Writes the export directory and returns the manifest."""
    snapshot = Path(snapshot)
    out = Path(out)
    digest = sha256_file(snapshot)
    size = snapshot.stat().st_size
    generated = layout.to_utc(now) if now else clock.now()
    generated_at = generated.isoformat()
    cutoff = (generated - timedelta(hours=limiter_window_hours)).isoformat()

    out.mkdir(parents=True, exist_ok=True)
    # Removed first: a crashed export must not leave a manifest that claims
    # the directory is complete.
    layout.manifest_path(out).unlink(missing_ok=True)

    conn = open_snapshot(snapshot)
    try:
        schema, fingerprint = schema_fingerprint(conn)
        plans = plan_users(conn)
        signals = _signals(conn, cutoff)
        globals_counts = _export_globals(conn, out, plans, cutoff)
        users = [_export_user(conn, out, plan) for plan in plans]
    finally:
        conn.close()

    _prune_stale_users(out, {layout.user_dir_name(p.id) for p in plans})

    manifest = {
        "tool_version": layout.TOOL_VERSION,
        "generated_at": generated_at,
        # The path is deliberately absent: the sha256 is the identity, and a
        # manifest gets pasted into places a production path should not go.
        "snapshot": {"sha256": digest, "bytes": size},
        "schema_fingerprint": fingerprint,
        "schema": schema,
        "data_tables": list(layout.DATA_TABLES),
        "reset_tables": list(layout.RESET_TABLES),
        "empty_tables": list(layout.EMPTY_TABLES),
        "limiter": {"window_hours": limiter_window_hours, "cutoff": cutoff},
        "globals": globals_counts,
        "signals": signals,
        "users": users,
    }
    with layout.manifest_path(out).open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=False, ensure_ascii=True)
        fh.write("\n")
    return manifest


def _signals(conn: sqlite3.Connection, cutoff: str) -> dict:
    """The counts the runbook's abort criteria read, and the three
    dispositions that are not a straight copy."""

    def one(sql: str, params: tuple = ()) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return {
        "account_merges_started": one(
            "SELECT COUNT(*) FROM account_merges WHERE status = 'started'"
        ),
        "study_sessions_grading": one(
            "SELECT COUNT(*) FROM study_sessions WHERE state = 'grading'"
        ),
        "byok_claude_subscription": one(
            "SELECT COUNT(*) FROM byok_credentials WHERE provider = ?", (DROPPED_BYOK_PROVIDER,)
        ),
        "active_workflows_reset": one("SELECT COUNT(*) FROM active_workflows"),
        "grading_idempotency_orphans": one(
            "SELECT COUNT(*) FROM grading_idempotency t"
            " LEFT JOIN questions q ON q.id = t.question_id WHERE q.id IS NULL"
        ),
        "instant_generations_total": one("SELECT COUNT(*) FROM instant_generations"),
        "instant_generations_dropped": one(
            "SELECT COUNT(*) FROM instant_generations WHERE created_at < ?", (cutoff,)
        ),
    }


def _assert_utc_offsets(conn: sqlite3.Connection) -> None:
    """The limiter filter is a string comparison against an ISO cutoff,
    which is only an ordering if every row shares the `+00:00` form
    `db.now()` writes. A different suffix would silently mis-window."""
    odd = int(
        conn.execute(
            "SELECT COUNT(*) FROM instant_generations WHERE created_at NOT LIKE '%+00:00'"
        ).fetchone()[0]
    )
    if odd:
        raise ExportError(
            f"{odd} instant_generations rows carry a created_at that is not +00:00-suffixed; "
            "the 48 h window cannot be applied by string comparison"
        )


def _export_globals(
    conn: sqlite3.Connection, out: Path, plans: Sequence[UserPlan], cutoff: str
) -> dict:
    counts = {
        "users": _write_ndjson(
            layout.directory_path(out, "users"),
            "users",
            layout.DIRECTORY_USER_COLUMNS,
            ((p.id, p.is_anonymous, p.created_at, p.idx) for p in plans),
        )
    }

    merges = _columns(conn, "account_merges")
    counts["account_merges"] = _write_ndjson(
        layout.directory_path(out, "account_merges"),
        "account_merges",
        merges,
        _stream(
            conn,
            f"SELECT {_quoted(merges)} FROM account_merges ORDER BY rowid",
            (),
        ),
    )

    _assert_utc_offsets(conn)
    limiter = _columns(conn, "instant_generations")
    counts["instant_generations"] = _write_ndjson(
        layout.limiter_path(out),
        "instant_generations",
        limiter,
        _stream(
            conn,
            f"SELECT {_quoted(limiter)} FROM instant_generations"
            " WHERE created_at >= ? ORDER BY rowid",
            (cutoff,),
        ),
    )
    return counts


def _export_user(conn: sqlite3.Connection, out: Path, plan: UserPlan) -> dict:
    row = conn.execute("SELECT * FROM users WHERE tailscale_login = ?", (plan.id,)).fetchone()
    # The cell's `profile` carries id, created_at and is_anonymous as well as
    # the eight columns only it holds; the directory keeps its own copy of the
    # three because that is what it enumerates on.
    names = [cell for cell, _ in layout.PROFILE_FROM_USERS]
    values = [row[python] for _, python in layout.PROFILE_FROM_USERS]
    path = layout.profile_path(out, plan.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(_encode("users", names, values))
        fh.write("\n")

    counts: dict[str, int] = {}
    for table in layout.DATA_TABLES:
        columns = [c for c in _columns(conn, table) if c not in layout.USER_COLUMNS]
        counts[table] = _write_ndjson(
            layout.table_path(out, plan.id, table),
            table,
            columns,
            _stream(conn, _select(table, columns), (plan.id,)),
        )
    return {
        "id": plan.id,
        "idx": plan.idx,
        "dir": layout.user_dir_name(plan.id),
        "is_anonymous": plan.is_anonymous,
        "created_at": plan.created_at,
        "counts": counts,
    }


# ---- cli ------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m prep.migrate.export",
        description="Export a Python snapshot into the celld migration format.",
    )
    parser.add_argument("--snapshot", required=True, type=Path, help="a VACUUM INTO snapshot")
    parser.add_argument("--out", required=True, type=Path, help="the export directory to write")
    parser.add_argument(
        "--now",
        default=None,
        help="ISO-8601 instant the export is taken at; the limiter window ends here",
    )
    parser.add_argument(
        "--limiter-window-hours",
        type=int,
        default=layout.LIMITER_WINDOW_HOURS,
        help="trailing window of instant_generations to carry",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    now = layout.parse_instant(args.now, flag="--now") if args.now else None
    manifest = export(
        args.snapshot, args.out, now=now, limiter_window_hours=args.limiter_window_hours
    )
    signals = manifest["signals"]
    rows = sum(sum(u["counts"].values()) for u in manifest["users"])
    print(f"snapshot   {manifest['snapshot']['sha256']}  {manifest['snapshot']['bytes']} bytes")
    print(f"schema     {manifest['schema_fingerprint']}")
    print(f"users      {len(manifest['users'])}  ({rows} per-user rows)")
    print(
        f"directory  users={manifest['globals']['users']} "
        f"account_merges={manifest['globals']['account_merges']}"
    )
    print(
        f"limiter    {manifest['globals']['instant_generations']} rows since "
        f"{manifest['limiter']['cutoff']} "
        f"({signals['instant_generations_dropped']} older, dropped)"
    )
    for name, value in signals.items():
        if name.startswith("instant_generations"):
            continue
        print(f"  {name:<28} {value}")
    if signals["account_merges_started"]:
        print(
            f"WARNING: {signals['account_merges_started']} account_merges rows are still "
            "'started'; celld has no marker for a Python merge in flight (abort criterion)",
            file=sys.stderr,
        )
    if signals["grading_idempotency_orphans"]:
        print(
            f"WARNING: {signals['grading_idempotency_orphans']} grading_idempotency rows name a "
            "question that no longer exists; they belong to no user and are not exported",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
