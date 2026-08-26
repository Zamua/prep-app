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

LLM_STUB_ENV = "PARITY_LLM_STUB_URL"
DEFAULT_LLM_STUB = "http://127.0.0.1:8089"


def base_url() -> str:
    url = os.environ.get(BASE_URL_ENV)
    if not url:
        pytest.skip(f"set {BASE_URL_ENV} to a running TypeScript parity server")
    return url.rstrip("/")


@pytest.fixture(scope="module")
def replayed() -> dict:
    stub = StubControl(os.environ.get(LLM_STUB_ENV) or DEFAULT_LLM_STUB)
    stub.canned(INSTANT_DECK)
    try:
        return json.loads(extract(remote_app(base_url(), internal_token(None)))["pairs.json"])
    finally:
        stub.canned(None)


def test_the_corpus_replays_against_the_server(replayed: dict):
    corpus = json.loads(read_corpus("contracts")["pairs.json"])
    candidate = replayed
    assert len(candidate["pairs"]) == 130
    diffs = compare_pairs(json.dumps(corpus), json.dumps(candidate))
    assert not diffs, "\n".join(diffs[:20]) + (
        f"\n... {len(diffs) - 20} more" if len(diffs) > 20 else ""
    )


def test_the_apkg_tools_round_trip(replayed: dict):
    """The export hands its base64 to the import in the same replay, so a
    codec that writes a package it cannot read fails here and nowhere else."""
    by_name = {p["name"]: p for p in replayed["pairs"]}
    exported = by_name["mcp-call-prep_export_deck_apkg"]["response"]["json"]["result"]
    assert exported["isError"] is False
    assert json.loads(exported["content"][0]["text"])["filename"] == "mcp-renamed.apkg"
    imported = by_name["mcp-call-prep_import_apkg"]["response"]["json"]["result"]
    assert imported["isError"] is False
    assert json.loads(imported["content"][0]["text"])["inserted"] == 4
