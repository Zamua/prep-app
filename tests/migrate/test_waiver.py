"""The tier-2 ULP waiver accepts exactly one representable double and no more."""

import struct

from prep.migrate.divergence import Divergence, Report, render, ulp_gap


def _at(value: float, steps: int) -> float:
    """The double `steps` representable values above `value`."""
    bits = struct.unpack(">Q", struct.pack(">d", value))[0]
    return struct.unpack(">d", struct.pack(">Q", bits + steps))[0]


def _tier2(snapshot: float, cell: float, field: str = "stability") -> Divergence:
    return Divergence(
        tier=2,
        table="cards",
        row="question_id=1",
        field=field,
        snapshot=render(snapshot),
        cell=render(cell),
        user="u",
    )


def test_one_ulp_is_waived_and_still_reported():
    v = 238.88495573508374
    report = Report(divergences=[_tier2(v, _at(v, 1))])
    assert report.waive_ulp() == 1
    assert report.clean
    assert len(report.waived) == 1
    assert report.as_json()["waived"][0]["field"] == "stability"
    assert "waived" in report.text()


def test_two_ulps_is_not_waived():
    v = 238.88495573508374
    report = Report(divergences=[_tier2(v, _at(v, 2))])
    assert report.waive_ulp() == 0
    assert not report.clean


def test_a_wholly_wrong_value_is_not_waived():
    report = Report(divergences=[_tier2(238.88495573508374, 0.5)])
    assert report.waive_ulp() == 0
    assert not report.clean


def test_tier_1_and_3_are_never_waived():
    v = 238.88495573508374
    for tier in (1, 3):
        d = _tier2(v, _at(v, 1))
        report = Report(divergences=[Divergence(**{**d.__dict__, "tier": tier})])
        assert report.waive_ulp() == 0
        assert not report.clean


def test_a_non_float_field_is_never_waived():
    report = Report(
        divergences=[
            Divergence(
                tier=2,
                table="cards",
                row="question_id=1",
                field="state",
                snapshot="'started'",
                cell="'done'",
                user="u",
            )
        ]
    )
    assert report.waive_ulp() == 0
    assert not report.clean


def test_adjacency_holds_across_zero():
    assert ulp_gap(render(0.0), render(-0.0)) == 0
    assert ulp_gap(render(_at(0.0, 1)), render(0.0)) == 1
