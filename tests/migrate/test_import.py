"""The migrator against a fleet it can drive without one.

What is pinned here is the client: the chunking, the parents-first order,
the delta the second pass carries, and the resume that reads the fleet's
own counts. The endpoint's keying is proved against real cells in
`worker/tests/migrate.import.test.ts`; this fleet keys rows by the same
primary key (`layout.KEY_COLUMNS`) and overwrites one that differs, which
is what a cell's upsert does.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from prep.migrate import importer, layout
from prep.migrate.export import DROPPED_BYOK_PROVIDER


class FleetKilled(RuntimeError):
    """The process stopping between chunks, which is the only way a run
    dies: a chunk is one transaction, so nothing is ever half applied."""


# The key each global table is written under, as its cell declares it.
GLOBAL_KEYS = {"users": ("id",), "account_merges": ("id",), "instant_generations": ("id",)}


class FakeFleet:
    """A fleet keyed the way the cells are: one row per primary key, and a
    row that differs overwrites the one already held. The count it reports
    for a write is rows inserted OR changed, which is what makes a second
    pass's total the window's writes."""

    def __init__(self, *, kill_after: int | None = None) -> None:
        self.users: dict[str, dict] = {}
        self.globals: dict[str, dict[str, dict]] = {
            "directory": {"users": {}, "account_merges": {}},
            "limiter": {"instant_generations": {}},
        }
        self.sent: list[dict] = []
        self.run: dict | None = None
        self.kill_after = kill_after

    # -- the endpoint's two routes -----------------------------------------

    def status(self, *, user: str | None = None, cell: str | None = None) -> dict:
        if cell is not None:
            counts = {t: len(rows) for t, rows in self.globals[cell].items()}
            return (
                {"tables": counts, "run": self.run} if cell == "directory" else {"tables": counts}
            )
        held = self.users.get(user or "")
        if held is None:
            return {"profile": False, "idx": 0, "tables": {t: 0 for t in layout.DATA_TABLES}}
        return {
            "profile": True,
            "idx": held["idx"],
            "tables": {t: len(held["tables"].get(t, ())) for t in layout.DATA_TABLES},
        }

    def send(self, chunk: dict) -> dict:
        self._refuse_over_cap(chunk)
        if self.kill_after is not None and len(self.sent) >= self.kill_after:
            raise FleetKilled(f"killed after {len(self.sent)} chunks")
        self.sent.append(chunk)
        if "snapshot" in chunk:
            self.run = {"snapshot": chunk["snapshot"], "openedAt": "2026-08-26T14:05:00+00:00"}
            return {"run": self.run}
        if "cell" in chunk:
            return self._global(chunk)
        return self._user(chunk)

    # -- what a cell does with one ------------------------------------------

    def _user(self, chunk: dict) -> dict:
        user, table = chunk["user"], chunk["table"]
        held = self.users.get(user)
        if chunk.get("profile"):
            assert chunk["profile"]["id"] == user
            held = self.users.setdefault(user, {"idx": chunk["idx"], "tables": {}})
            held["profile"] = chunk["profile"]
            self.globals["directory"]["users"][user] = {"id": user, "idx": chunk["idx"]}
        assert held is not None, f"{user} sent {table} before its profile"
        rows = chunk["rows"]
        dropped = 0
        if table == "byok_credentials":
            kept = [r for r in rows if r.get("provider") != DROPPED_BYOK_PROVIDER]
            dropped = len(rows) - len(kept)
            rows = kept
        written = (
            self._write(held["tables"].setdefault(table, {}), layout.KEY_COLUMNS[table], rows)
            if table
            else 0
        )
        return {
            "idx": held["idx"],
            "inserted": {table: written} if written else {},
            "dropped": dropped,
        }

    def _global(self, chunk: dict) -> dict:
        table = chunk["table"]
        written = self._write(self.globals[chunk["cell"]][table], GLOBAL_KEYS[table], chunk["rows"])
        return {"inserted": {table: written} if written else {}, "dropped": 0}

    @staticmethod
    def _write(held: dict, keys: tuple[str, ...], rows: list[dict]) -> int:
        written = 0
        for row in rows:
            key = tuple(row.get(c) for c in keys)
            if held.get(key) != row:
                held[key] = dict(row)
                written += 1
        return written

    def _refuse_over_cap(self, chunk: dict) -> None:
        body = json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        assert len(chunk.get("rows", ())) <= importer.MAX_CHUNK_ROWS
        assert len(body) <= importer.MAX_CHUNK_BYTES

    # -- what a test asserts on ---------------------------------------------

    def counts(self, user: str) -> dict[str, int]:
        held = self.users.get(user)
        return {} if held is None else {t: len(rows) for t, rows in held["tables"].items() if rows}

    def rows(self, user: str, table: str) -> list[dict]:
        return list(self.users[user]["tables"].get(table, {}).values())

    def snapshot(self) -> dict:
        return {
            "users": {
                u: {t: _sorted(rows) for t, rows in h["tables"].items()}
                for u, h in self.users.items()
            },
            "profiles": {u: h["profile"] for u, h in self.users.items()},
            "globals": {
                c: {t: _sorted(rows) for t, rows in ts.items()} for c, ts in self.globals.items()
            },
        }


def _sorted(rows: dict) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows.values())


@pytest.fixture
def export_dir(exported: tuple[Path, dict]) -> Path:
    return exported[0]


@pytest.fixture
def writable_export(exported: tuple[Path, dict], tmp_path: Path) -> Path:
    """A copy, for a test that edits the export to stand in for the window's
    writes. The session fixture is shared with every other module."""
    out = tmp_path / "export"
    shutil.copytree(exported[0], out)
    return out


@pytest.fixture
def manifest(exported: tuple[Path, dict]) -> dict:
    return exported[1]


def _expected(out: Path, entry: dict) -> dict[str, int]:
    return {
        t: n for t, n in importer.expected_counts(out, entry["id"], entry["counts"]).items() if n
    }


def test_lands_every_row_and_splits_the_globals(export_dir: Path, manifest: dict) -> None:
    fleet = FakeFleet()
    report = importer.run(export_dir, fleet)

    assert report.users == len(manifest["users"])
    for entry in manifest["users"]:
        assert fleet.counts(entry["id"]) == _expected(export_dir, entry), entry["id"]

    # Every user is enumerable, and every row a disposition took out is
    # counted rather than inferred from a gap.
    assert len(fleet.globals["directory"]["users"]) == manifest["globals"]["users"]
    assert (
        len(fleet.globals["directory"]["account_merges"]) == manifest["globals"]["account_merges"]
    )
    assert (
        len(fleet.globals["limiter"]["instant_generations"])
        == manifest["globals"]["instant_generations"]
    )
    assert report.dropped == manifest["signals"]["byok_claude_subscription"]


def test_the_profile_chunk_opens_a_user_and_carries_last_seen_at(
    export_dir: Path, manifest: dict
) -> None:
    fleet = FakeFleet()
    importer.run(export_dir, fleet)
    for entry in manifest["users"]:
        source = layout.read_profile(export_dir, entry["id"])
        assert fleet.users[entry["id"]]["profile"] == source
        assert fleet.users[entry["id"]]["idx"] == entry["idx"]


def test_a_second_run_of_the_same_export_writes_nothing(export_dir: Path) -> None:
    fleet = FakeFleet()
    importer.run(export_dir, fleet)
    after = fleet.snapshot()

    # The default: every chunk goes over the wire again. That is both a
    # re-run that has lost track of what it did and the cutover's second
    # pass, and neither may write a row.
    second = importer.run(export_dir, fleet)
    assert second.chunks > 0
    assert second.inserted == {}
    assert fleet.snapshot() == after

    # And with `--resume`, which sends only what the fleet is short of.
    third = importer.run(export_dir, fleet, resume=True)
    assert third.inserted == {}
    assert third.complete == third.users
    assert fleet.snapshot() == after


def test_a_run_killed_mid_user_resumes_and_converges(export_dir: Path, manifest: dict) -> None:
    clean = FakeFleet()
    importer.run(export_dir, clean, max_rows=5)
    whole = clean.snapshot()

    killed = FakeFleet(kill_after=10)
    with pytest.raises(FleetKilled):
        importer.run(export_dir, killed, max_rows=5)
    assert killed.snapshot() != whole

    # The kill landed inside a table, not on a boundary: the first user is
    # registered, an earlier table is whole, and the short one holds some of
    # its rows. That is the case the resume has to get right.
    first = manifest["users"][0]
    expected = importer.expected_counts(export_dir, first["id"], first["counts"])
    present = killed.status(user=first["id"])
    assert present["profile"] is True
    short = importer.first_short_table(expected, present["tables"])
    assert short is not None
    assert 0 < present["tables"][short] < expected[short]
    order = list(layout.DATA_TABLES)
    for table in order[: order.index(short)]:
        assert present["tables"][table] == expected[table]

    killed.kill_after = None
    sent_before = len(killed.sent)
    importer.run(export_dir, killed, max_rows=5, resume=True)
    assert killed.snapshot() == whole

    # The resume re-sent the short table whole and nothing before it: those
    # chunks write nothing, and only the rows the kill cost arrive. The
    # profile chunk leads regardless, because counts cannot say whether a
    # profile column changed.
    resent = [c for c in killed.sent[sent_before:] if c.get("user") == first["id"]]
    assert [c["table"] for c in resent] == [
        None,
        *(t for t in order[order.index(short) :] for _ in range(_batches(expected[t]))),
    ]

    assert importer.run(export_dir, killed).inserted == {}


def _batches(rows: int, size: int = 5) -> int:
    return -(-rows // size)


def test_chunk_rows_respects_both_caps() -> None:
    rows = [{"id": i, "body": "x" * 100} for i in range(25)]
    assert [len(b) for b in importer.chunk_rows(rows, max_rows=10)] == [10, 10, 5]
    # The byte cap wins when it binds first, and the envelope is charged to
    # the same budget so a profile chunk cannot push a body over.
    by_bytes = list(importer.chunk_rows(rows, max_rows=1000, max_bytes=600, envelope=100))
    assert all(len(json.dumps(b, separators=(",", ":")).encode()) <= 600 for b in by_bytes)
    assert len(by_bytes) > 1
    assert sum(len(b) for b in by_bytes) == len(rows)

    with pytest.raises(importer.ImportRefused):
        list(importer.chunk_rows([{"body": "x" * 500}], max_bytes=200))


def test_the_resume_point_accounts_for_the_dropped_credential(
    export_dir: Path, manifest: dict
) -> None:
    entry = next(u for u in manifest["users"] if u["counts"].get("byok_credentials"))
    expected = importer.expected_counts(export_dir, entry["id"], entry["counts"])
    kept = sum(
        1
        for r in layout.iter_rows(layout.table_path(export_dir, entry["id"], "byok_credentials"))
        if r.get("provider") != DROPPED_BYOK_PROVIDER
    )
    assert expected["byok_credentials"] == kept
    # Counting the dropped row as present would leave every later run
    # restarting at the same table forever.
    assert importer.first_short_table(expected, {**expected}) is None


def test_first_short_table_walks_in_insert_order() -> None:
    full = {t: 2 for t in layout.DATA_TABLES}
    assert importer.first_short_table(full, full) is None
    assert importer.first_short_table(full, {**full, "cards": 1}) == "cards"
    # Parents first: a short parent wins over a short child.
    assert importer.first_short_table(full, {**full, "cards": 1, "decks": 0}) == "decks"


# ---- the second pass carries the window's writes -------------------------


def test_the_second_pass_carries_a_changed_row(writable_export: Path, manifest: dict) -> None:
    """The whole reason the cutover has a second pass. `cards` is never
    inserted after creation and always rewritten - every review moves
    `step`, `next_due`, `stability`, `difficulty` and `fsrs_state` - so an
    import that could only insert would carry a studying user's PRE-window
    schedule onto the fleet, with no re-run able to repair it."""
    fleet = FakeFleet()
    importer.run(writable_export, fleet)

    entry = next(u for u in manifest["users"] if u["counts"].get("cards"))
    user = entry["id"]
    before = importer.run(writable_export, fleet)
    assert before.inserted == {}, "an unchanged export writes nothing"

    # The window: the user studies, so their card's whole FSRS state moves.
    path = layout.table_path(writable_export, user, "cards")
    rows = list(layout.iter_rows(path))
    rows[0] = {
        **rows[0],
        "step": rows[0]["step"] + 5,
        "next_due": "2026-12-01T00:00:00+00:00",
        "stability": 42.5,
        "fsrs_state": 2,
    }
    _rewrite(path, rows)

    delta = importer.run(writable_export, fleet)
    assert delta.inserted == {"cards": 1}, "the count is the window's writes and nothing else"
    landed = next(
        r for r in fleet.rows(user, "cards") if r["question_id"] == rows[0]["question_id"]
    )
    assert landed == rows[0]


def test_the_second_pass_carries_a_profile_edit(writable_export: Path, manifest: dict) -> None:
    """`desired_retention` reshapes every future due date and
    `last_seen_at` is the anonymous reaper's only input, so a profile the
    window changed has to arrive."""
    fleet = FakeFleet()
    importer.run(writable_export, fleet)
    user = manifest["users"][0]["id"]

    profile = dict(layout.read_profile(writable_export, user))
    profile["display_name"] = "renamed during the window"
    profile["last_seen_at"] = "2026-08-26T13:59:00+00:00"
    layout.profile_path(writable_export, user).write_text(
        json.dumps(profile, separators=(",", ":")) + "\n", encoding="utf-8"
    )

    importer.run(writable_export, fleet)
    assert fleet.users[user]["profile"] == profile


def test_a_resumed_run_still_sends_every_profile(export_dir: Path, manifest: dict) -> None:
    """`--resume` skips tables the fleet is not short of. It must not skip
    the profile too: counts say nothing about a column's value."""
    fleet = FakeFleet()
    importer.run(export_dir, fleet)
    sent_before = len(fleet.sent)
    importer.run(export_dir, fleet, resume=True)
    profiles = [c for c in fleet.sent[sent_before:] if c.get("profile")]
    assert len(profiles) == len(manifest["users"])


def test_the_run_names_the_snapshot_before_any_user(export_dir: Path, manifest: dict) -> None:
    fleet = FakeFleet()
    importer.run(export_dir, fleet)
    assert fleet.sent[0] == {"snapshot": manifest["snapshot"]["sha256"]}
    assert fleet.run is not None
    assert fleet.run["snapshot"] == manifest["snapshot"]["sha256"]


def test_the_globals_are_sent_whatever_the_fleet_already_holds(export_dir: Path) -> None:
    """A count-based skip compares totals, not identities: a fleet holding
    a ledger row of its own would reach the expected count and skip the
    real import, leaving the window's rows out with nothing to say so."""
    fleet = FakeFleet()
    fleet.globals["limiter"]["instant_generations"] = {
        (n,): {"id": n, "ip": "203.0.113.1"} for n in range(9000, 9100)
    }
    importer.run(export_dir, fleet)
    ledger = fleet.globals["limiter"]["instant_generations"]
    exported = list(layout.iter_rows(layout.limiter_path(export_dir)))
    assert {(r["id"],) for r in exported} <= set(ledger)


def _rewrite(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8"
    )
