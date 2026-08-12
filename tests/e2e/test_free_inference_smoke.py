"""Real-API smoke for the deploy-configured free inference tier.

The only tests that touch the real endpoint (docs/AI-PROVIDERS.md
section 6). Configuration comes from the PREP_FREE_INFERENCE_* env
vars ONLY - the whole module skips unless they are set, so this never
runs in CI without the operator-provided key. Never read operator key
files here.

Run against a configured environment:

    PREP_FREE_INFERENCE_BASE_URL=... \
    PREP_FREE_INFERENCE_API_KEY=... \
    PREP_FREE_INFERENCE_MODEL=... \
    pytest tests/e2e/test_free_inference_smoke.py -q
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

_KEY = (os.environ.get("PREP_FREE_INFERENCE_API_KEY") or "").strip()
_BASE = (os.environ.get("PREP_FREE_INFERENCE_BASE_URL") or "").strip().rstrip("/")
_MODEL = (os.environ.get("PREP_FREE_INFERENCE_MODEL") or "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (_KEY and _BASE and _MODEL),
        reason="free-tier env (PREP_FREE_INFERENCE_*) not configured",
    ),
]


def test_models_endpoint_lists_configured_default():
    """The configured model must actually exist upstream - a typo'd
    PREP_FREE_INFERENCE_MODEL would otherwise only surface as per-call
    errors after deploy."""
    resp = httpx.get(
        f"{_BASE}/models",
        headers={"Authorization": f"Bearer {_KEY}"},
        timeout=30.0,
    )
    assert resp.status_code == 200, resp.text[:300]
    ids = {m.get("id") for m in resp.json().get("data", [])}
    assert _MODEL in ids, f"{_MODEL!r} not in {sorted(i for i in ids if i)}"


def test_strict_json_grading_call_parses():
    """One grading-shaped strict-JSON call through the REAL free-tier
    adapter (built by the factory, so extra_body / max_tokens / shared
    mode match the deploy), asserting the output survives the real
    grading parser."""
    from prep.agent.selector import free_tier_agent
    from prep.trivia.service import _AI_GRADE_PROMPT, _parse_grade_json

    agent = free_tier_agent()
    assert agent is not None, "factory returned None despite env being set"

    prompt = _AI_GRADE_PROMPT % {
        "prompt": "What does WAL stand for in database systems?",
        "expected": "write-ahead log",
        "given": "wal",
        "current_regex": "null",
    }
    result = asyncio.run(agent.run(prompt, timeout_s=60.0))
    parsed = _parse_grade_json(result.text)
    assert parsed.get("verdict") in {"right", "wrong"}, parsed
    assert isinstance(parsed.get("feedback"), str)


def _factory_adapter():
    from prep.agent.selector import free_tier_agent

    agent = free_tier_agent()
    assert agent is not None, "factory returned None despite env being set"
    return agent


def test_trivia_batch_shape_survives_parser():
    """A trivia-shaped generation (the bounded high-volume flow) comes
    back parseable by the real parser at the real output size."""
    from prep.trivia.service import _parse_qa_pairs

    agent = _factory_adapter()
    prompt = (
        "Generate exactly 25 trivia flashcards about world geography. "
        'Reply with ONLY a JSON array of objects: [{"prompt": "...", '
        '"answer": "..."}]. Answers must be one to four words.'
    )
    result = asyncio.run(agent.run(prompt, timeout_s=120.0))
    pairs = _parse_qa_pairs(result.text)
    assert len(pairs) >= 20, f"expected a near-full batch, got {len(pairs)}"


def test_transform_shaped_output_fits_configured_cap():
    """The cap-sizing probe docs/AI-PROVIDERS.md section 6 requires:
    a transform-shaped output at large-deck scale must come back
    complete (finish_reason stop, not length) under the configured
    free-tier max_tokens. If the endpoint clamps or 400s on our cap,
    every large transform would fail in production; this catches it
    before deploy."""
    from prep.agent.selector import _FREE_TIER_MAX_TOKENS

    agent = _factory_adapter()
    prompt = (
        "Here are 40 flashcard fronts, one per line:\n"
        + "\n".join(f"Q{i}: What is concept number {i} in distributed systems?" for i in range(40))
        + "\n\nRewrite ALL 40 cards to be more specific. Reply with ONLY a "
        'JSON array of 40 objects: [{"old_prompt": "...", "new_prompt": '
        '"...", "new_answer": "..."}]. Each new_answer should be two to '
        "three sentences."
    )
    result = asyncio.run(agent.run(prompt, timeout_s=240.0))
    import json as _json

    text = result.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("[") :]
    start, end = text.find("["), text.rfind("]")
    assert start != -1 and end > start, f"no JSON array in output head: {text[:200]}"
    items = _json.loads(text[start : end + 1])
    assert len(items) >= 35, (
        f"expected a complete 40-item transform under the "
        f"{_FREE_TIER_MAX_TOKENS}-token cap, got {len(items)} items"
    )
