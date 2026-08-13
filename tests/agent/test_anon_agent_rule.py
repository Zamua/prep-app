"""Anonymous accounts reach the free tier through the instant
endpoint and nowhere else.

The rule is a `users` read, never a test on the shape of the id: a
non-anonymous account whose external id happens to start with `anon:`
keeps the free tier.
"""

from __future__ import annotations

import pytest

from prep.agent import selector
from prep.agent.selector import _NoopAgent, agent_for_user, funding_tier_for_user
from prep.auth.repo import UserRepo
from prep.infrastructure.db import cursor

ANON_ID = "anon:" + "ab" * 16
LOOKALIKE_ID = "anon:" + "cd" * 16


@pytest.fixture
def free_tier(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PREP_FREE_INFERENCE_BASE_URL", "https://inference.example/v1")
    monkeypatch.setenv("PREP_FREE_INFERENCE_API_KEY", "free-key")
    monkeypatch.setenv("PREP_FREE_INFERENCE_MODEL", "some-model")


def seed_anon(external_id: str = ANON_ID) -> str:
    with cursor() as c:
        c.execute(
            """INSERT INTO users (tailscale_login, display_name, email, created_at,
                                  last_seen_at, is_anonymous)
               VALUES (?, 'Guest', NULL, '2000-01-01', '2000-01-01', 1)""",
            (external_id,),
        )
    return external_id


def test_the_free_tier_serves_a_named_user(initialized_db: str, free_tier):
    """The fixture configures a tier that would otherwise serve; the
    refusals below are the anonymity rule, not an unconfigured
    deploy."""
    assert not isinstance(agent_for_user(initialized_db), _NoopAgent)
    assert funding_tier_for_user(initialized_db) == "free"


def test_an_anonymous_account_gets_the_noop_adapter(initialized_db: str, free_tier):
    uid = seed_anon()
    assert isinstance(agent_for_user(uid), _NoopAgent)


def test_an_anonymous_account_is_funded_by_no_tier(initialized_db: str, free_tier):
    uid = seed_anon()
    assert funding_tier_for_user(uid) == "none"


def test_the_rule_does_not_read_the_id_prefix(initialized_db: str, free_tier):
    """A named account whose id looks anonymous still gets the tier."""
    UserRepo().upsert(LOOKALIKE_ID, display_name="Looks anonymous")
    assert not isinstance(agent_for_user(LOOKALIKE_ID), _NoopAgent)
    assert funding_tier_for_user(LOOKALIKE_ID) == "free"


def test_a_workflow_start_is_refused_for_an_anonymous_account(initialized_db: str, free_tier):
    from prep.agent.port import AgentUnavailable

    uid = seed_anon()
    with pytest.raises(AgentUnavailable):
        selector.require_funded_workflow(uid)
    # Same guard, funded user: no refusal.
    selector.require_funded_workflow(initialized_db)


def test_agent_context_answers_from_the_row_without_the_selector(initialized_db: str, monkeypatch):
    """The context processor runs on every render, so it reads the
    flag off the resolved user rather than paying a query."""
    from prep.web import templates as templates_mod

    def explode(_uid):
        raise AssertionError("selector must not be called for an anonymous user")

    monkeypatch.setattr("prep.agent.selector.agent_available_for_user", explode, raising=True)
    request = _request_with_user({"tailscale_login": ANON_ID, "is_anonymous": 1})
    assert templates_mod._agent_context(request) == {"agent_available": False}


def _request_with_user(user: dict):
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "headers": [],
            "state": {},
        }
    )
    request.state.user = user
    return request
