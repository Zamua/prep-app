"""The golden browser pin: what proceeds, what refuses, what rewrites."""

from __future__ import annotations

from tests.parity.harness import browser


def test_compare_on_the_pinned_browser_proceeds():
    assert browser.check_pin("chromium 140.0.0.0", "chromium 140.0.0.0", "compare") is None


def test_compare_on_another_browser_refuses_and_names_both():
    reason = browser.check_pin("chromium 140.0.0.0", "chromium 141.0.0.0", "compare")
    assert reason is not None
    assert "chromium 140.0.0.0" in reason and "chromium 141.0.0.0" in reason


def test_golden_mode_and_an_unpinned_set_proceed():
    assert browser.check_pin("chromium 140.0.0.0", "chromium 141.0.0.0", "golden") is None
    assert browser.check_pin(None, "chromium 141.0.0.0", "compare") is None


def test_pin_round_trips(tmp_path):
    path = tmp_path / "goldens" / "browser.txt"
    assert browser.read_pin(path) is None
    browser.write_pin("chromium 140.0.0.0", path)
    assert browser.read_pin(path) == "chromium 140.0.0.0"
