"""Unit tests for the SDK adapter's error-status mapping.

The `claude-agent-sdk` frequently yields an error `ResultMessage` carrying the
real upstream `api_error_status` (e.g. 401 for an invalid token) and THEN raises
a generic wrapper exception ("...returned an error result: success"). Earlier the
adapter discarded the captured status and surfaced only the opaque wrapper, so a
BYOK user with an invalid/expired key saw a meaningless 502 instead of "your token
is invalid - re-add it". `_raise_for_agent_error` is the pure mapping that fixes
that; it's I/O-free so we test it directly without the SDK installed.
"""

import pytest

from prep.agent.port import AgentBudgetExhausted, AgentUnavailable
from prep.agent.sdk_adapter import _raise_for_agent_error


def test_401_surfaces_status_and_reauth_hint():
    with pytest.raises(AgentUnavailable) as ei:
        _raise_for_agent_error(401, "Failed to authenticate. API Error: 401 Invalid bearer token")
    msg = str(ei.value)
    assert "401" in msg
    assert "re-add" in msg.lower()
    # auth failure is NOT a budget problem
    assert not isinstance(ei.value, AgentBudgetExhausted)


def test_403_treated_as_auth_failure():
    with pytest.raises(AgentUnavailable) as ei:
        _raise_for_agent_error(403, "forbidden")
    assert "403" in str(ei.value)
    assert not isinstance(ei.value, AgentBudgetExhausted)


def test_429_maps_to_budget_exhausted():
    with pytest.raises(AgentBudgetExhausted) as ei:
        _raise_for_agent_error(429, "rate limit")
    assert "429" in str(ei.value)


def test_credit_marker_maps_to_budget_even_without_status():
    with pytest.raises(AgentBudgetExhausted):
        _raise_for_agent_error(None, "Your credit balance is too low to proceed")


def test_other_status_surfaced_generically():
    with pytest.raises(AgentUnavailable) as ei:
        _raise_for_agent_error(503, "upstream overloaded")
    assert "503" in str(ei.value)
    assert not isinstance(ei.value, AgentBudgetExhausted)


def test_no_status_falls_back_to_opaque_wrapper():
    with pytest.raises(AgentUnavailable) as ei:
        _raise_for_agent_error(None, "Claude Code returned an error result: success")
    assert "claude-agent-sdk error" in str(ei.value)
