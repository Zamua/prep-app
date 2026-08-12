"""Purge regression guard: user-facing surfaces stay provider-neutral.

The adapter layer (and the provider registry / MCP-client docs /
chat-handoff links) may name vendors; everything the USER sees for
prep's own AI features must not. Pins the M1 neutral wording from
docs/AI-PROVIDERS.md section 1:

- the trivia grading fallback feedback strings
- the workflow status display labels
- the Go worker's string literals that feed progress/error fields
  (templates render `progress.Error` verbatim, so a template-only
  check would miss them)

Deliberately narrow: does NOT grep the whole tree - the KEEP list
(byok registry, subscription-token machinery, claude.ai handoff,
MCP docs) is legal.
"""

from __future__ import annotations

from pathlib import Path

import prep.trivia.service as svc
from prep.agent.port import AgentBusy, AgentUnavailable
from prep.workflows import entities as wf_entities

REPO_ROOT = Path(__file__).resolve().parent.parent

# Vendor tokens that must not appear on the user-facing surfaces below.
_VENDOR_TOKENS = ("claude", "anthropic")


def _assert_neutral(text: str, context: str = "") -> None:
    lowered = text.lower()
    for token in _VENDOR_TOKENS:
        assert token not in lowered, f"vendor name {token!r} in {context or text!r}"


# ---- workflow status display -------------------------------------------


def test_workflow_status_display_is_neutral():
    statuses = (
        wf_entities.ACTION_REQUIRED_STATUSES
        | wf_entities.TERMINAL_STATUSES
        | wf_entities.IN_PROGRESS_STATUSES
    )
    for status in statuses:
        w = wf_entities.ActiveWorkflow(
            workflow_id="wf-1",
            user_login="u@example.com",
            workflow_type=wf_entities.WorkflowType.TRIVIA_GEN,
            status=status,
            started_at="2026-01-01T00:00:00Z",
            url_path="/trivia/gen/wf-1",
        )
        _assert_neutral(status, f"workflow status {status!r}")
        _assert_neutral(w.display_status, f"display_status for {status!r}")


# ---- grading fallback feedback -----------------------------------------


async def test_fallback_feedback_unavailable_is_neutral(monkeypatch):
    async def boom(*_a, **_k):
        raise AgentUnavailable("down")

    monkeypatch.setattr(svc, "run_prompt_async", boom)
    out = await svc.ai_grade(prompt="Q?", expected="Paris", given="London")
    _assert_neutral(out["feedback"], "unavailable-fallback feedback")


async def test_fallback_feedback_bad_json_is_neutral(monkeypatch):
    async def garbage(*_a, **_k):
        return "not json"

    monkeypatch.setattr(svc, "run_prompt_async", garbage)
    out = await svc.ai_grade(prompt="Q?", expected="Paris", given="London")
    _assert_neutral(out["feedback"], "bad-json-fallback feedback")


async def test_fallback_feedback_busy_is_neutral(monkeypatch):
    async def busy(*_a, **_k):
        raise AgentBusy("shared capacity saturated")

    monkeypatch.setattr(svc, "run_prompt_async", busy)
    out = await svc.ai_grade(prompt="Q?", expected="Paris", given="London")
    _assert_neutral(out["feedback"], "busy-fallback feedback")


# ---- Go worker progress/error string literals --------------------------


def _go_string_literals(source: str) -> list[str]:
    """Extract interpreted (") and raw (`) Go string literals,
    skipping line + block comments. Small state machine; enough for
    the worker sources (no lexer edge cases like rune literals with
    quote chars are load-bearing here)."""
    literals: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = source.find("\n", i)
            i = n if j < 0 else j + 1
        elif c == "/" and nxt == "*":
            j = source.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and source[j] != '"':
                if source[j] == "\\":
                    buf.append(source[j : j + 2])
                    j += 2
                else:
                    buf.append(source[j])
                    j += 1
            literals.append("".join(buf))
            i = j + 1
        elif c == "`":
            j = source.find("`", i + 1)
            if j < 0:
                break
            literals.append(source[i + 1 : j])
            i = j + 1
        else:
            i += 1
    return literals


def test_worker_string_literals_are_neutral():
    """No string literal in the worker's workflows/activities names the
    vendor - those literals are what feeds progress.Error / activity
    error messages that templates render verbatim."""
    offenders = []
    for subdir in ("worker-go/workflows", "worker-go/activities"):
        for path in sorted((REPO_ROOT / subdir).glob("*.go")):
            if path.name.endswith("_test.go"):
                continue
            for lit in _go_string_literals(path.read_text()):
                if any(token in lit.lower() for token in _VENDOR_TOKENS):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {lit[:80]!r}")
    assert not offenders, "vendor name in worker string literals:\n" + "\n".join(offenders)
