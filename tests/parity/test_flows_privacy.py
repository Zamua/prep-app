"""Pixel flow `privacy`, one test per scheme."""

import pytest

from tests.parity.harness.constants import SCHEMES

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_privacy(parity_run, scheme):
    parity_run("privacy", scheme)
