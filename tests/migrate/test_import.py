"""The migrator against a fleet it can drive without one.

What is pinned here is the client: the chunking, the parents-first order,
and the resume that reads the fleet's own counts. The endpoint's keying is
proved against real cells in `worker/tests/migrate.import.test.ts`; this
fleet deduplicates whole rows, which for a replay of the same export is
the same answer `INSERT OR IGNORE` on the row's primary key gives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prep.migrate import importer, layout
from prep.migrate.export import DROPPED_BYOK_PROVIDER


class FleetKilled(RuntimeError):
    """The process stopping between chunks, which is the only way a run
    dies: a chunk is one transaction, so nothing is ever half applied."""


class FakeFleet:
    def __init__(self, *, kill_after: int | None = None) -> None:
        self.users: dict[str, dict] = {}
        self.globals: dict[str, dict[str, set]] = {
            "directory": {"users": set(), "account_merges": set()},
            "limiter": {"instant_generations": set()},
        }
        self.sent: list[dict] = []
        self.kill_after = kill_after

    # -- the endpoint's two routes -----------------------------------------

    def status(self, *, user: str | None = None, cell: str | None = None) -> dict:
        if cell is not None:
            return {"tables": {t: len(rows) for t, rows in self.globals[cell].items()}}
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
            self.globals["directory"]["users"].add(user)
        assert held is not None, f"{user} sent {table} before its profile"
        rows = chunk["rows"]
        dropped = 0
        if table == "byok_credentials":
            kept = [r for r in rows if r.get("provider") != DROPPED_BYOK_PROVIDER]
            dropped = len(rows) - len(kept)
            rows = kept
        inserted = self._insert(held["tables"].setdefault(table, set()) if table else set(), rows)
        return {
            "idx": held["idx"],
            "inserted": {table: inserted} if inserted else {},
            "dropped": dropped,
        }

    def _global(self, chunk: dict) -> dict:
        inserted = self._insert(self.globals[chunk["cell"]][chunk["table"]], chunk["rows"])
        return {"inserted": {chunk["table"]: inserted} if inserted else {}, "dropped": 0}

    @staticmethod
    def _insert(held: set, rows: list[dict]) -> int:
        before = len(held)
        held.update(json.dumps(r, sort_keys=True) for r in rows)
        return len(held) - before

    def _refuse_over_cap(self, chunk: dict) -> None:
        body = json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        assert len(chunk["rows"]) <= importer.MAX_CHUNK_ROWS
        assert len(body) <= importer.MAX_CHUNK_BYTES

    # -- what a test asserts on ---------------------------------------------

    def counts(self, user: str) -> dict[str, int]:
        held = self.users.get(user)
        return {} if held is None else {t: len(rows) for t, rows in held["tables"].items() if rows}

    def snapshot(self) -> dict:
        return {
            "users": {
                u: {t: sorted(rows) for t, rows in h["tables"].items()}
                for u, h in self.users.items()
            },
            "profiles": {u: h["profile"] for u, h in self.users.items()},
            "globals": {
                c: {t: sorted(rows) for t, rows in ts.items()} for c, ts in self.globals.items()
            },
        }


@pytest.fixture
def export_dir(exported: tuple[Path, dict]) -> Path:
    return exported[0]


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


def test_a_second_run_of_the_same_export_inserts_nothing(export_dir: Path) -> None:
    fleet = FakeFleet()
    importer.run(export_dir, fleet)
    after = fleet.snapshot()

    # No resume: every chunk goes over the wire again, which is the shape of
    # a re-run that has lost track of what it did.
    second = importer.run(export_dir, fleet, resume=False)
    assert second.chunks > 0
    assert second.inserted == {}
    assert fleet.snapshot() == after

    # And with the resume, which sends only what the fleet is short of.
    third = importer.run(export_dir, fleet)
    assert third.inserted == {}
    assert third.complete == third.users
    assert fleet.snapshot() == after


def test_a_run_killed_mid_user_resumes_and_converges(export_dir: Path, manifest: dict) -> None:
    clean = FakeFleet()
    importer.run(export_dir, clean, max_rows=5)
    whole = clean.snapshot()

    killed = FakeFleet(kill_after=9)
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
    importer.run(export_dir, killed, max_rows=5)
    assert killed.snapshot() == whole

    # The resume re-sent the short table whole and nothing before it: those
    # chunks insert nothing, and only the rows the kill cost arrive.
    resent = [c for c in killed.sent[9:] if c.get("user") == first["id"]]
    assert [c["table"] for c in resent] == [
        t for t in order[order.index(short) :] for _ in range(_batches(expected[t]))
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
