"""Unit tests for the URL-encoded mini-session state helpers.

These were route-private until phase 4; surfaced into their own module
so they can be tested in isolation rather than only through the HTTP
flow."""

from __future__ import annotations

from prep.trivia.session_state import (
    flip_done_verdict,
    format_done,
    parse_card_ids,
    parse_done,
)


def test_parse_card_ids_handles_empty_and_garbage():
    assert parse_card_ids(None) == []
    assert parse_card_ids("") == []
    assert parse_card_ids("1,2,3") == [1, 2, 3]
    # Whitespace-tolerant + drops non-digits.
    assert parse_card_ids(" 1 , foo, 3, , 5 ") == [1, 3, 5]


def test_parse_done_round_trip():
    items = [(42, "r"), (17, "w"), (99, "r")]
    encoded = format_done(items)
    assert encoded == "42r,17w,99r"
    assert parse_done(encoded) == items


def test_parse_done_drops_malformed_chunks():
    # Hand-edited URLs shouldn't crash the route — drop the bad bits,
    # keep the rest.
    assert parse_done("42r,foo,17w,99x,99r") == [(42, "r"), (17, "w"), (99, "r")]


def test_format_done_empty():
    assert format_done([]) == ""


def test_flip_done_verdict_flips_only_target_qid():
    """Re-grade flips the verdict for one specific qid; siblings stay."""
    items = [(42, "w"), (17, "r"), (99, "w")]
    flipped = flip_done_verdict(items, qid=42, correct=True)
    assert parse_done(flipped) == [(42, "r"), (17, "r"), (99, "w")]


def test_flip_done_verdict_noop_when_qid_absent():
    items = [(42, "w"), (17, "r")]
    flipped = flip_done_verdict(items, qid=999, correct=True)
    assert parse_done(flipped) == items
