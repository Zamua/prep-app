"""Pytest fixtures for the pixel flows; `tests/parity/conftest.py`
re-exports them."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.parity.harness import browser as browser_pin
from tests.parity.harness import registry, runner
from tests.parity.harness.constants import INTERNAL_TOKEN_ENV
from tests.parity.harness.server import BASE_URL_ENV, ParityTarget

LLM_STUB_URL_ENV = "PARITY_LLM_STUB_URL"
DEFAULT_LLM_STUB = "http://127.0.0.1:8089"


@pytest.fixture(scope="session")
def parity_llm():
    """The stub the target under test answers from.

    The target was pointed at its stub when it was deployed, so the knobs
    a flow turns have to reach that process; an in-process stub would
    leave the server calling the other one. `PARITY_LLM_STUB_URL` names
    it, default the port `worker/scripts/run-node.sh` documents.
    """
    from tests.parity.llm_stub import RemoteStub

    origin = (os.environ.get(LLM_STUB_URL_ENV) or "").strip()
    yield RemoteStub(origin or DEFAULT_LLM_STUB)


@pytest.fixture(scope="session")
def parity_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        pytest.skip(f"playwright not installed: {e}")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"chromium launch failed: {e}")
        try:
            label = browser_pin.browser_label(browser)
            if runner.mode() == "golden":
                browser_pin.write_pin(label)
            reason = browser_pin.check_pin(browser_pin.read_pin(), label, runner.mode())
            if reason:
                pytest.fail(reason)
            yield browser
        finally:
            browser.close()


@pytest.fixture(scope="session")
def parity_target(parity_llm) -> Iterator[ParityTarget]:
    remote = (os.environ.get(BASE_URL_ENV) or "").strip()
    if not remote:
        pytest.skip(f"set {BASE_URL_ENV} to a running parity target")
    yield ParityTarget(remote, token=os.environ.get(INTERNAL_TOKEN_ENV))


@pytest.fixture
def parity_run(request):
    """`parity_run(flow_name, scheme)`: skips per the selection env,
    else runs the flow and fails with every shot's verdict.

    The browser and target come through `getfixturevalue` AFTER the
    skip decision: Playwright's sync API keeps an event loop running
    for its whole session scope, which would break every later
    pytest-asyncio test in the same invocation. Pixel flows still run
    in their own invocation, like tests/e2e."""

    def _run(name: str, scheme: str) -> runner.RunResult:
        flow = registry.get_flow(name)
        reason = registry.flow_selected(flow) or registry.scheme_selected(flow, scheme)
        if reason:
            pytest.skip(reason)
        browser = request.getfixturevalue("parity_browser")
        target = request.getfixturevalue("parity_target")
        llm = request.getfixturevalue("parity_llm")
        result = runner.run_flow(
            flow,
            scheme,
            browser=browser,
            target=target,
            llm=llm,
            record_property=lambda k, v: request.node.user_properties.append((k, v)),
        )
        for p in result.shots:
            request.node.user_properties.append(("shot", str(p)))
        assert result.passed, f"{name}@{scheme} ({result.mode}):\n  " + "\n  ".join(result.failures)
        return result

    return _run
