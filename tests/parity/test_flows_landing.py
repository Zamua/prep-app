"""Pixel flow `landing` (docs/PARITY-GATE.md C5), one test per scheme."""

import pytest

from tests.parity.harness.constants import SCHEMES

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_landing(parity_run, scheme):
    parity_run("landing", scheme)
