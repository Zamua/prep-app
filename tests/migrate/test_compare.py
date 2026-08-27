"""The comparison rules, in isolation. No snapshot, no fleet."""

from __future__ import annotations

from prep.migrate.compare import compare_count, compare_rows, normalise, same
from prep.migrate.divergence import Divergence, Report, float_bits, render, row_key


def test_two_doubles_that_print_alike_still_differ():
    a = 0.1 + 0.2
    b = 0.30000000000000004
    assert a == b
    nudged = b * (1 + 2**-52)
    assert repr(b)[:16] == repr(nudged)[:16]
    assert not same(b, nudged, real=True)
    assert float_bits(b) != float_bits(nudged)


def test_minus_zero_is_not_zero():
    assert not same(0.0, -0.0, real=True)


def test_json_widens_an_integral_double_and_nothing_else():
    # `JSON.stringify(30.0)` writes `30`; the transport cannot carry the
    # distinction, so a REAL column widens it back.
    assert same(30.0, 30, real=True)
    assert not same(30.0, 30, real=False)
    assert normalise(True, real=False) == 1


def test_a_string_never_equals_its_number():
    assert not same(7, "7", real=False)
    assert not same(None, "", real=False)
    assert same(None, None, real=True)


def test_a_missing_row_is_reported_by_its_key():
    found = compare_rows(
        tier=1,
        table="reviews",
        key_columns=("id",),
        snapshot_rows=[{"id": 4, "ts": "t"}],
        cell_rows=[],
        user="a@example.com",
    )
    assert len(found) == 1
    assert found[0].row == "id=4"
    assert found[0].cell == "absent"
    assert "user=a@example.com" in found[0].line()


def test_a_composite_key_prints_every_part():
    assert row_key(("session_id", "question_id"), {"session_id": "s1", "question_id": 4}) == (
        "session_id='s1',question_id=4"
    )


def test_a_report_that_examined_nothing_is_still_clean_but_says_so():
    report = Report()
    assert report.clean
    assert "clean: 0 users" in report.text()


def test_a_count_check_says_which_table_and_who():
    found = compare_count(
        tier=1, table="active_workflows", expected=0, actual=3, user="a@example.com"
    )
    assert found[0].field == "<count>"
    assert found[0].table == "active_workflows"
    assert found[0].as_json()["user"] == "a@example.com"


def test_a_float_renders_with_its_bit_pattern():
    assert render(1.5) == "1.5 (bits 3ff8000000000000)"
    assert render(None) == "NULL"


def test_a_divergence_line_names_all_four_places():
    line = Divergence(
        tier=2,
        user="a@example.com",
        table="cards",
        row="question_id=41",
        field="stability",
        snapshot="1.0",
        cell="1.1",
    ).line()
    assert "user=a@example.com" in line
    assert "table=cards" in line
    assert "row=question_id=41" in line
    assert "field=stability" in line
