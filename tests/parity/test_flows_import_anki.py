"""Pixel flow `import-anki`, one test per scheme."""

import pytest

from tests.parity.harness.constants import SCHEMES

pytestmark = [pytest.mark.browser]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_import_anki(parity_run, scheme):
    parity_run("import-anki", scheme)
