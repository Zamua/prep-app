"""Merging an anonymous account into a provider account.

One public write, `merge_anonymous_into`, plus the boot-time guard
that keeps its policy map honest against the live schema.

Rows are REASSIGNED in place, never copied and recreated, so there is
no window in which the data exists only in a temporary form. The only
destructive statements are the three DELETE rules on expected-empty
tables (secrets and device endpoints, which must never change hands)
and the guarded `users` delete.

Derived tables carry no user column and get no statement: `cards` and
`reviews` (via `question_id`), `study_session_answers` (via
`session_id`), `trivia_queue` (via `question_id`), and the two
idempotency tables keyed by workflow id. Ownership follows their
parent. `account_merges` names two user ids and is the audit trail
itself: it is never rewritten by a merge.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from dataclasses import dataclass

from prep.infrastructure.db import cursor, now

log = logging.getLogger(__name__)

REASSIGN = "reassign"
REASSIGN_DROP_CONFLICTS = "reassign_drop_conflicts"
DELETE = "delete"


class UnknownUserScopedTable(RuntimeError):
    """The schema carries a user-scoped table with no merge rule. The
    anonymous delete would cascade its rows away in silence, so the
    boot fails instead."""


class LeftoverAnonRows(RuntimeError):
    """A table still holds rows for the anonymous account after its
    rule ran. Raised before the delete, so the merge rolls back with
    the anonymous account still owning everything."""


@dataclass(frozen=True)
class TableRule:
    table: str
    column: str
    rule: str
    # Non-user half of the primary key, for the drop-conflicts rule.
    conflict_key: str | None = None


@dataclass(frozen=True)
class MergeResult:
    """`resolved` means the browser's cookie is safe to discard;
    `merged` means data moved and the user should be told. Collapsing
    them is how a cookie gets dropped on a failure path."""

    resolved: bool
    merged: bool
    counts: dict[str, int]
    reason: str | None


# Order matters: parents before children, the users row last.
POLICY: tuple[TableRule, ...] = (
    TableRule("decks", "user_id", REASSIGN),
    TableRule("questions", "user_id", REASSIGN),
    TableRule("study_sessions", "user_id", REASSIGN),
    TableRule("trivia_sessions", "user_id", REASSIGN),
    TableRule("notifications_log", "user_id", REASSIGN),
    TableRule("active_workflows", "user_login", REASSIGN),
    TableRule("offline_sync_idempotency", "user_id", REASSIGN_DROP_CONFLICTS, "client_id"),
    TableRule("instant_generations", "user_id", REASSIGN),
    TableRule("push_subscriptions", "user_id", DELETE),
    TableRule("byok_credentials", "user_id", DELETE),
    TableRule("api_tokens", "user_id", DELETE),
)

POLICY_TABLES = frozenset(rule.table for rule in POLICY)

# The two `users` columns an anonymous user is allowed to set.
# COPY-IF-NULL: a target who already chose one keeps their own.
# `desired_retention` is load-bearing, not cosmetic: it already
# shaped the `next_due` values on the cards being merged.
CARRIED_USER_COLUMNS = ("desired_retention", "editor_input_mode")

# Columns that die with the anonymous row. Paired with
# CARRIED_USER_COLUMNS this enumerates every `users` column, which is
# what the column-drift test asserts against `PRAGMA table_info`.
DROPPED_USER_COLUMNS = (
    "tailscale_login",
    "display_name",
    "email",
    "profile_pic_url",
    "created_at",
    "last_seen_at",
    "is_anonymous",
    "notification_prefs",
    "active_byok_provider",
)

_USER_COLUMNS = ("user_id", "user_login")

# Slug de-collision: numbered suffixes first, then random ones. The
# random tail is unbounded by design - a user with a hundred
# same-named decks must not become permanently unmergeable.
_NUMBERED_SUFFIXES = range(2, 101)
_SUFFIX_BYTES = 3

POLICY_COLUMNS = frozenset((rule.table, rule.column) for rule in POLICY)


def discover_user_scoped_tables(c) -> dict[str, set[str]]:
    """Every table the anonymous delete can reach, mapped to EVERY
    column naming an owner: FKs to `users`, plus a `user_id` /
    `user_login` column with no declared FK (three tables carry one).

    A set, not one column: a table owning rows through two user
    columns would otherwise be half-covered, and the cascade would
    destroy the other half while both guards reported it clean."""
    found: dict[str, set[str]] = {}
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for row in tables:
        table = row["name"]
        if table == "users":
            continue
        cols = {r["name"] for r in c.execute(f'PRAGMA table_info("{table}")').fetchall()}
        columns = {name for name in _USER_COLUMNS if name in cols}
        columns |= {
            fk["from"]
            for fk in c.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            if fk["table"] == "users"
        }
        if columns:
            found[table] = columns
    return found


def assert_policy_covers_schema(c) -> None:
    """Fail closed on schema drift. Runs at boot, where a rollback is
    the available answer, and never on the request path, where the
    same mistake would 500 every request carrying an anon cookie.

    Keyed by (table, column): a new user column on a covered table is
    as uncovered as a whole new table."""
    discovered = {
        (table, column)
        for table, columns in discover_user_scoped_tables(c).items()
        for column in columns
    }
    uncovered = sorted(f"{table}.{column}" for table, column in discovered - POLICY_COLUMNS)
    if uncovered:
        raise UnknownUserScopedTable(
            "user-scoped columns with no merge rule in prep/auth/merge.py POLICY: "
            + ", ".join(uncovered)
        )


def previous_user_ids(target_user_id: str) -> list[str]:
    """Ids this account used to have, oldest first. Server-derived and
    never accepted from a client: the offline device compares its
    owner stamp against them instead of raising the mismatch dialog."""
    with cursor() as c:
        rows = c.execute(
            "SELECT anon_user_id FROM account_merges"
            " WHERE target_user_id = ? AND status = 'completed' ORDER BY id",
            (target_user_id,),
        ).fetchall()
    return [row["anon_user_id"] for row in rows]


def merge_anonymous_into(anon_user_id: str, target_user_id: str) -> MergeResult:
    """Move everything the anonymous account owns onto the target and
    delete it. One BEGIN IMMEDIATE transaction: a partial failure
    rolls back, leaving the anonymous account owning everything and
    the caller's cookie still pointing at it."""
    if anon_user_id == target_user_id:
        # Not an attempt at anything; recording it would only pollute
        # the trail the operator reads.
        return MergeResult(resolved=True, merged=False, counts={}, reason="same_user")

    with cursor() as c:
        refusal = _precheck(c, anon_user_id, target_user_id)
        if refusal is not None:
            return refusal
        audit_id = _audit_started(c, anon_user_id, target_user_id)

        c.execute("BEGIN IMMEDIATE")
        anon = _user_row(c, anon_user_id)
        if anon is None:
            return _refuse(c, audit_id, "anon_missing", resolved=True)
        if not anon["is_anonymous"]:
            return _refuse(c, audit_id, "not_anonymous", resolved=False)
        target = _user_row(c, target_user_id)
        if target is None:
            return _refuse(c, audit_id, "target_missing", resolved=False)

        counts = _move(c, anon, target, anon_user_id, target_user_id)
        _assert_no_leftovers(c, anon_user_id)
        c.execute("DELETE FROM users WHERE tailscale_login = ?", (anon_user_id,))
        c.execute(
            "UPDATE account_merges SET status = 'completed', completed_at = ?, counts = ?"
            " WHERE id = ?",
            (now(), json.dumps(counts, sort_keys=True), audit_id),
        )
    return MergeResult(resolved=True, merged=True, counts=counts, reason=None)


def _user_row(c, user_id: str):
    return c.execute("SELECT * FROM users WHERE tailscale_login = ?", (user_id,)).fetchone()


def _precheck(c, anon_user_id: str, target_user_id: str) -> MergeResult | None:
    """The two refusals that keep the cookie, read before the audit
    row exists and in the same order the transaction applies them.

    A kept cookie is re-presented on every authenticated request, so
    auditing an attempt that writes nothing else would grow
    `account_merges` without bound on a browser stuck in that state.
    Advisory only: the transaction below re-reads under the write lock
    and is the authoritative answer."""
    anon = _user_row(c, anon_user_id)
    if anon is None:
        return None
    if not anon["is_anonymous"]:
        return MergeResult(resolved=False, merged=False, counts={}, reason="not_anonymous")
    if _user_row(c, target_user_id) is None:
        return MergeResult(resolved=False, merged=False, counts={}, reason="target_missing")
    return None


def _audit_started(c, anon_user_id: str, target_user_id: str) -> int:
    """Committed before the merge transaction opens, so the record
    that an attempt happened survives that transaction rolling back."""
    row = c.execute(
        "INSERT INTO account_merges (anon_user_id, target_user_id, started_at, status)"
        " VALUES (?, ?, ?, 'started')",
        (anon_user_id, target_user_id, now()),
    )
    audit_id = int(row.lastrowid)
    c.commit()
    return audit_id


def _refuse(c, audit_id: int, reason: str, *, resolved: bool) -> MergeResult:
    """A precondition that stops the merge. Nothing else was written,
    so committing the audit update is the whole effect. A crash takes
    the other path and leaves the row `started`, which is what tells
    an operator to look."""
    c.execute(
        "UPDATE account_merges SET status = 'failed', completed_at = ?, error = ? WHERE id = ?",
        (now(), reason, audit_id),
    )
    return MergeResult(resolved=resolved, merged=False, counts={}, reason=reason)


def _move(c, anon, target, anon_user_id: str, target_user_id: str) -> dict[str, int]:
    """Apply every rule in policy order. Counts hold the rows each
    rule acted on; a rule that touched nothing is omitted."""
    counts: dict[str, int] = {}
    _decollide_deck_slugs(c, anon_user_id, target_user_id)
    for rule in POLICY:
        _apply(c, rule, anon_user_id, target_user_id, counts)
    _carry_preferences(c, anon, target, target_user_id, counts)
    return counts


def _apply(c, rule: TableRule, anon_user_id: str, target_user_id: str, counts: dict) -> None:
    table, column = rule.table, rule.column
    if rule.rule == DELETE:
        moved = c.execute(f'DELETE FROM "{table}" WHERE "{column}" = ?', (anon_user_id,)).rowcount
    else:
        if rule.rule == REASSIGN_DROP_CONFLICTS:
            key = rule.conflict_key
            dropped = c.execute(
                f'DELETE FROM "{table}" WHERE "{column}" = ? AND "{key}" IN'
                f' (SELECT "{key}" FROM "{table}" WHERE "{column}" = ?)',
                (anon_user_id, target_user_id),
            ).rowcount
            if dropped:
                counts[f"{table}.dropped"] = dropped
        moved = c.execute(
            f'UPDATE "{table}" SET "{column}" = ? WHERE "{column}" = ?',
            (target_user_id, anon_user_id),
        ).rowcount
    if moved:
        counts[table] = moved


def _carry_preferences(c, anon, target, target_user_id: str, counts: dict) -> None:
    """COPY-IF-NULL onto the target, never overwrite."""
    assignments = ", ".join(f"{col} = COALESCE({col}, ?)" for col in CARRIED_USER_COLUMNS)
    c.execute(
        f"UPDATE users SET {assignments} WHERE tailscale_login = ?",
        (*(anon[col] for col in CARRIED_USER_COLUMNS), target_user_id),
    )
    for col in CARRIED_USER_COLUMNS:
        if target[col] is None and anon[col] is not None:
            counts[f"users.{col}"] = 1


def _decollide_deck_slugs(c, anon_user_id: str, target_user_id: str) -> None:
    """`UNIQUE(user_id, name)` is the only unique constraint a merge
    can fire. Renaming happens on the anonymous row before the bulk
    update; `display_name` is untouched, so the user's label survives
    and only the URL slug moves."""
    clashes = c.execute(
        "SELECT id, name FROM decks WHERE user_id = ?"
        " AND name IN (SELECT name FROM decks WHERE user_id = ?) ORDER BY id",
        (anon_user_id, target_user_id),
    ).fetchall()
    if not clashes:
        return
    # Both namespaces: the rename must clear the anonymous account's
    # own UNIQUE before the row moves, and the target's after.
    taken = {
        row["name"]
        for row in c.execute(
            "SELECT name FROM decks WHERE user_id IN (?, ?)", (anon_user_id, target_user_id)
        ).fetchall()
    }
    for row in clashes:
        taken.add(_rename_deck(c, row["id"], row["name"], taken))


def _rename_deck(c, deck_id: int, name: str, taken: set[str]) -> str:
    candidates = _slug_candidates(name, taken)
    while True:
        candidate = next(candidates)
        try:
            c.execute("UPDATE decks SET name = ? WHERE id = ?", (candidate, deck_id))
        except sqlite3.IntegrityError:
            continue
        return candidate


def _slug_candidates(name: str, taken: set[str]):
    """Free numbered suffixes in order, then random ones without
    bound. Every candidate is a retry for the one before it, so an
    IntegrityError on any of them costs a suffix rather than the
    merge. There is no exhaustion branch: raising would make a user
    with a hundred same-named decks permanently unmergeable, and the
    random tail draws from 16.7M values against a set of deck names."""
    for n in _NUMBERED_SUFFIXES:
        candidate = f"{name}-{n}"
        if candidate not in taken:
            yield candidate
    while True:
        candidate = f"{name}-{secrets.token_hex(_SUFFIX_BYTES)}"
        if candidate not in taken:
            yield candidate


def _assert_no_leftovers(c, anon_user_id: str) -> None:
    """The `users` delete cascades, so prove it destroys nothing
    first. Discovered tables are counted alongside the policy's: a
    table the map does not cover is exactly the one whose rows would
    disappear in silence."""
    scoped = discover_user_scoped_tables(c)
    for rule in POLICY:
        scoped.setdefault(rule.table, set()).add(rule.column)
    for table, columns in sorted(scoped.items()):
        for column in sorted(columns):
            left = c.execute(
                f'SELECT COUNT(*) AS n FROM "{table}" WHERE "{column}" = ?', (anon_user_id,)
            ).fetchone()["n"]
            if left:
                raise LeftoverAnonRows(
                    f"{table}.{column} still holds {left} row(s) for {anon_user_id}"
                )
