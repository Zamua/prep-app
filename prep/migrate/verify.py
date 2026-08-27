"""The verifier: a snapshot and a fleet, compared until they agree.

    python -m prep.migrate.verify \\
      --snapshot <path> --base-url <url> --token-file <path> \\
      --generated-at <iso> [--manifest <path>] [--users <file>] \\
      [--at <iso>] [--json <report>]

The snapshot is one side and `GET /_migrate/dump` is the other. It holds
no state from a previous run, but it will not compare a fleet to a
snapshot the fleet was not built from: every import records the snapshot
digest on the directory, and a run whose `--snapshot` does not match it
aborts. Exit 0 clean, 1 with a report.

Three tiers, and none of them may be skipped. A tier that could not run
raises; a tier that ran and found nothing is the only thing that reads as
clean.

* **Tier 1, every row and every field.** All fourteen per-user tables,
  column by column, plus the directory both ways, the reset tables and
  the limiter's 48 h window. A row one side holds and the other does not
  is reported by its key, never folded into a count. The fields tier 2
  owns are excluded here, so each one is compared once, at its own tier.
* **Tier 2, FSRS state, field by field, as an exact oracle.** No tolerance
  anywhere: TEXT compares as bytes, REAL compares as bits. This is a copy,
  not a computation, and the 1e-9 the FSRS port is allowed would hide
  exactly the drift being hunted. `2026-08-26T14:00:00+00:00` and
  `...T14:00:00Z` are the same instant, a different byte, and a different
  rendered page.
* **Tier 3, the schedule oracle.** Bytes agreeing is necessary, not
  sufficient: a card can copy perfectly and still schedule differently if
  the retention resolution changes. Every migrated card is scheduled on
  both sides at one fixed clock and the two answers are compared.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from prep.infrastructure import clock
from prep.migrate import layout
from prep.migrate.cellreader import (
    DIRECTORY_CELL,
    LIMITER_CELL,
    CellReader,
    CellUnreachable,
    HttpCellReader,
    read_all,
    token_from_file,
)
from prep.migrate.compare import (
    REAL_TYPES,
    close,
    compare_count,
    compare_rows,
    compare_scalar,
    compare_streams,
    index_by_key,
    suffix_by,
)
from prep.migrate.divergence import TABLE_SCOPE, Divergence, Report, render, row_key

# `_SCOPE` is the one place a table is scoped to its owner. Imported rather
# than restated: an export and a verification that disagree about which rows
# belong to a user would cancel each other out silently.
from prep.migrate.export import _SCOPE as USER_SCOPE
from prep.migrate.export import DROPPED_BYOK_PROVIDER, carried_idx, plan_users
from prep.migrate.fsrs_oracle import (
    VERDICTS,
    NodeFsrsOracle,
    PyFsrsOracle,
    ScheduleInput,
    ScheduleOracle,
)
from prep.migrate.snapshot import open_snapshot, sha256_file

# The primary key each table is compared by, user columns already dropped.
KEY_COLUMNS = layout.KEY_COLUMNS

# Tier 2's fields, in the spec's order. `cards` has exactly these columns,
# so the whole row is the FSRS state.
CARD_FIELDS: tuple[str, ...] = (
    "question_id",
    "step",
    "fsrs_state",
    "next_due",
    "last_review",
    "stability",
    "difficulty",
)
CARD_REAL_COLUMNS = frozenset({"stability", "difficulty"})

# The fields tier 2 owns, by table. Tier 1 compares every other column of
# every table, so each field is read exactly once and at the tier that owns
# it. A field reported at both would make "tier 1 and tier 3 clean, tier 2
# waived" unreadable, and that is the waiver the runbook may have to take.
TIER2_FIELDS: dict[str, tuple[str, ...]] = {
    "cards": CARD_FIELDS,
    "decks": ("desired_retention",),
}

# Tier 3's tolerance, and the only one in the file: it belongs to the FSRS
# port, on values both sides computed rather than copied.
FSRS_TOLERANCE = 1e-9
EXACT_SCHEDULE_FIELDS: tuple[str, ...] = (
    "next_due",
    "fsrs_state",
    "interval_seconds",
    "step_bucket",
)

PROFILE_ROW = "profile"

# Tables held whole rather than streamed. `cards` because tier 2 compares it
# field by field and tier 3 schedules both sides of it; `decks` and
# `byok_credentials` because a later check re-reads them, and both are small.
MATERIALISED: frozenset[str] = frozenset({"cards", "decks", "byok_credentials"})


class VerificationImpossible(RuntimeError):
    """The verifier could not look. Never reported as clean."""


@dataclass(frozen=True)
class Fixed:
    """The two clocks the verifier is parameterised by. `at` is tier 3's,
    shared by both sides so the comparison is of implementations rather
    than of instants. `generated_at` is the export's, and it is required:
    it is what turns the limiter's trailing filter into a two-sided check,
    where an in-window row the cell lacks is a lost row rather than a row
    the filter is assumed to have dropped."""

    at: str
    generated_at: str


def snapshot_real_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return frozenset(r["name"] for r in info if str(r["type"]).strip().upper() in REAL_TYPES)


def snapshot_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(r["name"] for r in info if r["name"] not in layout.USER_COLUMNS)


class _SnapshotSide:
    """The snapshot side of one table, streamed, with the rows a
    disposition takes out set aside rather than compared. `kept` and
    `dropped` are only final once the stream has been drained."""

    def __init__(
        self, conn: sqlite3.Connection, table: str, user: str, columns: Sequence[str]
    ) -> None:
        self.conn = conn
        self.table = table
        self.user = user
        self.columns = columns
        self.kept = 0
        self.dropped: list[dict] = []

    def __iter__(self) -> Iterator[dict]:
        self.kept = 0
        self.dropped = []
        for row in _scoped_stream(self.conn, self.table, self.user, self.columns):
            # Decision 7.4: the export is a faithful copy and the importer
            # owns the policy, so the cell must not hold one.
            if self.table == "byok_credentials" and row.get("provider") == DROPPED_BYOK_PROVIDER:
                self.dropped.append(row)
                continue
            self.kept += 1
            yield row


def _scoped_stream(
    conn: sqlite3.Connection, table: str, user: str, columns: Sequence[str]
) -> Iterator[dict]:
    """One user's rows of one table, in rowid order, a batch at a time. The
    heaviest account's 50,000 reviews are compared without either side of
    the comparison being materialised."""
    projection = ", ".join(f't."{c}"' for c in columns)
    sql = f"SELECT {projection} {USER_SCOPE[table]} ORDER BY t.rowid"
    cursor = conn.execute(sql, (user,))
    while batch := cursor.fetchmany(1000):
        for row in batch:
            yield dict(zip(columns, row, strict=True))


class Verifier:
    def __init__(
        self,
        conn: sqlite3.Connection,
        reader: CellReader,
        *,
        fixed: Fixed,
        snapshot_sha256: str,
        carried: dict[str, int] | None = None,
        py_oracle: ScheduleOracle | None = None,
        ts_oracle: ScheduleOracle | None = None,
    ) -> None:
        self.conn = conn
        self.reader = reader
        self.fixed = fixed
        self.snapshot_sha256 = snapshot_sha256
        self.carried = carried
        self.py_oracle = py_oracle or PyFsrsOracle()
        self.ts_oracle = ts_oracle or NodeFsrsOracle()
        self.report = Report()
        self._cell_decks: dict = {}

    # ---- entry ------------------------------------------------------------

    def run(self, users: Sequence[str] | None = None) -> Report:
        self.verify_provenance()
        plans = plan_users(self.conn, self.carried)
        wanted = set(users) if users is not None else None
        if wanted is not None:
            missing = sorted(wanted - {p.id for p in plans})
            if missing:
                raise VerificationImpossible(
                    f"--users names {len(missing)} account(s) the snapshot has no row for: {', '.join(missing[:5])}"
                )
            plans = [p for p in plans if p.id in wanted]
        self.report.users = [p.id for p in plans]
        self.report.notes.append(f"tier 3 clock: {self.fixed.at}")
        self.report.notes.append(f"limiter window pinned to {self.fixed.generated_at}")
        self.report.notes.append(
            "idx carried from the manifest"
            if self.carried
            else "idx derived from the snapshot's own ranking"
        )

        self.verify_directory(plans, scoped=wanted is not None)
        self.verify_limiter()
        for plan in plans:
            self.verify_user(plan.id)
        return self.report

    def verify_provenance(self) -> None:
        """Which snapshot this fleet was built from, before a single row is
        compared.

        The verifier takes a snapshot path and a base URL and nothing ties
        them together; run it against the FIRST snapshot after the second
        import and it compares the fleet to the file the fleet was built
        from, reporting clean while the whole window's delta is missing.
        The importer records the digest on the directory, so it can be read
        back and refused."""
        run = self.reader.run()
        if run is None:
            raise VerificationImpossible(
                "the fleet does not say which snapshot it was built from. Every import "
                "records one; a fleet with no record was written by something else, and "
                "there is nothing to verify it against"
            )
        recorded = str(run.get("snapshot", ""))
        if recorded != self.snapshot_sha256:
            raise VerificationImpossible(
                f"this fleet was built from snapshot {recorded}, not {self.snapshot_sha256}. "
                "Verifying against the wrong snapshot would read clean while the second "
                "pass's whole delta is missing"
            )
        self.report.notes.append(f"snapshot {recorded} (as the fleet records it)")
        self.report.counted("provenance")

    # ---- the globals ------------------------------------------------------

    def verify_directory(self, plans: Sequence, *, scoped: bool) -> None:
        """`DirectoryCell.users` against the exporter's own assignment, and
        `account_merges` field by field: it is the source of `previous_ids`,
        so a lost row silently orphans a device's queue."""
        expected_users = [
            {"id": p.id, "is_anonymous": p.is_anonymous, "created_at": p.created_at, "idx": p.idx}
            for p in plans
        ]
        cell_users = list(read_all(self.reader, table="users", cell=DIRECTORY_CELL))
        self.report.counted("directory_users", len(cell_users))
        wanted = {u["id"] for u in expected_users}
        # A `--users` subset makes the directory a superset by design, so
        # only the named accounts are compared. A whole-snapshot run compares
        # BOTH ways: an account deleted between the two snapshots keeps its
        # directory row and its entire cell - decks, questions, reviews, push
        # subscriptions, BYOK ciphertext - and nothing else looks for it.
        # A parity seed at block 0 is the one row that legitimately has no
        # snapshot counterpart.
        cell_rows = [
            r for r in cell_users if r.get("id") in wanted or (not scoped and r.get("idx") != 0)
        ]
        self.report.add(
            *compare_rows(
                tier=1,
                table="directory.users",
                key_columns=("id",),
                snapshot_rows=expected_users,
                cell_rows=cell_rows,
                fields=("id", "is_anonymous", "created_at", "idx"),
            )
        )
        # Only a MIGRATED user is refused block 0. A parity host keeps its own
        # seed there, and every rehearsal fleet is one.
        for row in cell_users:
            if row.get("id") in wanted and row.get("idx") == 0:
                self.report.add(
                    Divergence(
                        tier=1,
                        table="directory.users",
                        row=row_key(("id",), row),
                        field="idx",
                        snapshot=">= 1",
                        cell="0",
                        note="block 0 belongs to the parity seed; a migrated user may never hold it",
                    )
                )

        merge_columns = snapshot_columns(self.conn, "account_merges")
        snapshot_merges = [
            dict(zip(merge_columns, row, strict=True))
            for row in self.conn.execute(
                f"SELECT {', '.join(chr(34) + c + chr(34) for c in merge_columns)} FROM account_merges ORDER BY rowid"
            ).fetchall()
        ]
        cell_merges = list(read_all(self.reader, table="account_merges", cell=DIRECTORY_CELL))
        self.report.counted("account_merges", len(snapshot_merges))
        self.report.add(
            *compare_rows(
                tier=1,
                table="directory.account_merges",
                key_columns=("id",),
                snapshot_rows=snapshot_merges,
                cell_rows=cell_merges,
                real_columns=snapshot_real_columns(self.conn, "account_merges"),
            )
        )
        # Its own abort criterion, not a copy defect: the row is carried
        # faithfully and it is the merge, not the migration, that cannot
        # resume. Named per row so the operator knows which one to wait on.
        for row in snapshot_merges:
            if row.get("status") == "started":
                self.report.warn(
                    Divergence(
                        tier=1,
                        table="account_merges",
                        row=row_key(("id",), row),
                        field="status",
                        snapshot="'started'",
                        cell="no marker exists",
                        note="celld has no marker for a Python merge in flight, so it would never resume; "
                        "let it finish or fail on Python before the window opens",
                    )
                )

        markers = list(read_all(self.reader, table="merge_markers", cell=DIRECTORY_CELL))
        for row in markers:
            self.report.add(
                Divergence(
                    tier=1,
                    table="directory.merge_markers",
                    row=row_key(("anon_id",), row),
                    field="<row>",
                    snapshot="absent",
                    cell="present",
                    note="merge_markers has no Python counterpart and is not created by the migration",
                )
            )

    def verify_limiter(self) -> None:
        """The ledger is the limiter's window source, so a reset would hand
        every IP a fresh burst allowance at the moment of highest exposure.
        The export keeps a trailing 48 h, so the check is two-sided against
        the pinned cutoff: every row inside the window is present and
        byte-identical, every row outside it is absent.

        Two-sided is the whole point. Filtering the snapshot down to the
        rows the cell happens to hold and comparing that makes a missing row
        vanish from both sides of the comparison, and an entirely empty
        ledger - the import skipped, or refused - reads as clean."""
        table = "instant_generations"
        columns = snapshot_columns(self.conn, table)
        snapshot_rows = [
            dict(zip(columns, row, strict=True))
            for row in self.conn.execute(
                f"SELECT {', '.join(chr(34) + c + chr(34) for c in columns)} FROM {table} ORDER BY rowid"
            ).fetchall()
        ]
        cell_rows = list(read_all(self.reader, table=table, cell=LIMITER_CELL))
        self.report.counted("limiter_rows", len(cell_rows))

        by_id = index_by_key(snapshot_rows, ("id",))
        kept: set = set()
        for row in cell_rows:
            key = (row.get("id"),)
            if key not in by_id:
                self.report.add(
                    Divergence(
                        tier=1,
                        table="limiter.instant_generations",
                        row=row_key(("id",), row),
                        field="<row>",
                        snapshot="absent",
                        cell="present",
                        note="the ledger holds a row the snapshot does not",
                    )
                )
                continue
            kept.add(row.get("id"))
        self.report.add(
            *compare_rows(
                tier=1,
                table="limiter.instant_generations",
                key_columns=("id",),
                snapshot_rows=[r for r in snapshot_rows if r.get("id") in kept],
                cell_rows=cell_rows,
                real_columns=snapshot_real_columns(self.conn, table),
            )
        )

        cutoff = (
            datetime.fromisoformat(self.fixed.generated_at)
            - timedelta(hours=layout.LIMITER_WINDOW_HOURS)
        ).isoformat()
        for row in snapshot_rows:
            inside = str(row["created_at"]) >= cutoff
            present = row.get("id") in kept
            if inside == present:
                continue
            self.report.add(
                Divergence(
                    tier=1,
                    table="limiter.instant_generations",
                    row=row_key(("id",), row),
                    field="created_at",
                    snapshot=render(row["created_at"]),
                    cell="present" if present else "absent",
                    note=(
                        f"inside the {layout.LIMITER_WINDOW_HOURS} h window that starts at "
                        f"{cutoff}, so the import lost it"
                        if inside
                        else f"outside the {layout.LIMITER_WINDOW_HOURS} h window that starts "
                        f"at {cutoff}, so the export should not have carried it"
                    ),
                )
            )

        # A second, independent reading of the same filter: a clean trailing
        # window is a suffix by `created_at`, whatever the cutoff was.
        oldest_kept, newest_dropped = suffix_by(snapshot_rows, kept, "id", "created_at")
        if (
            oldest_kept is not None
            and newest_dropped is not None
            and str(newest_dropped) >= str(oldest_kept)
        ):
            offender = next(
                r
                for r in snapshot_rows
                if r.get("id") not in kept and r["created_at"] == newest_dropped
            )
            self.report.add(
                Divergence(
                    tier=1,
                    table="limiter.instant_generations",
                    row=row_key(("id",), offender),
                    field="created_at",
                    snapshot=render(newest_dropped),
                    cell="dropped",
                    note=f"the filter is not a trailing window: this dropped row is newer than the oldest kept one ({oldest_kept})",
                )
            )

    # ---- one user ---------------------------------------------------------

    def verify_user(self, user: str) -> None:
        cards = self.verify_tables(user)
        self.verify_profile(user)
        self.verify_reset_and_empty(user)
        self.verify_schedule(user, cards)

    def verify_tables(self, user: str) -> tuple[list[dict], list[dict]]:
        """Tier 1 for the fourteen per-user tables, every column, and tier 2
        for `cards` and `decks.desired_retention`. Returns both sides of
        `cards` so the schedule oracle does not read them twice.

        Every column, because a key-against-key comparison reads no data at
        all: a mangled `questions.prompt`, a `push_subscriptions.p256dh`
        that no longer decrypts, a `byok_credentials.ciphertext`, a question
        moved to another deck - all of them verify clean when only the key
        is read, and the verifier exists to refuse exactly that.

        Streamed, so reading every column costs no more memory than reading
        the key did: the fields tier 2 owns are excluded here, so each one
        is compared once, at its own tier."""
        snapshot_cards: list[dict] = []
        cell_cards: list[dict] = []
        for table in layout.DATA_TABLES:
            keys = KEY_COLUMNS[table]
            columns = snapshot_columns(self.conn, table)
            owned = TIER2_FIELDS.get(table, ())
            tier1_fields = tuple(c for c in columns if c not in owned or c in keys)
            # `cards` is held whole: tier 2 compares it field by field and
            # tier 3 schedules both sides of it, so streaming it would only
            # mean reading it twice.
            source = _SnapshotSide(self.conn, table, user, columns)
            snapshot_rows = list(source) if table in MATERIALISED else source
            cell_stream = read_all(self.reader, table=table, user=user)
            cell_rows = list(cell_stream) if table in MATERIALISED else cell_stream
            self.report.add(
                *compare_streams(
                    tier=1,
                    user=user,
                    table=table,
                    key_columns=keys,
                    snapshot_rows=snapshot_rows,
                    cell_rows=cell_rows,
                    real_columns=snapshot_real_columns(self.conn, table),
                    fields=tier1_fields,
                )
            )
            self.report.counted(f"rows.{table}", source.kept)
            for row in source.dropped:
                if any(r.get("provider") == DROPPED_BYOK_PROVIDER for r in cell_rows):
                    self.report.add(
                        Divergence(
                            tier=1,
                            user=user,
                            table=table,
                            row=row_key(keys, row),
                            field="provider",
                            snapshot=f"exported, then dropped at import ({DROPPED_BYOK_PROVIDER})",
                            cell="present",
                            note="decision 7.4: the app-wide subscription credential never reaches a cell",
                        )
                    )
            if table == "cards":
                snapshot_cards, cell_cards = list(snapshot_rows), list(cell_rows)
                self.report.add(
                    *compare_rows(
                        tier=2,
                        user=user,
                        table="cards",
                        key_columns=keys,
                        snapshot_rows=snapshot_rows,
                        cell_rows=cell_rows,
                        real_columns=CARD_REAL_COLUMNS,
                        fields=CARD_FIELDS,
                    )
                )
                self.report.counted("cards_tier2", len(snapshot_rows))
            if table == "decks":
                self.report.add(
                    *compare_rows(
                        tier=2,
                        user=user,
                        table="decks",
                        key_columns=keys,
                        snapshot_rows=snapshot_rows,
                        cell_rows=cell_rows,
                        real_columns=frozenset({"desired_retention"}),
                        fields=("desired_retention",),
                    )
                )
                self._decks = {r["id"]: r for r in snapshot_rows}
                self._cell_decks = {r.get("id"): r for r in cell_rows}
        return snapshot_cards, cell_cards

    def _page_columns(self, user: str, table: str, columns: Sequence[str]):
        """A projected dump, for a check that wants named columns rather
        than whole rows. A page returning a cursor it has already returned
        would spin forever; that is reported, not spun on, exactly as
        `read_all` does it."""
        after: int | None = None
        seen: set[int] = set()
        while True:
            page = self.reader.page(table=table, user=user, after=after, columns=list(columns))
            yield from page.rows
            if page.next is None:
                return
            if page.next in seen:
                raise CellUnreachable(f"{table} paged back to cursor {page.next}")
            seen.add(page.next)
            after = page.next

    def verify_profile(self, user: str) -> None:
        """`users` splits: three columns to the directory, the other eight
        to the profile. `last_seen_at` verbatim, because it is the anonymous
        reaper's only input."""
        row = self.conn.execute("SELECT * FROM users WHERE tailscale_login = ?", (user,)).fetchone()
        if row is None:
            raise VerificationImpossible(f"the snapshot has no users row for {user}")
        source = dict(row)
        cell_rows = list(read_all(self.reader, table=PROFILE_ROW, user=user))
        if len(cell_rows) != 1:
            self.report.add(
                Divergence(
                    tier=1,
                    user=user,
                    table="profile",
                    row=TABLE_SCOPE,
                    field="<row>",
                    snapshot="1",
                    cell=str(len(cell_rows)),
                    note="a migrated user holds exactly one profile row",
                )
            )
            return
        cell = cell_rows[0]
        self.report.counted("profiles")
        for profile_column, users_column in layout.PROFILE_FROM_USERS:
            expected = source.get(users_column)
            if profile_column == "active_byok_provider" and expected == DROPPED_BYOK_PROVIDER:
                # The credential is dropped at import, so the pointer to it
                # must be nulled rather than left dangling.
                expected = None
            self.report.add(
                *compare_scalar(
                    tier=2 if profile_column == "desired_retention" else 1,
                    user=user,
                    table="profile",
                    row=PROFILE_ROW,
                    field=profile_column,
                    snapshot_value=expected,
                    cell_value=cell.get(profile_column),
                    real=profile_column == "desired_retention",
                )
            )

    def verify_reset_and_empty(self, user: str) -> None:
        """`active_workflows` is reset on purpose and the four cell-only
        tables have no Python counterpart. A row in any of them means the
        import carried something it should not have."""
        for table in (*layout.RESET_TABLES, *layout.EMPTY_TABLES):
            rows = list(read_all(self.reader, table=table, user=user))
            self.report.add(
                *compare_count(
                    tier=1,
                    user=user,
                    table=table,
                    expected=0,
                    actual=len(rows),
                    note="reset by design: every row names a Temporal execution that stops existing with the Go worker"
                    if table in layout.RESET_TABLES
                    else "no Python counterpart, so the migration leaves it empty",
                )
            )
            for row in rows[:20]:
                self.report.add(
                    Divergence(
                        tier=1,
                        user=user,
                        table=table,
                        row=render(row),
                        field="<row>",
                        snapshot="absent",
                        cell="present",
                    )
                )

    # ---- tier 3 -----------------------------------------------------------

    def verify_schedule(self, user: str, cards: tuple[list[dict], list[dict]]) -> None:
        snapshot_cards, cell_cards = cards
        if not snapshot_cards and not cell_cards:
            return
        snapshot_retention = self._snapshot_retention(user)
        cell_retention = self._cell_retention(user)
        py_inputs = [self._input(c, snapshot_retention) for c in snapshot_cards]
        ts_inputs = [self._input(c, cell_retention) for c in cell_cards]
        py_out = self.py_oracle.schedule(py_inputs, self.fixed.at)
        ts_out = self.ts_oracle.schedule(ts_inputs, self.fixed.at)
        self.report.counted("cards_tier3", len(py_inputs))

        for card in py_inputs:
            left = py_out.get(card.key)
            right = ts_out.get(card.key)
            if left is None or right is None:
                # Tier 1 already named the missing row; this says the oracle
                # had nothing to schedule rather than that it agreed.
                continue
            for verdict in VERDICTS:
                self.report.add(
                    *self._compare_schedule(user, card.key, verdict, left[verdict], right[verdict])
                )
                self.report.counted("transitions_tier3")

    def _compare_schedule(
        self, user: str, key: str, verdict: str, left: dict, right: dict
    ) -> list[Divergence]:
        table = f"cards[{verdict}]"
        if ("error" in left) != ("error" in right) or left.get("error") != right.get("error"):
            return [
                Divergence(
                    tier=3,
                    user=user,
                    table=table,
                    row=key,
                    field="<outcome>",
                    snapshot=left.get("error", "scheduled"),
                    cell=right.get("error", "scheduled"),
                    note="the two schedulers refused differently on the same state",
                )
            ]
        if "error" in left:
            return []
        found: list[Divergence] = []
        for field in ("stability", "difficulty"):
            if not close(left[field], right[field], FSRS_TOLERANCE):
                found.append(
                    Divergence(
                        tier=3,
                        user=user,
                        table=table,
                        row=key,
                        field=field,
                        snapshot=render(left[field]),
                        cell=render(right[field]),
                        note=f"beyond the port's {FSRS_TOLERANCE:g} tolerance",
                    )
                )
        for field in EXACT_SCHEDULE_FIELDS:
            if left[field] != right[field]:
                found.append(
                    Divergence(
                        tier=3,
                        user=user,
                        table=table,
                        row=key,
                        field=field,
                        snapshot=render(left[field]),
                        cell=render(right[field]),
                    )
                )
        return found

    def _input(self, card: dict, retention: dict) -> ScheduleInput:
        qid = card.get("question_id")
        return ScheduleInput(
            key=f"question_id={qid}",
            stability=card.get("stability"),
            difficulty=card.get("difficulty"),
            fsrs_state=int(card.get("fsrs_state") or 1),
            last_review=card.get("last_review"),
            retention=retention.get(qid),
        )

    def _snapshot_retention(self, user: str) -> dict:
        """deck override, then the user default, then the algorithm's own,
        which `schedule_review` applies for a null."""
        rows = self.conn.execute(
            """SELECT q.id AS question_id, d.desired_retention AS deck_ret, u.desired_retention AS user_ret
                 FROM questions q
                 JOIN decks d ON d.id = q.deck_id
                 JOIN users u ON u.tailscale_login = q.user_id
                WHERE q.user_id = ?""",
            (user,),
        ).fetchall()
        return {
            r["question_id"]: (r["deck_ret"] if r["deck_ret"] is not None else r["user_ret"])
            for r in rows
        }

    def _cell_retention(self, user: str) -> dict:
        profile = next(iter(read_all(self.reader, table=PROFILE_ROW, user=user)), {})
        user_ret = profile.get("desired_retention")
        decks = {r.get("id"): r.get("desired_retention") for r in self._cell_decks.values()}
        out: dict = {}
        for row in self._page_columns(user, "questions", ("id", "deck_id")):
            deck_ret = decks.get(row.get("deck_id"))
            out[row.get("id")] = deck_ret if deck_ret is not None else user_ret
        return out


# ---- the CLI --------------------------------------------------------------


def _fixed_clock(raw: str | None) -> str:
    """Whole seconds, so both sides serialise the same string. A fraction
    would put microseconds on one side of the comparison and not the
    other."""
    at = clock.parse_fake_now(raw) if raw is not None else clock.now()
    return at.replace(microsecond=0).isoformat()


def _users_file(path: Path | None) -> list[str] | None:
    if path is None:
        return None
    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m prep.migrate.verify",
        description="Compare a snapshot against a celld fleet, field by field.",
    )
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument(
        "--users", type=Path, help="one user id per line; the default is every user in the snapshot"
    )
    parser.add_argument(
        "--generated-at",
        required=True,
        help="the export's generated_at, which pins the limiter's 48 h window. Required: "
        "without it a missing ledger row cannot be told from one the filter dropped",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="the export's manifest.json. Its per-user idx is what the directory is "
        "checked against, which a second pass carrying idx forward needs",
    )
    parser.add_argument("--at", help="tier 3's fixed clock (default: now, whole seconds)")
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="divergences printed before the report refers to --json",
    )
    parser.add_argument(
        "--waive-tier2-ulp",
        action="store_true",
        help="accept tier-2 float differences of exactly one representable "
        "double: celld's SQLite bridge shifts that last bit on some values, "
        "and tier 3 proves no schedule moved. A wider gap still fails",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        reader = HttpCellReader(
            args.base_url, token_from_file(args.token_file), timeout=args.timeout
        )
        carried = None
        if args.manifest is not None:
            with Path(args.manifest).open(encoding="utf-8") as fh:
                carried = carried_idx(json.load(fh))
        digest = sha256_file(args.snapshot)
        conn = open_snapshot(args.snapshot)
    except (OSError, CellUnreachable, RuntimeError) as e:
        print(f"ABORT: {e}", file=sys.stderr)
        return 1

    try:
        verifier = Verifier(
            conn,
            reader,
            fixed=Fixed(at=_fixed_clock(args.at), generated_at=args.generated_at),
            snapshot_sha256=digest,
            carried=carried,
        )
        report = verifier.run(_users_file(args.users))
    except (CellUnreachable, VerificationImpossible, RuntimeError) as e:
        print(f"ABORT: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if args.waive_tier2_ulp:
        report.waive_ulp()
    if args.json is not None:
        Path(args.json).write_text(json.dumps(report.as_json(), indent=2) + "\n", encoding="utf-8")
    if report.warnings:
        print(report.warning_text(), file=sys.stderr)
    print(report.text(limit=args.limit))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
