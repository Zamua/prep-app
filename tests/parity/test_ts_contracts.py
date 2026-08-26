"""The contracts corpus replayed against a running TypeScript server
(docs/PHASE-3.md F.3).

`worker/tests/api/contracts.test.ts` replays the same corpus in node
over a SQLite fake. This one drives the real server over HTTP, which is
the only place a runtime difference shows: the HKDF import that node
accepts and the cell runtime does not was green in node and 500'd on a
node of the fleet.

    PARITY_BASE_URL=http://127.0.0.1:8791 \
    PARITY_INTERNAL_TOKEN=parity-internal-token \
    .venv/bin/pytest tests/parity/test_ts_contracts.py -q

The target's `PREP_FREE_INFERENCE_BASE_URL` has to be the parity LLM
stub, whose origin this test pins the generation answer on: the corpus
was recorded against an in-process fake agent, and the stub's canned
answer is how a server in another process is given the same one.
`PARITY_LLM_STUB_URL` names it, default `http://127.0.0.1:8089`.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.parity.harness.constants import internal_token
from tests.parity.harness.server import BASE_URL_ENV
from tests.parity.llm_stub import StubControl
from tests.parity.oracles import read_corpus
from tests.parity.oracles.contracts import INSTANT_DECK, extract
from tests.parity.oracles.harness import remote_app
from tests.parity.oracles.test_oracles import compare_pairs

# `.apkg` is phase 5, so its two calls are not replayed and the deck they
# would have created is dropped from the list pair that follows them.
PHASE_5 = frozenset({"mcp-call-prep_export_deck_apkg", "mcp-call-prep_import_apkg"})
APKG_DECK = "mcp-restored"
LIST_AFTER = "v1-decks-list-after"


LLM_STUB_ENV = "PARITY_LLM_STUB_URL"
DEFAULT_LLM_STUB = "http://127.0.0.1:8089"


def base_url() -> str:
    url = os.environ.get(BASE_URL_ENV)
    if not url:
        pytest.skip(f"set {BASE_URL_ENV} to a running TypeScript parity server")
    return url.rstrip("/")


def without_phase_5(corpus: dict) -> dict:
    """The corpus as this phase owns it: the two deferred calls removed,
    and the deck the import would have created removed from the list."""
    pairs = []
    for pair in corpus["pairs"]:
        if pair["name"] in PHASE_5:
            continue
        if pair["name"] == LIST_AFTER:
            body = pair["response"]["json"]
            decks = [d for d in body["decks"] if d["name"] != APKG_DECK]
            pair = {**pair, "response": {**pair["response"], "json": {**body, "decks": decks}}}
        pairs.append(pair)
    return {**corpus, "pairs": pairs}


@pytest.fixture(scope="module")
def replayed() -> dict:
    stub = StubControl(os.environ.get(LLM_STUB_ENV) or DEFAULT_LLM_STUB)
    stub.canned(INSTANT_DECK)
    try:
        return json.loads(extract(remote_app(base_url(), internal_token(None)))["pairs.json"])
    finally:
        stub.canned(None)


def test_the_corpus_replays_against_the_server(replayed: dict):
    corpus = without_phase_5(json.loads(read_corpus("contracts")["pairs.json"]))
    candidate = without_phase_5(replayed)
    assert len(candidate["pairs"]) == 128
    diffs = compare_pairs(json.dumps(corpus), json.dumps(candidate))
    assert not diffs, "\n".join(diffs[:20]) + (
        f"\n... {len(diffs) - 20} more" if len(diffs) > 20 else ""
    )


def test_the_two_deferred_calls_answer_a_tool_error(replayed: dict):
    """Phase 5 owns the codecs, not the route: the tool has to exist and
    refuse, or `tools/call` is answering a different shape."""
    by_name = {p["name"]: p for p in replayed["pairs"]}
    for name in PHASE_5:
        pair = by_name[name]
        assert pair["response"]["status"] == 200, name
        assert pair["response"]["json"]["result"]["isError"] is True, name
