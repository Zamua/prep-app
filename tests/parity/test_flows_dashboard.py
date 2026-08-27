"""Pixel flow `dashboard`, one test per scheme."""

import pytest

from tests.parity.harness.constants import SCHEMES

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_dashboard(parity_run, scheme):
    parity_run("dashboard", scheme)
