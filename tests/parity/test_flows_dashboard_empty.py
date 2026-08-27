"""Pixel flow `dashboard-empty`, one test per scheme."""

import pytest

from tests.parity.harness.constants import SCHEMES

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_dashboard_empty(parity_run, scheme):
    parity_run("dashboard-empty", scheme)
