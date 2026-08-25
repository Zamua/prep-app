"""Pytest fixtures for the pixel flows; `tests/parity/conftest.py`
re-exports them."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.parity.harness import registry, runner
from tests.parity.harness.server import (
    BASE_URL_ENV,
    INTERNAL_TOKEN_ENV,
    LocalParityServer,
    ParityTarget,
)


@pytest.fixture(scope="session")
def parity_llm():
    from tests.parity.llm_stub import FIXTURES_DIR, LLMStub

    with LLMStub(FIXTURES_DIR) as stub:
        yield stub


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
            yield browser
        finally:
            browser.close()


@pytest.fixture(scope="session")
def parity_target(tmp_path_factory, parity_llm) -> Iterator[ParityTarget]:
    remote = (os.environ.get(BASE_URL_ENV) or "").strip()
    if remote:
        yield ParityTarget(remote, token=os.environ.get(INTERNAL_TOKEN_ENV))
        return
    db_path = tmp_path_factory.mktemp("parity") / "data.sqlite"
    server = LocalParityServer(db_path, parity_llm.base_url)
    server.start()
    try:
        yield server
    finally:
        server.stop()


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
