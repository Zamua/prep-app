"""Pixel flow `trivia-generating`, one test per scheme."""

import pytest

from tests.parity.harness.constants import SCHEMES

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_trivia_generating(parity_run, scheme):
    parity_run("trivia-generating", scheme)
