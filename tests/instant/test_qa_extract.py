"""Foundation pin for the shared parser at prep/domain/qa_extract.py.

Trivia generation and instant generation must keep consuming the SAME
parse function; the behavior pins cover the tolerant-parse contract
both depend on.
"""

from __future__ import annotations

import pytest

from prep.domain.qa_extract import parse_qa_pairs
from prep.trivia import service as trivia_service

ITEMS = '[{"q": "Q?", "a": "A", "r": null}]'


def test_trivia_binds_the_shared_parser():
    assert trivia_service._parse_qa_pairs is parse_qa_pairs


def test_plain_array_parses():
    assert parse_qa_pairs(ITEMS) == [{"q": "Q?", "a": "A", "r": None}]


def test_code_fences_are_stripped():
    assert parse_qa_pairs(f"```json\n{ITEMS}\n```") == [{"q": "Q?", "a": "A", "r": None}]
    assert parse_qa_pairs(f"```\n{ITEMS}\n```") == [{"q": "Q?", "a": "A", "r": None}]


def test_leading_note_is_tolerated():
    assert parse_qa_pairs(f"Here are the cards:\n{ITEMS}") == [{"q": "Q?", "a": "A", "r": None}]


def test_no_array_raises_value_error():
    with pytest.raises(ValueError):
        parse_qa_pairs("no json here")


def test_malformed_bracket_chunk_raises_value_error():
    with pytest.raises(ValueError):
        parse_qa_pairs('[{"q": broken]')
