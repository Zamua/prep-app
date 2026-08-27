"""Pixel flow `import-csv`, one test per scheme."""

import pytest

from tests.parity.harness.constants import SCHEMES

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_import_csv(parity_run, scheme):
    parity_run("import-csv", scheme)
