"""The export directory's on-disk contract.

The exporter writes it, the importer replays it, the verifier reads the
cell side against it. Every name and every ordering decision lives here
so the three tools cannot drift.

    <out>/manifest.json
    <out>/directory/{users,account_merges}.ndjson
    <out>/limiter/instant_generations.ndjson
    <out>/users/<b64u(user_id)>/profile.json
    <out>/users/<b64u(user_id)>/<table>.ndjson

`manifest.json` is written last: its presence is what makes an export
directory complete.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

TOOL_VERSION = 1

MANIFEST_NAME = "manifest.json"
PROFILE_NAME = "profile.json"

# The 18 tables of the Python schema. The fingerprint in the manifest is
# taken over exactly this set, so a table added to db.py without a
# disposition here fails the export rather than being skipped in silence.
PYTHON_TABLES: tuple[str, ...] = (
    "account_merges",
    "active_workflows",
    "api_tokens",
    "byok_credentials",
    "cards",
    "decks",
    "grading_idempotency",
    "instant_generations",
    "notifications_log",
    "offline_sync_idempotency",
    "push_subscriptions",
    "questions",
    "reviews",
    "study_session_answers",
    "study_sessions",
    "trivia_queue",
    "trivia_sessions",
    "users",
)

# Per-user tables, parent before child: the order the importer inserts in,
# a subset of the cell's DATA_TABLES (worker/runtime/adapters/sql/schema.ts).
DATA_TABLES: tuple[str, ...] = (
    "decks",
    "questions",
    "cards",
    "reviews",
    "grading_idempotency",
    "offline_sync_idempotency",
    "study_sessions",
    "study_session_answers",
    "trivia_sessions",
    "trivia_queue",
    "notifications_log",
    "push_subscriptions",
    "byok_credentials",
    "api_tokens",
)

# The primary key each per-user table is keyed by inside a cell, user
# columns already dropped. One home, because the cell's upsert, the
# verifier's comparison and any fleet standing in for one all have to agree:
# a disagreement would hide a lost row rather than report it.
KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "decks": ("id",),
    "questions": ("id",),
    "cards": ("question_id",),
    "reviews": ("id",),
    "grading_idempotency": ("idempotency_key",),
    "offline_sync_idempotency": ("client_id",),
    "study_sessions": ("id",),
    "study_session_answers": ("session_id", "question_id"),
    "trivia_sessions": ("id",),
    "trivia_queue": ("question_id",),
    "notifications_log": ("id",),
    "push_subscriptions": ("endpoint",),
    "byok_credentials": ("provider",),
    "api_tokens": ("id",),
}

# Exported to nothing on purpose: every row names a Temporal execution that
# stops existing with the Go worker, and the badge is a 60 s read model.
RESET_TABLES: tuple[str, ...] = ("active_workflows",)

# Cell tables with no Python counterpart. `tombstone` is not a data table;
# it is listed so the verifier can assert the whole cell, not just DATA_TABLES.
EMPTY_TABLES: tuple[str, ...] = (
    "questions_idempotency",
    "steps_idempotency",
    "job_progress",
    "tombstone",
)

# Owner columns of the multi-user schema. A cell has none, so they are
# projected away at export and the remaining order is the cell's own.
USER_COLUMNS = frozenset({"user_id", "user_login"})

# `profile` column, `users` column. The cell's column order minus `id_base`,
# which the importer derives from `idx` rather than carrying.
PROFILE_FROM_USERS: tuple[tuple[str, str], ...] = (
    ("id", "tailscale_login"),
    ("display_name", "display_name"),
    ("profile_pic_url", "profile_pic_url"),
    ("email", "email"),
    ("created_at", "created_at"),
    ("last_seen_at", "last_seen_at"),
    ("is_anonymous", "is_anonymous"),
    ("notification_prefs", "notification_prefs"),
    ("editor_input_mode", "editor_input_mode"),
    ("active_byok_provider", "active_byok_provider"),
    ("desired_retention", "desired_retention"),
)

# DirectoryCell.users, in its own column order. `idx` is the exporter's.
DIRECTORY_USER_COLUMNS: tuple[str, ...] = ("id", "is_anonymous", "created_at", "idx")

# Twice the widest limiter window, so a dropped row can never change a
# decision while the largest global table stays small.
LIMITER_WINDOW_HOURS = 48


def to_utc(at: datetime) -> datetime:
    """Every timestamp these tools write has to render `+00:00`: the
    limiter window is applied by string comparison against an ISO cutoff,
    and an offset of `+02:00` would order wrongly against the rows. Naive
    means UTC, matching `PREP_FAKE_NOW`."""
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def parse_instant(raw: str, *, flag: str) -> datetime:
    """A `--now` argument, in ISO-8601. Named in the error, because a
    message about an environment variable is the wrong thing to read
    mid-cutover."""
    try:
        return to_utc(datetime.fromisoformat(raw.strip()))
    except ValueError:
        raise SystemExit(f"{flag}={raw!r} is not an ISO-8601 datetime") from None


def user_dir_name(user_id: str) -> str:
    """base64url, unpadded. User ids are opaque provider strings and
    anonymous ones carry a colon, so neither is a safe path segment."""
    return base64.urlsafe_b64encode(user_id.encode("utf-8")).decode("ascii").rstrip("=")


def user_id_from_dir(name: str) -> str:
    padding = "=" * (-len(name) % 4)
    return base64.urlsafe_b64decode(name + padding).decode("utf-8")


def manifest_path(out: Path) -> Path:
    return Path(out) / MANIFEST_NAME


def directory_path(out: Path, table: str) -> Path:
    return Path(out) / "directory" / f"{table}.ndjson"


def limiter_path(out: Path) -> Path:
    return Path(out) / "limiter" / "instant_generations.ndjson"


def user_dir(out: Path, user_id: str) -> Path:
    return Path(out) / "users" / user_dir_name(user_id)


def users_root(out: Path) -> Path:
    return Path(out) / "users"


def table_path(out: Path, user_id: str, table: str) -> Path:
    return user_dir(out, user_id) / f"{table}.ndjson"


def profile_path(out: Path, user_id: str) -> Path:
    return user_dir(out, user_id) / PROFILE_NAME


def read_manifest(out: Path) -> dict:
    with manifest_path(out).open(encoding="utf-8") as fh:
        return json.load(fh)


def read_profile(out: Path, user_id: str) -> dict:
    with profile_path(out, user_id).open(encoding="utf-8") as fh:
        return json.load(fh)


def iter_rows(path: Path) -> Iterator[dict]:
    """One decoded object per line. Streams: a 50,000-review table never
    lands in memory whole."""
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)
