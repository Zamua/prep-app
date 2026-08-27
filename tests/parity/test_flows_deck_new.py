"""Pixel flow `deck-new`, one test per scheme."""

import pytest

from tests.parity.harness.constants import SCHEMES

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_deck_new(parity_run, scheme):
    parity_run("deck-new", scheme)
