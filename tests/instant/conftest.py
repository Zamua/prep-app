"""Fixtures for the instant bounded-context suite.

The agent-factory seam in prep.instant.service is module-level state
that survives across tests; every override installed here is
restored on teardown so tests stay order-independent.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from prep.instant import service


@pytest.fixture
def instant_factory() -> Callable:
    """Installer for the instant agent-factory seam. Call with a
    factory (invoked as factory(max_output_tokens=<int>)); teardown
    restores the default free-tier path."""

    def install(factory):
        service.set_instant_agent_factory(factory)
        return factory

    yield install
    service.set_instant_agent_factory(None)


@pytest.fixture(autouse=True)
def _default_ip_header(monkeypatch: pytest.MonkeyPatch):
    """Pin the header mode to the default unless a test sets it."""
    monkeypatch.delenv("PREP_CLIENT_IP_HEADER", raising=False)


@pytest.fixture
def deck_json() -> Callable[..., str]:
    """Well-formed model output: n q/a/r items whose regexes match
    their answers."""

    def build(n: int = 5) -> str:
        return json.dumps(
            [{"q": f"Question {i}?", "a": f"answer {i}", "r": f"answer {i}"} for i in range(n)]
        )

    return build
