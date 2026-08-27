"""REAL columns survive the export bit for bit (docs/PHASE-6.md A1).

`json.dumps` emits Python's shortest round-tripping repr and `JSON.parse`
reads it back to the same double, so a copied card schedules off the same
number it was scheduled off before.
"""

from __future__ import annotations

import struct
from pathlib import Path

from prep.migrate import layout
from prep.migrate.snapshot import open_snapshot


def _bits(value: float | None) -> bytes | None:
    return None if value is None else struct.pack(">d", value)


def test_every_card_float_round_trips_bit_exactly(snapshot: Path, exported):
    out, manifest = exported
    conn = open_snapshot(snapshot)
    try:
        source = {
            row["question_id"]: (row["stability"], row["difficulty"])
            for row in conn.execute("SELECT question_id, stability, difficulty FROM cards")
        }
    finally:
        conn.close()

    seen = 0
    for user in manifest["users"]:
        for row in layout.iter_rows(layout.table_path(out, user["id"], "cards")):
            stability, difficulty = source[row["question_id"]]
            assert _bits(row["stability"]) == _bits(stability)
            assert _bits(row["difficulty"]) == _bits(difficulty)
            seen += 1
    assert seen == len(source)


def test_the_fixture_carries_floats_that_only_repr_survives(snapshot: Path):
    """Guards the test above: a fixture of round numbers would pass a
    lossy encoder too."""
    conn = open_snapshot(snapshot)
    try:
        stabilities = [
            r[0] for r in conn.execute("SELECT stability FROM cards") if r[0] is not None
        ]
    finally:
        conn.close()
    assert stabilities
    assert any(len(repr(v)) >= 18 for v in stabilities)


def test_retention_reaches_both_clamp_ends(snapshot: Path, exported):
    """`desired_retention` shaped the `next_due` values being copied, so
    the export has to carry the extremes, not just the default."""
    out, manifest = exported
    profiles = [layout.read_profile(out, u["id"]) for u in manifest["users"]]
    user_retentions = {p["desired_retention"] for p in profiles}
    deck_retentions = {
        row["desired_retention"]
        for u in manifest["users"]
        for row in layout.iter_rows(layout.table_path(out, u["id"], "decks"))
    }
    assert {0.70, 0.97} <= user_retentions
    assert {0.70, 0.97} <= deck_retentions
