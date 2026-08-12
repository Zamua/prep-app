"""start_plan_generation threads the free-tier card cap: max_cards=5
rides the workflow start for a free-funded user; BYOK users pass no
cap and the model picks the plan size."""

from __future__ import annotations

from typing import Any

import pytest

from prep.decks import service as svc


class FakeTemporalClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.start_plan_returns: Any = type("WfHandle", (), {"workflow_id": "wf-cap"})()

    async def start_plan_generate(self, **kwargs):
        self.calls.append(("start_plan_generate", kwargs))
        return self.start_plan_returns


@pytest.fixture
def free_tier_env(monkeypatch):
    monkeypatch.setenv("PREP_FREE_INFERENCE_BASE_URL", "https://inference.example/v1")
    monkeypatch.setenv("PREP_FREE_INFERENCE_API_KEY", "free-tier-key")
    monkeypatch.setenv("PREP_FREE_INFERENCE_MODEL", "test-free-model")
    monkeypatch.delenv("PREP_FREE_INFERENCE_EXTRA_BODY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("PREP_AUTH_MODE", raising=False)


async def test_free_tier_user_plan_capped_at_five(initialized_db, free_tier_env):
    client = FakeTemporalClient()
    await svc.start_plan_generation(
        client,
        user_id=initialized_db,
        deck_id=7,
        deck_name="capped",
        prompt="a broad survey topic",
    )
    _, kwargs = client.calls[0]
    assert kwargs["max_cards"] == 5


async def test_byok_user_plan_uncapped(initialized_db, free_tier_env):
    # Row existence routes the tier to "byok"; nothing decrypts here.
    from prep.infrastructure.db import cursor

    with cursor() as c:
        c.execute(
            "INSERT INTO byok_credentials"
            " (user_id, provider, ciphertext, key_prefix, created_at)"
            " VALUES (?, 'anthropic-api', 'not-real-ciphertext', 'sk-ant-', '2020-01-01T00:00:00Z')",
            (initialized_db,),
        )
    client = FakeTemporalClient()
    await svc.start_plan_generation(
        client,
        user_id=initialized_db,
        deck_id=7,
        deck_name="uncapped",
        prompt="a broad survey topic",
    )
    _, kwargs = client.calls[0]
    assert "max_cards" not in kwargs
