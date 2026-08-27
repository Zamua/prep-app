"""The verifier against a snapshot and a cell side built from its export.

The cell fixture is what a correct import produces, so a clean run pins
the whole path; every other test breaks one thing and asserts the report
names the user, the table, the row and the field. A bare count would send
the operator back to SQL, which is the failure mode these tests exist to
prevent.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from migrate import layout
from migrate.cellreader import (
    DIRECTORY_CELL,
    LIMITER_CELL,
    CellSealed,
    FixtureCellReader,
    Page,
)
from migrate.divergence import Divergence, float_bits
from migrate.export import DROPPED_BYOK_PROVIDER
from migrate.fsrs_oracle import NodeFsrsOracle, ScheduleInput
from migrate.snapshot import open_snapshot, sha256_file
from migrate.verify import Fixed, VerificationImpossible, Verifier

AT = "2026-09-01T12:00:00+00:00"
GENERATED_AT = "2026-08-26T14:00:00+00:00"
OPENED_AT = "2026-08-26T14:05:00+00:00"


@pytest.fixture(scope="session")
def node_oracle() -> NodeFsrsOracle:
    """One esbuild bundle for the whole module; the oracle is stateless
    past it."""
    oracle = NodeFsrsOracle()
    oracle.bundle()
    return oracle


def cell_from_export(out: Path, manifest: dict) -> dict[tuple[str, str], list[dict]]:
    """The cell side a correct import leaves behind. The two dispositions
    that are not a straight copy live here because they live in the
    importer: the subscription credential is dropped and the profile
    pointer to it is nulled."""
    tables: dict[tuple[str, str], list[dict]] = {
        (DIRECTORY_CELL, "users"): list(layout.iter_rows(layout.directory_path(out, "users"))),
        (DIRECTORY_CELL, "account_merges"): list(
            layout.iter_rows(layout.directory_path(out, "account_merges"))
        ),
        (DIRECTORY_CELL, "merge_markers"): [],
        (LIMITER_CELL, "instant_generations"): list(layout.iter_rows(layout.limiter_path(out))),
    }
    for entry in manifest["users"]:
        user = entry["id"]
        for table in layout.DATA_TABLES:
            rows = list(layout.iter_rows(layout.table_path(out, user, table)))
            if table == "byok_credentials":
                rows = [r for r in rows if r["provider"] != DROPPED_BYOK_PROVIDER]
            tables[(user, table)] = rows
        profile = dict(layout.read_profile(out, user))
        if profile.get("active_byok_provider") == DROPPED_BYOK_PROVIDER:
            profile["active_byok_provider"] = None
        tables[(user, "profile")] = [profile]
        for table in (*layout.RESET_TABLES, *layout.EMPTY_TABLES):
            tables[(user, table)] = []
    return tables


@pytest.fixture(scope="session")
def fleet(exported: tuple[Path, dict]) -> dict[tuple[str, str], list[dict]]:
    out, manifest = exported
    return cell_from_export(out, manifest)


def run(
    snapshot: Path,
    tables: dict,
    node_oracle,
    users=None,
    generated_at=GENERATED_AT,
    run_record=None,
    carried=None,
):
    conn = open_snapshot(snapshot)
    digest = sha256_file(snapshot)
    if run_record is None:
        run_record = {"snapshot": digest, "openedAt": OPENED_AT}
    try:
        verifier = Verifier(
            conn,
            FixtureCellReader(tables, run=run_record),
            fixed=Fixed(at=AT, generated_at=generated_at),
            snapshot_sha256=digest,
            carried=carried,
            ts_oracle=node_oracle,
        )
        return verifier.run(users)
    finally:
        conn.close()


def by_field(report, tier: int, table: str, field: str) -> list[Divergence]:
    return [
        d for d in report.divergences if d.tier == tier and d.table == table and d.field == field
    ]


# ---- the clean run --------------------------------------------------------


def test_a_correct_import_verifies_clean(snapshot, fleet, node_oracle):
    report = run(snapshot, copy.deepcopy(fleet), node_oracle)
    assert report.divergences == [], report.text()
    assert report.clean
    # A run that examined nothing must not be able to read as clean.
    assert report.checks["cards_tier2"] > 0
    assert report.checks["cards_tier3"] > 0
    assert report.checks["transitions_tier3"] == report.checks["cards_tier3"] * 2
    assert report.checks["rows.reviews"] > 0
    assert report.checks["profiles"] == len(report.users)


def test_the_mid_merge_is_a_warning_not_a_divergence(snapshot, fleet, node_oracle, plan):
    report = run(snapshot, copy.deepcopy(fleet), node_oracle)
    assert report.clean
    started = [d for d in report.warnings if d.field == "status"]
    assert started, "the synthetic snapshot carries one merge left 'started'"
    assert "would never resume" in started[0].note
    assert "WARNING (abort criterion)" in report.warning_text()


def test_a_users_file_narrows_the_run(snapshot, fleet, node_oracle, plan):
    report = run(snapshot, copy.deepcopy(fleet), node_oracle, users=[plan.heavy])
    assert report.users == [plan.heavy]
    assert report.clean


def test_an_unknown_user_is_refused_rather_than_skipped(snapshot, fleet, node_oracle):
    with pytest.raises(VerificationImpossible, match="no row for"):
        run(snapshot, copy.deepcopy(fleet), node_oracle, users=["nobody@example.com"])


# ---- tier 2: the copy, byte for byte and bit for bit ----------------------


def test_one_flipped_stability_bit_names_the_card(snapshot, fleet, node_oracle, plan):
    tables = copy.deepcopy(fleet)
    card = next(c for c in tables[(plan.heavy, "cards")] if c["stability"] is not None)
    original = card["stability"]
    # The next double up: same repr to six figures, a different value.
    card["stability"] = original * (1 + 2**-52)
    report = run(snapshot, tables, node_oracle)
    found = by_field(report, 2, "cards", "stability")
    assert len(found) == 1
    assert found[0].user == plan.heavy
    assert found[0].row == f"question_id={card['question_id']}"
    assert float_bits(original) in found[0].snapshot
    assert float_bits(card["stability"]) in found[0].cell
    assert not report.clean


def test_a_z_suffixed_timestamp_is_a_divergence_tier_3_cannot_see(
    snapshot, fleet, node_oracle, plan
):
    """`...+00:00` and `...Z` are the same instant and a different byte.
    The schedule oracle parses both to the same moment, so only the byte
    comparison catches it, and it is what a golden renders."""
    tables = copy.deepcopy(fleet)
    card = tables[(plan.heavy, "cards")][0]
    card["next_due"] = card["next_due"].replace("+00:00", "Z")
    report = run(snapshot, tables, node_oracle)
    found = by_field(report, 2, "cards", "next_due")
    assert len(found) == 1
    assert found[0].row == f"question_id={card['question_id']}"
    assert by_field(report, 3, "cards[right]", "next_due") == []


def test_a_dropped_column_is_named_rather_than_ignored(snapshot, fleet, node_oracle, plan):
    tables = copy.deepcopy(fleet)
    for card in tables[(plan.heavy, "cards")]:
        card.pop("difficulty")
    report = run(snapshot, tables, node_oracle)
    found = by_field(report, 2, "cards", "difficulty")
    assert found and all(d.cell == "<column absent>" for d in found)


def test_a_drifted_profile_retention_is_bit_exact(snapshot, fleet, node_oracle, plan):
    tables = copy.deepcopy(fleet)
    tables[(plan.retention_high, "profile")][0]["desired_retention"] = 0.96
    report = run(snapshot, tables, node_oracle)
    found = by_field(report, 2, "profile", "desired_retention")
    assert len(found) == 1
    assert found[0].user == plan.retention_high


# ---- tier 1: rows, not counts --------------------------------------------


def test_a_lost_review_is_named_by_its_id(snapshot, fleet, node_oracle, plan):
    tables = copy.deepcopy(fleet)
    lost = tables[(plan.heavy, "reviews")].pop(3)
    report = run(snapshot, tables, node_oracle)
    found = by_field(report, 1, "reviews", "<row>")
    assert len(found) == 1
    assert found[0].row == f"id={lost['id']}"
    assert found[0].cell == "absent"
    assert found[0].user == plan.heavy


def test_an_invented_row_is_named_too(snapshot, fleet, node_oracle, plan):
    tables = copy.deepcopy(fleet)
    extra = dict(tables[(plan.heavy, "reviews")][0])
    extra["id"] = 10_000_001
    tables[(plan.heavy, "reviews")].append(extra)
    report = run(snapshot, tables, node_oracle)
    found = by_field(report, 1, "reviews", "<row>")
    assert len(found) == 1
    assert found[0].row == "id=10000001"
    assert found[0].snapshot == "absent"


def test_a_surviving_subscription_credential_is_refused(snapshot, fleet, node_oracle, plan):
    tables = copy.deepcopy(fleet)
    tables[(plan.subscription_user, "byok_credentials")].append(
        {
            "provider": DROPPED_BYOK_PROVIDER,
            "ciphertext": "x",
            "key_prefix": "sk-ant",
            "created_at": GENERATED_AT,
            "last_used_at": None,
        }
    )
    report = run(snapshot, tables, node_oracle)
    found = [d for d in report.divergences if d.table == "byok_credentials"]
    assert found
    assert any("7.4" in d.note for d in found)


def test_a_carried_workflow_row_is_refused(snapshot, fleet, node_oracle, plan):
    tables = copy.deepcopy(fleet)
    tables[(plan.heavy, "active_workflows")] = [{"workflow_id": "grade-1", "status": "running"}]
    report = run(snapshot, tables, node_oracle)
    found = [d for d in report.divergences if d.table == "active_workflows"]
    assert len(found) == 2, "the count and the row it names"
    assert any(d.field == "<count>" for d in found)
    assert any("grade-1" in d.row for d in found)


def test_block_zero_is_refused(snapshot, fleet, node_oracle):
    tables = copy.deepcopy(fleet)
    tables[(DIRECTORY_CELL, "users")][0]["idx"] = 0
    report = run(snapshot, tables, node_oracle)
    assert any(d.field == "idx" and "block 0" in d.note for d in report.divergences)


def test_the_seed_at_block_zero_is_not_a_divergence(snapshot, fleet, node_oracle):
    """A parity host holds its own seed at idx 0. Only a MIGRATED user may
    never hold that block, so the seed row is not the migration's business."""
    tables = copy.deepcopy(fleet)
    tables[(DIRECTORY_CELL, "users")].append(
        {
            "id": "parity@example.com",
            "is_anonymous": 0,
            "created_at": "2026-03-14T15:00:00+00:00",
            "idx": 0,
        }
    )
    report = run(snapshot, tables, node_oracle)
    assert report.clean, report.text()


def test_a_lost_merge_audit_row_is_named(snapshot, fleet, node_oracle):
    tables = copy.deepcopy(fleet)
    lost = tables[(DIRECTORY_CELL, "account_merges")].pop()
    report = run(snapshot, tables, node_oracle)
    found = by_field(report, 1, "directory.account_merges", "<row>")
    assert len(found) == 1
    assert found[0].row == f"id={lost['id']}"


def test_a_merge_marker_the_migration_did_not_create(snapshot, fleet, node_oracle):
    tables = copy.deepcopy(fleet)
    tables[(DIRECTORY_CELL, "merge_markers")] = [{"anon_id": "anon:1", "target_id": "t"}]
    report = run(snapshot, tables, node_oracle)
    assert any(d.table == "directory.merge_markers" for d in report.divergences)


# ---- tier 1: the limiter window ------------------------------------------


def test_a_reset_ledger_is_refused_row_by_row(snapshot, fleet, node_oracle):
    tables = copy.deepcopy(fleet)
    kept = tables[(LIMITER_CELL, "instant_generations")]
    assert kept, "the fixture carries a windowed ledger"
    tables[(LIMITER_CELL, "instant_generations")] = []
    report = run(snapshot, tables, node_oracle)
    found = [d for d in report.divergences if d.table == "limiter.instant_generations"]
    assert len(found) >= len(kept)
    assert {d.row for d in found} >= {f"id={r['id']}" for r in kept}


def test_a_row_outside_the_window_is_named_with_the_cutoff(snapshot, fleet, node_oracle):
    tables = copy.deepcopy(fleet)
    tables[(LIMITER_CELL, "instant_generations")].pop(0)
    report = run(snapshot, tables, node_oracle)
    found = [d for d in report.divergences if d.table == "limiter.instant_generations"]
    assert found
    assert any("48 h window that starts at" in d.note for d in found)


def test_a_hole_in_the_middle_of_the_window_is_two_divergences(snapshot, fleet, node_oracle):
    """One from the cutoff check (an in-window row the cell lacks) and one
    from the suffix check (a dropped row newer than the oldest kept one).
    Two independent readings of the same filter."""
    tables = copy.deepcopy(fleet)
    rows = tables[(LIMITER_CELL, "instant_generations")]
    if len(rows) < 3:
        pytest.skip("the fixture's window is too short to hole")
    rows.pop(len(rows) // 2)
    report = run(snapshot, tables, node_oracle)
    found = [d for d in report.divergences if d.table == "limiter.instant_generations"]
    assert any("not a trailing window" in d.note for d in found)
    assert any("so the import lost it" in d.note for d in found)


# ---- tier 3: the schedule oracle -----------------------------------------


class DriftingOracle:
    """A cell-side scheduler that answers one card differently. Nothing in
    the copy has changed, which is the point: tier 3 is the only tier that
    can see this."""

    def __init__(self, inner, key: str, mutate) -> None:
        self.inner = inner
        self.key = key
        self.mutate = mutate

    def schedule(self, cards, now):
        out = self.inner.schedule(cards, now)
        if self.key in out:
            out[self.key] = {v: self.mutate(dict(r)) for v, r in out[self.key].items()}
        return out


def one_card_key(fleet, user: str) -> str:
    card = next(c for c in fleet[(user, "cards")] if c["stability"] is not None)
    return f"question_id={card['question_id']}"


def test_a_drifted_stability_beyond_the_tolerance_is_named(snapshot, fleet, node_oracle, plan):
    key = one_card_key(fleet, plan.heavy)

    def bump(row):
        if "stability" in row:
            row["stability"] = row["stability"] + 1e-6
        return row

    report = run(
        snapshot,
        copy.deepcopy(fleet),
        DriftingOracle(node_oracle, key, bump),
        users=[plan.heavy],
    )
    found = [d for d in report.divergences if d.tier == 3 and d.field == "stability"]
    assert found
    assert {d.row for d in found} == {key}
    assert "tolerance" in found[0].note


def test_a_drifted_due_date_is_named_exactly(snapshot, fleet, node_oracle, plan):
    key = one_card_key(fleet, plan.heavy)

    def shift(row):
        if "next_due" in row:
            row["next_due"] = row["next_due"].replace("T12:", "T13:")
        return row

    report = run(
        snapshot,
        copy.deepcopy(fleet),
        DriftingOracle(node_oracle, key, shift),
        users=[plan.heavy],
    )
    found = [d for d in report.divergences if d.tier == 3 and d.field == "next_due"]
    assert found and {d.row for d in found} == {key}


def test_a_scheduler_that_refuses_differently_is_named(snapshot, fleet, node_oracle, plan):
    key = one_card_key(fleet, plan.heavy)
    report = run(
        snapshot,
        copy.deepcopy(fleet),
        DriftingOracle(node_oracle, key, lambda _row: {"error": "InvalidCardState"}),
        users=[plan.heavy],
    )
    found = [d for d in report.divergences if d.tier == 3 and d.field == "<outcome>"]
    assert found
    assert found[0].snapshot == "scheduled"
    assert found[0].cell == "InvalidCardState"


def test_a_drifted_deck_retention_changes_the_schedule(snapshot, fleet, node_oracle, plan):
    """Resolution, not bytes: the deck override the cell resolves is what
    tier 3 reads, so a wrong one moves every card in that deck."""
    tables = copy.deepcopy(fleet)
    deck = next(d for d in tables[(plan.retention_high, "decks")])
    deck["desired_retention"] = 0.70
    report = run(snapshot, tables, node_oracle, users=[plan.retention_high])
    assert by_field(report, 2, "decks", "desired_retention")
    assert [d for d in report.divergences if d.tier == 3 and d.field == "next_due"]


# ---- the verifier refuses to guess ---------------------------------------


class SealedReader:
    def page(self, **_kwargs) -> Page:
        raise CellSealed("the fleet is sealed")

    def run(self) -> dict | None:
        raise CellSealed("the fleet is sealed")


def test_a_sealed_fleet_aborts_rather_than_reading_clean(snapshot, node_oracle):
    conn = open_snapshot(snapshot)
    try:
        verifier = Verifier(
            conn,
            SealedReader(),
            fixed=Fixed(at=AT, generated_at=GENERATED_AT),
            snapshot_sha256=sha256_file(snapshot),
            ts_oracle=node_oracle,
        )
        with pytest.raises(CellSealed):
            verifier.run()
    finally:
        conn.close()


def test_the_two_oracles_agree_on_the_snapshot_itself(snapshot, node_oracle):
    """The port against its reference on real rows rather than a corpus:
    every card in the snapshot, both verdicts, at one clock."""
    from migrate.fsrs_oracle import PyFsrsOracle

    conn = open_snapshot(snapshot)
    try:
        rows = conn.execute(
            "SELECT question_id, stability, difficulty, fsrs_state, last_review FROM cards"
        ).fetchall()
    finally:
        conn.close()
    cards = [
        ScheduleInput(
            key=f"question_id={r['question_id']}",
            stability=r["stability"],
            difficulty=r["difficulty"],
            fsrs_state=r["fsrs_state"] or 1,
            last_review=r["last_review"],
            retention=None,
        )
        for r in rows
    ]
    assert cards
    assert PyFsrsOracle().schedule(cards, AT) == node_oracle.schedule(cards, AT)


# ---- tier 1 reads every column -------------------------------------------


# One non-key field per table: the thing the row is FOR. `cards` is absent
# because tier 2 owns every one of its columns.
MANGLED = {
    "decks": "name",
    "questions": "prompt",
    "reviews": "result",
    "grading_idempotency": "next_due",
    "offline_sync_idempotency": "status",
    "study_sessions": "state",
    "study_session_answers": "result",
    "trivia_sessions": "queue",
    "trivia_queue": "queue_position",
    "notifications_log": "seen_at",
    "push_subscriptions": "p256dh",
    "byok_credentials": "ciphertext",
    "api_tokens": "token_hash",
}


def first_holder(fleet: dict, table: str) -> tuple[str, dict] | None:
    for (owner, held), rows in fleet.items():
        if held == table and rows and owner not in (DIRECTORY_CELL, LIMITER_CELL):
            return owner, rows[0]
    return None


def mangle(value):
    """A different value of the same shape, so the comparison is of the
    field rather than of a type mismatch."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if value is None:
        return "2026-08-26T14:00:00+00:00"
    return f"{value}-mangled"


def test_a_mangled_non_key_column_is_refused(snapshot, fleet, node_oracle):
    """The whole class a key-against-key tier 1 cannot see. Every field
    here is the thing its row is FOR - the flashcard, the review, the RFC
    8291 encryption key, the encrypted credential - and none of them is
    part of a primary key, so comparing keys against keys reads no data at
    all."""
    tables = copy.deepcopy(fleet)
    expected = set()
    for table, field in MANGLED.items():
        holder = first_holder(tables, table)
        assert holder is not None, f"the fixture carries no {table} row"
        owner, row = holder
        assert field in row, f"{table}.{field} is not a column"
        row[field] = mangle(row[field])
        expected.add((owner, table, field))

    report = run(snapshot, tables, node_oracle)
    found = {(d.user, d.table, d.field) for d in report.divergences if d.tier == 1}
    assert expected <= found, sorted(expected - found)
    assert not report.clean


def test_a_question_moved_to_another_deck_is_refused(snapshot, fleet, node_oracle):
    tables = copy.deepcopy(fleet)
    owner = next(
        u
        for (u, t), rows in tables.items()
        if t == "decks" and len(rows) > 1 and u not in (DIRECTORY_CELL, LIMITER_CELL)
    )
    question = tables[(owner, "questions")][0]
    other = next(d for d in tables[(owner, "decks")] if d["id"] != question["deck_id"])
    question["deck_id"] = other["id"]
    report = run(snapshot, tables, node_oracle, users=[owner])
    assert by_field(report, 1, "questions", "deck_id")


def test_the_fields_tier_two_owns_are_not_reported_twice(snapshot, fleet, node_oracle, plan):
    """Each field belongs to exactly one tier. A `stability` reported at
    both would make "tier 1 and tier 3 clean, tier 2 waived" unreadable,
    and that waiver is the one the rehearsal may force."""
    tables = copy.deepcopy(fleet)
    card = next(c for c in tables[(plan.heavy, "cards")] if c["stability"] is not None)
    card["stability"] = card["stability"] * (1 + 2**-52)
    tables[(plan.retention_high, "decks")][0]["desired_retention"] = 0.71
    report = run(snapshot, tables, node_oracle)
    assert by_field(report, 2, "cards", "stability")
    assert by_field(report, 1, "cards", "stability") == []
    assert by_field(report, 2, "decks", "desired_retention")
    assert by_field(report, 1, "decks", "desired_retention") == []


# ---- the ledger ----------------------------------------------------------


def test_a_ledger_the_import_never_wrote_is_refused(snapshot, fleet, node_oracle):
    """The import skipped, refused, or never scoped to the globals at all.
    Every in-window row is named, rather than both sides being filtered
    down to the empty set the cell happens to hold."""
    tables = copy.deepcopy(fleet)
    kept = tables[(LIMITER_CELL, "instant_generations")]
    assert kept, "the fixture carries a windowed ledger"
    tables[(LIMITER_CELL, "instant_generations")] = []
    report = run(snapshot, tables, node_oracle)
    found = [d for d in report.divergences if d.table == "limiter.instant_generations"]
    assert {d.row for d in found} >= {f"id={r['id']}" for r in kept}
    assert all("the import lost it" in d.note for d in found)


# ---- the directory, both ways --------------------------------------------


def test_an_account_the_snapshot_no_longer_has_is_refused(snapshot, fleet, node_oracle):
    """A user deleted between the two snapshots keeps its directory row and
    its whole cell. Nothing else on the fleet looks for it."""
    tables = copy.deepcopy(fleet)
    tables[(DIRECTORY_CELL, "users")].append(
        {
            "id": "ghost@example.com",
            "is_anonymous": 0,
            "created_at": "2026-03-14T15:00:00+00:00",
            "idx": 9_001,
        }
    )
    report = run(snapshot, tables, node_oracle)
    found = by_field(report, 1, "directory.users", "<row>")
    assert [d.row for d in found] == ["id='ghost@example.com'"]
    assert found[0].snapshot == "absent"


def test_a_scoped_run_leaves_the_rest_of_the_directory_alone(snapshot, fleet, node_oracle, plan):
    """`--users` makes the directory a superset by design."""
    tables = copy.deepcopy(fleet)
    report = run(snapshot, tables, node_oracle, users=[plan.heavy])
    assert by_field(report, 1, "directory.users", "<row>") == []
    assert report.clean


# ---- the fleet names the snapshot it was built from ----------------------


def test_a_fleet_built_from_another_snapshot_aborts(snapshot, fleet, node_oracle):
    """Run against the FIRST snapshot after the second import and every row
    matches, because it is the snapshot the fleet was built from - while the
    whole window's delta is missing. The digest is what refuses it."""
    with pytest.raises(VerificationImpossible, match="was built from snapshot"):
        run(
            snapshot,
            copy.deepcopy(fleet),
            node_oracle,
            run_record={"snapshot": "0" * 64, "openedAt": OPENED_AT},
        )


def test_a_fleet_that_names_no_snapshot_aborts(snapshot, fleet, node_oracle):
    class Nameless(FixtureCellReader):
        def run(self) -> dict | None:
            return None

    conn = open_snapshot(snapshot)
    try:
        verifier = Verifier(
            conn,
            Nameless(copy.deepcopy(fleet)),
            fixed=Fixed(at=AT, generated_at=GENERATED_AT),
            snapshot_sha256=sha256_file(snapshot),
            ts_oracle=node_oracle,
        )
        with pytest.raises(VerificationImpossible, match="does not say which snapshot"):
            verifier.run()
    finally:
        conn.close()
