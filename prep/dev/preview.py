"""Dev-only template preview routes for the UI sweep.

Mounted onto the FastAPI app via `dev_preview.register(app, templates)`.
Renders any template with named fixture data — read-only, no DB writes,
doesn't interfere with the running app's state.

Fixtures are kept here (not loaded from disk) so the screenshot script
needs no extra files. Each fixture mirrors the shape that the real route
handlers pass into the template.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


def _now_iso() -> str:
    # Used by fixtures that need a timestamp-shaped string. Keep it stable
    # so screenshots are deterministic across runs.
    return "2026-04-27T04:55:00.000Z"


def _next_due_in(minutes: int) -> str:
    # Stable string — the template only slices [:16] for display.
    return f"2026-04-27T{(4 + minutes // 60) % 24:02d}:{minutes % 60:02d}:00.000Z"


# ---- Fixtures by template ------------------------------------------------

# index.html

INDEX_FIXTURES: dict[str, dict[str, Any]] = {
    "empty": {"decks": []},
    "populated": {
        "decks": [
            {"id": 1, "name": "go-systems", "total": 12, "due": 5},
            {"id": 2, "name": "webhooks", "total": 18, "due": 0},
        ]
    },
}

# deck.html

_QCARD_BASE = {
    "type": "code",
    "topic": "concurrency-go",
    "suspended": 0,
    "next_due": _next_due_in(1440),
    "last_review": None,
    "rights": 1,
    "attempts": 2,
    "step": 1,
    "prompt": "Implement a thread-safe bounded blocking queue in Go (capacity N) with Put and Take that block when full/empty. No external libraries.",
}

DECK_FIXTURES: dict[str, dict[str, Any]] = {
    "empty": {
        "deck_name": "go-systems",
        "questions": [],
        "due_count": 0,
    },
    "populated": {
        "deck_name": "webhooks",
        "due_count": 3,
        "questions": [
            {"id": 21, **_QCARD_BASE, "type": "code"},
            {
                "id": 22,
                **_QCARD_BASE,
                "type": "mcq",
                "topic": "system-design",
                "prompt": "Which retry strategy is best for webhook delivery to a flaky downstream?",
            },
            {
                "id": 23,
                **_QCARD_BASE,
                "type": "multi",
                "topic": "behavioral",
                "prompt": "Which of the following are indicators of senior-level reflection in a project retro?",
            },
            {
                "id": 24,
                **_QCARD_BASE,
                "type": "short",
                "topic": "system-design-money",
                "prompt": "Why should monetary amounts be stored as bigint cents rather than floats?",
            },
        ],
    },
    "with_suspended": {
        "deck_name": "go-systems",
        "due_count": 1,
        "questions": [
            {"id": 31, **_QCARD_BASE, "suspended": 0, "type": "code"},
            {
                "id": 32,
                **_QCARD_BASE,
                "suspended": 1,
                "type": "mcq",
                "topic": "broken",
                "prompt": "(busted card — typo in the answer key)",
            },
        ],
    },
}

# result.html — verdict + state, plus picked/correct sets for mcq/multi.

_BASE_STATE_RIGHT = {"step": 2, "next_due": _next_due_in(4320), "interval_minutes": 1440}
_BASE_STATE_WRONG = {"step": 0, "next_due": _next_due_in(10), "interval_minutes": 10}

_RUBRIC = "- Names the core reason: floats can't represent decimal fractions exactly\n- Gives a money-specific failure: reconciliation drift, off-by-a-penny in sum/refund\n- Mentions bigint cents (or fixed-point) as the fix"

GENERATION_FIXTURES: dict[str, dict[str, Any]] = {
    "in-progress": {
        "wid": "gen-webhooks-PREVIEW01",
        "deck_name": "webhooks",
        "progress": {
            "total": 5,
            "completed": 2,
            "current_topic": "consistent-hashing",
            "started_at": _now_iso(),
            "last_card_at": _now_iso(),
            "status": "generating",
        },
        "desc": {
            "status": "RUNNING",
            "started_at": _now_iso(),
            "closed_at": None,
            "task_queue": "prep-generation",
        },
    },
    "complete": {
        "wid": "gen-webhooks-PREVIEW02",
        "deck_name": "webhooks",
        "progress": {
            "total": 5,
            "completed": 5,
            "current_topic": "wal-recovery",
            "started_at": _now_iso(),
            "last_card_at": _now_iso(),
            "status": "done",
        },
        "desc": {
            "status": "COMPLETED",
            "started_at": _now_iso(),
            "closed_at": _now_iso(),
            "task_queue": "prep-generation",
        },
    },
}

# grading.html — wid, deck_name, progress, desc, failed

# ---- Registry ------------------------------------------------------------

_REGISTRY: dict[str, dict[str, dict[str, Any]]] = {
    "index": INDEX_FIXTURES,
    "deck": DECK_FIXTURES,
    "generation": GENERATION_FIXTURES,
}


def all_fixtures() -> list[tuple[str, str]]:
    """Return [(template, fixture_name), ...] for every preview the screenshot
    script should capture. Stable ordering for reproducibility."""
    out: list[tuple[str, str]] = []
    for tpl, fixtures in _REGISTRY.items():
        for name in fixtures.keys():
            out.append((tpl, name))
    return out


def register(app: FastAPI, templates: Jinja2Templates) -> None:
    """Mount the dev preview routes onto an existing FastAPI app."""

    @app.get(
        "/dev/preview/{template}/{fixture}", response_class=HTMLResponse, include_in_schema=False
    )
    async def preview(request: Request, template: str, fixture: str):
        fixtures = _REGISTRY.get(template)
        if fixtures is None:
            raise HTTPException(404, f"unknown template '{template}' (have: {sorted(_REGISTRY)})")
        ctx = fixtures.get(fixture)
        if ctx is None:
            raise HTTPException(
                404,
                f"unknown fixture '{fixture}' for template '{template}' "
                f"(have: {sorted(fixtures)})",
            )
        ctx = {**ctx}
        # Result fixtures don't carry the handoff payload (it's computed in
        # the live route from the same question + answer data). Recompute
        # on the fly so the discuss popup is visible in dev preview too.
        if template == "result" and "handoff_urls" not in ctx:
            import chat_handoff

            msg = chat_handoff.build_message(
                deck_name=ctx.get("deck_name", ""),
                q=ctx.get("q", {}),
                user_answer=ctx.get("user_answer", ""),
                verdict=ctx.get("verdict"),
                idk=ctx.get("idk", False),
                picked_set=ctx.get("picked_set", []),
                correct_set=ctx.get("correct_set", []),
            )
            ctx["handoff_message"] = msg
            ctx["handoff_urls"] = chat_handoff.provider_urls(msg)
            ctx["handoff_providers"] = chat_handoff.CHAT_PROVIDERS
            ctx["handoff_default_provider"] = chat_handoff.DEFAULT_PROVIDER
        # Inject `request` for url generation in templates.
        return templates.TemplateResponse(f"{template}.html", {"request": request, **ctx})

    @app.get("/dev/preview", response_class=HTMLResponse, include_in_schema=False)
    async def preview_index(request: Request):
        rows = "\n".join(
            f'<li><a href="{request.scope.get("root_path","")}/dev/preview/{t}/{f}">{t}/{f}</a></li>'
            for t, f in all_fixtures()
        )
        return HTMLResponse(
            f"<!doctype html><html><body><h1>Preview index</h1>"
            f"<p>Dev-only template renders for the UI sweep — no DB writes.</p>"
            f"<ul>{rows}</ul></body></html>"
        )
