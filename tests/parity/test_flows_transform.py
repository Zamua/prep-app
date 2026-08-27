"""Pixel flow `transform`, one test per scheme."""

import pytest

from tests.parity.harness.constants import SCHEMES

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_transform(parity_run, scheme):
    parity_run("transform", scheme)
