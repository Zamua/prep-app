"""Pytest fixtures for the e2e suite.

The e2e suite drives a deployed prep instance over HTTP — staging by
default, overridable via E2E_BASE_URL. Each test session creates a
throwaway deck (`e2e-test-deck`) via the app's normal HTTP routes,
runs assertions, then deletes it via the same routes — so the
fixture itself exercises create + delete + cascade. Failures don't
leak the deck into staging because teardown runs in a `yield`-style
fixture's `finally`.

Run from the repo root:
    .venv/bin/pytest tests/e2e -q
or
    make e2e
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest

# Default points at staging behind Tailscale serve. Override locally with
# `E2E_BASE_URL=http://localhost:8082` (or another env) at invocation
# time. The trailing slash is normalized off so callers can `+ "/path"`.
DEFAULT_BASE_URL = "https://macmini.trout-chimera.ts.net/prep-staging"

E2E_DECK_NAME = "e2e-test-deck"

# Canonical questions seeded into the throwaway deck. The first three
# are short single-token answers that grade through the deterministic
# path (no claude). The "claude" question has an answer long enough
# that classify_grading routes it to claude_grade — used by the
# claude-grading + regrade e2e cases.
E2E_QUESTIONS = [
    {"prompt": "Capital of France?", "answer": "Paris"},
    {"prompt": "Capital of Japan?", "answer": "Tokyo"},
    {"prompt": "Capital of Egypt?", "answer": "Cairo"},
    {
        # Long-enough answer (>3 tokens, with sentence punctuation)
        # forces claude_grade per prep.trivia.service.classify_grading.
        "prompt": "Briefly: what is the role of the GIL in CPython?",
        "answer": "It serializes Python bytecode execution so only one thread runs at a time.",
    },
]


def _base_url() -> str:
    return os.environ.get("E2E_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


@pytest.fixture(scope="session")
def base_url() -> str:
    """Where the e2e suite points its HTTP + browser clients. The trailing
    slash is normalized off so tests can `f"{base_url}/path"`."""
    return _base_url()


@pytest.fixture(scope="session")
def http(base_url: str) -> Iterator[httpx.Client]:
    """Synchronous HTTP client for setup/teardown. Tailscale serve on
    the same machine auto-injects identity headers, so no auth setup
    needed when running on the mac mini host. Outside that environment
    set E2E_TAILSCALE_LOGIN to spoof for local fastapi dev."""
    headers = {}
    spoof = os.environ.get("E2E_TAILSCALE_LOGIN")
    if spoof:
        headers["Tailscale-User-Login"] = spoof
    with httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=30.0,
        follow_redirects=False,
        verify=True,
    ) as c:
        yield c


@pytest.fixture(scope="session")
def test_deck(http: httpx.Client) -> Iterator[dict]:
    """Create the e2e-test-deck via the SRS deck-creation route, seed
    `E2E_QUESTIONS` via the manual question-add route, yield a dict of
    deck metadata to the tests, and delete the deck on teardown.

    Idempotent on entry: if a prior run left the deck behind (e.g.
    interrupted teardown), we delete it first.
    """
    # Pre-clean: if a prior failed run left the deck, drop it first
    # so create succeeds. Delete is idempotent.
    _delete_test_deck(http)

    # Create — SRS path with no AI agent involvement, fastest path.
    r = http.post(
        "/decks/new/srs",
        data={
            "name": E2E_DECK_NAME,
            "context_prompt": "e2e test deck — created + torn down per run",
            "action": "empty",  # no claude generation
        },
    )
    assert r.status_code in (200, 303), f"deck create returned {r.status_code}: {r.text[:300]}"

    # Seed questions via the manual add route.
    qids: list[int] = []
    for q in E2E_QUESTIONS:
        r = http.post(
            f"/deck/{E2E_DECK_NAME}/question/new",
            data={
                "prompt": q["prompt"],
                "answer": q["answer"],
                "type": "short",
            },
        )
        assert r.status_code in (
            200,
            303,
        ), f"seed question {q['prompt']!r}: {r.status_code} {r.text[:200]}"

    # Pull the question ids back. The deck page exposes them via
    # data-qid attributes on each .qcard.
    r = http.get(f"/deck/{E2E_DECK_NAME}")
    assert r.status_code == 200, f"deck page: {r.status_code}"
    import re

    for m in re.finditer(r'data-qid="(\d+)"', r.text):
        qid = int(m.group(1))
        if qid not in qids:
            qids.append(qid)
    assert len(qids) >= len(
        E2E_QUESTIONS
    ), f"expected {len(E2E_QUESTIONS)} qids on deck page, got {qids}"

    info = {"name": E2E_DECK_NAME, "qids": qids[: len(E2E_QUESTIONS)]}
    try:
        yield info
    finally:
        _delete_test_deck(http)


def _delete_test_deck(http: httpx.Client) -> None:
    """Best-effort delete. The app requires the deck name in the
    `confirm` field as a typo guard; we always pass it. 303 = success,
    404 = already gone (also fine), other = the test reports it."""
    r = http.post(
        f"/deck/{E2E_DECK_NAME}/delete",
        data={"confirm": E2E_DECK_NAME},
    )
    if r.status_code not in (200, 303, 404):
        # Surface non-fatally — we don't want a teardown failure to mask
        # a real test failure earlier in the session.
        print(f"[e2e teardown] deck delete returned {r.status_code}: {r.text[:200]}")
