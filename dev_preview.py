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
            {"id": 1, "name": "cherry", "total": 12, "due": 5},
            {"id": 2, "name": "temporal", "total": 18, "due": 0},
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
        "deck_name": "cherry",
        "questions": [],
        "due_count": 0,
    },
    "populated": {
        "deck_name": "temporal",
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
                "topic": "behavioral-star",
                "prompt": "Which of the following are senior-tell signals after a STAR story?",
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
        "deck_name": "cherry",
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

# study.html — q is one card

STUDY_FIXTURES: dict[str, dict[str, Any]] = {
    "mcq": {
        "deck_name": "cherry",
        "q": {
            "id": 22,
            "type": "mcq",
            "topic": "system-design",
            "prompt": "You're designing Cherry's webhook delivery system. A provider's endpoint times out **after** the request has been sent but before the 200 ack is received. Which is the simplest correct guarantee you should ship?",
            "choices_list": [
                "A monotonically increasing sequence number on each webhook",
                "An idempotency key (event ID) that the provider uses to dedupe",
                "Exactly-once delivery guaranteed by Cherry's broker",
                "A signed HMAC header so the provider can verify authenticity",
            ],
        },
    },
    "multi": {
        "deck_name": "cherry",
        "q": {
            "id": 23,
            "type": "multi",
            "topic": "behavioral-star",
            "prompt": "Which of the following are senior-tell signals after a STAR story?",
            "choices_list": [
                "A reflection: 'Knowing what I know now, I'd have done X differently'",
                "A quantified result with a real number",
                "Naming the dissenting view and why we chose otherwise",
                "Reciting the company's values verbatim",
            ],
        },
    },
    "code": {
        "deck_name": "temporal",
        "q": {
            "id": 21,
            "type": "code",
            "topic": "concurrency-go",
            "prompt": "Implement a thread-safe bounded blocking queue in Go (capacity `N`) with `Put(v)` and `Take()` that block when full/empty. Don't use external libraries. Show the type definition and the two methods.",
        },
    },
    "short": {
        "deck_name": "cherry",
        "q": {
            "id": 24,
            "type": "short",
            "topic": "system-design-money",
            "prompt": "Why should monetary amounts be stored as `bigint` (minor units, e.g. cents) rather than as floating-point? Give the one-line reason plus one concrete failure mode.",
        },
    },
}

# study_empty.html

STUDY_EMPTY_FIXTURES: dict[str, dict[str, Any]] = {
    "default": {"deck_name": "cherry"},
}

# result.html — verdict + state, plus picked/correct sets for mcq/multi.

_BASE_STATE_RIGHT = {"step": 2, "next_due": _next_due_in(4320), "interval_minutes": 1440}
_BASE_STATE_WRONG = {"step": 0, "next_due": _next_due_in(10), "interval_minutes": 10}

_RUBRIC = "- Names the core reason: floats can't represent decimal fractions exactly\n- Gives a money-specific failure: reconciliation drift, off-by-a-penny in sum/refund\n- Mentions bigint cents (or fixed-point) as the fix"

RESULT_FIXTURES: dict[str, dict[str, Any]] = {
    "mcq-right": {
        "deck_name": "cherry",
        "q": {
            "id": 22,
            "type": "mcq",
            "topic": "system-design",
            "prompt": "You're designing Cherry's webhook delivery system. A provider's endpoint times out **after** the request has been sent. Which is the simplest correct guarantee you should ship?",
            "choices_list": [
                "A monotonically increasing sequence number on each webhook",
                "An idempotency key (event ID) that the provider uses to dedupe",
                "Exactly-once delivery guaranteed by Cherry's broker",
                "A signed HMAC header so the provider can verify authenticity",
            ],
            "answer": "An idempotency key (event ID) that the provider uses to dedupe",
            "rubric": _RUBRIC,
        },
        "user_answer": "An idempotency key (event ID) that the provider uses to dedupe",
        "idk": False,
        "verdict": {
            "result": "right",
            "feedback": "Correct.",
            "model_answer_summary": "An idempotency key (event ID) that the provider uses to dedupe",
        },
        "state": _BASE_STATE_RIGHT,
        "picked_set": ["An idempotency key (event ID) that the provider uses to dedupe"],
        "correct_set": ["An idempotency key (event ID) that the provider uses to dedupe"],
    },
    "mcq-wrong": {
        "deck_name": "cherry",
        "q": {
            "id": 22,
            "type": "mcq",
            "topic": "system-design",
            "prompt": "You're designing Cherry's webhook delivery system. A provider's endpoint times out **after** the request has been sent. Which is the simplest correct guarantee you should ship?",
            "choices_list": [
                "A monotonically increasing sequence number on each webhook",
                "An idempotency key (event ID) that the provider uses to dedupe",
                "Exactly-once delivery guaranteed by Cherry's broker",
                "A signed HMAC header so the provider can verify authenticity",
            ],
            "answer": "An idempotency key (event ID) that the provider uses to dedupe",
            "rubric": _RUBRIC,
        },
        "user_answer": "Exactly-once delivery guaranteed by Cherry's broker",
        "idk": False,
        "verdict": {
            "result": "wrong",
            "feedback": "Wrong choice.",
            "model_answer_summary": "An idempotency key (event ID) that the provider uses to dedupe",
        },
        "state": _BASE_STATE_WRONG,
        "picked_set": ["Exactly-once delivery guaranteed by Cherry's broker"],
        "correct_set": ["An idempotency key (event ID) that the provider uses to dedupe"],
    },
    "multi-right": {
        "deck_name": "cherry",
        "q": {
            "id": 23,
            "type": "multi",
            "topic": "behavioral-star",
            "prompt": "Which of the following are senior-tell signals after a STAR story?",
            "choices_list": [
                "A reflection: 'Knowing what I know now, I'd have done X differently'",
                "A quantified result with a real number",
                "Naming the dissenting view and why we chose otherwise",
                "Reciting the company's values verbatim",
            ],
            "answer": '["A reflection: \'Knowing what I know now, I\'d have done X differently\'", "A quantified result with a real number", "Naming the dissenting view and why we chose otherwise"]',
            "rubric": _RUBRIC,
        },
        "user_answer": "[]",
        "idk": False,
        "verdict": {
            "result": "right",
            "feedback": "All three correct picks landed.",
            "model_answer_summary": "",
        },
        "state": _BASE_STATE_RIGHT,
        "picked_set": [
            "A reflection: 'Knowing what I know now, I'd have done X differently'",
            "A quantified result with a real number",
            "Naming the dissenting view and why we chose otherwise",
        ],
        "correct_set": [
            "A reflection: 'Knowing what I know now, I'd have done X differently'",
            "A quantified result with a real number",
            "Naming the dissenting view and why we chose otherwise",
        ],
    },
    "multi-wrong": {
        "deck_name": "cherry",
        "q": {
            "id": 23,
            "type": "multi",
            "topic": "behavioral-star",
            "prompt": "Which of the following are senior-tell signals after a STAR story?",
            "choices_list": [
                "A reflection: 'Knowing what I know now, I'd have done X differently'",
                "A quantified result with a real number",
                "Naming the dissenting view and why we chose otherwise",
                "Reciting the company's values verbatim",
            ],
            "answer": '["A reflection", "Quantified result", "Naming the dissenting view"]',
            "rubric": _RUBRIC,
        },
        "user_answer": "[]",
        "idk": False,
        "verdict": {
            "result": "wrong",
            "feedback": "Missed two correct picks; included one wrong one.",
            "model_answer_summary": "",
        },
        "state": _BASE_STATE_WRONG,
        "picked_set": [
            "Reciting the company's values verbatim",
            "A reflection: 'Knowing what I know now, I'd have done X differently'",
        ],
        "correct_set": [
            "A reflection: 'Knowing what I know now, I'd have done X differently'",
            "A quantified result with a real number",
            "Naming the dissenting view and why we chose otherwise",
        ],
    },
    "code-right": {
        "deck_name": "temporal",
        "q": {
            "id": 21,
            "type": "code",
            "topic": "concurrency-go",
            "prompt": "Implement a thread-safe bounded blocking queue in Go (capacity N).",
            "answer": "type BBQ struct {\n    items chan any\n}\nfunc New(n int) *BBQ           { return &BBQ{items: make(chan any, n)} }\nfunc (q *BBQ) Put(v any)       { q.items <- v }\nfunc (q *BBQ) Take() any       { return <-q.items }",
            "rubric": _RUBRIC,
        },
        "user_answer": "type BBQ struct { c chan any }\nfunc New(n int) *BBQ { return &BBQ{c: make(chan any, n)} }\nfunc (q *BBQ) Put(v any) { q.c <- v }\nfunc (q *BBQ) Take() any { return <-q.c }",
        "idk": False,
        "verdict": {
            "result": "right",
            "feedback": "Buffered channel + send/receive is the canonical Go idiom — Put blocks when full, Take blocks when empty, both are safe under concurrent use without explicit locking.",
            "model_answer_summary": "Wrap a buffered channel.",
        },
        "state": _BASE_STATE_RIGHT,
        "picked_set": [],
        "correct_set": [],
    },
    "code-wrong": {
        "deck_name": "temporal",
        "q": {
            "id": 21,
            "type": "code",
            "topic": "concurrency-go",
            "prompt": "Implement a thread-safe bounded blocking queue in Go (capacity N).",
            "answer": "type BBQ struct { items chan any }\n// ... (canonical channel-based impl)",
            "rubric": _RUBRIC,
        },
        "user_answer": "type BBQ struct {\n    items []any\n    mu    sync.Mutex\n}\nfunc (q *BBQ) Put(v any) { q.mu.Lock(); q.items = append(q.items, v); q.mu.Unlock() }\nfunc (q *BBQ) Take() any { q.mu.Lock(); defer q.mu.Unlock(); v := q.items[0]; q.items = q.items[1:]; return v }",
        "idk": False,
        "verdict": {
            "result": "wrong",
            "feedback": "Doesn't block when empty (Take panics on empty slice) or when full (Put always succeeds — capacity is ignored). A bounded blocking queue must do both.",
            "model_answer_summary": "Wrap a buffered channel — the channel handles both blocking conditions naturally.",
        },
        "state": _BASE_STATE_WRONG,
        "picked_set": [],
        "correct_set": [],
    },
    "short-right": {
        "deck_name": "cherry",
        "q": {
            "id": 24,
            "type": "short",
            "topic": "system-design-money",
            "prompt": "Why should monetary amounts be stored as bigint cents rather than floats?",
            "answer": "Floats can't represent most decimal fractions exactly, so arithmetic accumulates rounding error. A concrete failure: summing thousands of payment amounts in a float drifts by fractions of a cent and breaks reconciliation against the bank's ledger.",
            "rubric": _RUBRIC,
        },
        "user_answer": "Because floats lose precision on decimal fractions; you'd see reconciliation drift when summing thousands of payments.",
        "idk": False,
        "verdict": {
            "result": "right",
            "feedback": "Hits the core reason (decimal precision loss) and a concrete money-specific failure (reconciliation drift). Could also mention bigint cents / fixed-point as the fix to be more complete.",
            "model_answer_summary": "Floats can't represent most decimals exactly → accumulating rounding error → reconciliation drift.",
        },
        "state": _BASE_STATE_RIGHT,
        "picked_set": [],
        "correct_set": [],
    },
    "short-wrong": {
        "deck_name": "cherry",
        "q": {
            "id": 24,
            "type": "short",
            "topic": "system-design-money",
            "prompt": "Why should monetary amounts be stored as bigint cents rather than floats?",
            "answer": "Floats can't represent most decimal fractions exactly, so arithmetic accumulates rounding error. Use bigint cents.",
            "rubric": _RUBRIC,
        },
        "user_answer": "Because bigint is faster than float on most CPUs.",
        "idk": False,
        "verdict": {
            "result": "wrong",
            "feedback": "The motivation isn't speed — it's correctness. Floats use binary representation that can't exactly hold most decimal fractions, so summing money in floats drifts.",
            "model_answer_summary": "Floats can't represent most decimals exactly → reconciliation drift.",
        },
        "state": _BASE_STATE_WRONG,
        "picked_set": [],
        "correct_set": [],
    },
    "code-idk": {
        "deck_name": "temporal",
        "q": {
            "id": 21,
            "type": "code",
            "topic": "concurrency-go",
            "prompt": "Implement a thread-safe bounded blocking queue in Go (capacity N).",
            "answer": "type BBQ struct { items chan any }\n// ...",
            "rubric": _RUBRIC,
        },
        "user_answer": "",
        "idk": True,
        "verdict": {
            "result": "wrong",
            "feedback": "Marked as 'I don't know' — see again soon.",
            "model_answer_summary": "Wrap a buffered channel.",
        },
        "state": _BASE_STATE_WRONG,
        "picked_set": [],
        "correct_set": [],
    },
}

# generation.html — wid, deck_name, progress, desc

GENERATION_FIXTURES: dict[str, dict[str, Any]] = {
    "in-progress": {
        "wid": "gen-temporal-PREVIEW01",
        "deck_name": "temporal",
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
        "wid": "gen-temporal-PREVIEW02",
        "deck_name": "temporal",
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

GRADING_FIXTURES: dict[str, dict[str, Any]] = {
    "in-progress": {
        "wid": "grade-temporal-q21-PREVIEW",
        "deck_name": "temporal",
        "progress": {"status": "grading", "started_at": _now_iso()},
        "desc": {
            "status": "RUNNING",
            "started_at": _now_iso(),
            "closed_at": None,
            "task_queue": "prep-generation",
        },
        "failed": False,
    },
}


# ---- Registry ------------------------------------------------------------

_REGISTRY: dict[str, dict[str, dict[str, Any]]] = {
    "index": INDEX_FIXTURES,
    "deck": DECK_FIXTURES,
    "study": STUDY_FIXTURES,
    "study_empty": STUDY_EMPTY_FIXTURES,
    "result": RESULT_FIXTURES,
    "generation": GENERATION_FIXTURES,
    "grading": GRADING_FIXTURES,
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

    @app.get("/dev/study-by-id/{qid}", response_class=HTMLResponse, include_in_schema=False)
    async def study_by_id(request: Request, qid: int):
        """Render study.html for any DB question id, regardless of due state.
        Used by the screenshot capture script to grab specific cards (e.g.
        the fizzbuzz card for the skeleton-feature verification) without
        mutating the cards.next_due column to force them due."""
        import db as _db  # local import to avoid circular

        q = _db.get_question(qid)
        if not q:
            raise HTTPException(404, f"no question {qid}")
        # Pick deck_name from the questions/decks join.
        with _db.cursor() as c:
            row = c.execute(
                "SELECT d.name FROM decks d JOIN questions q ON q.deck_id=d.id " "WHERE q.id = ?",
                (qid,),
            ).fetchone()
        return templates.TemplateResponse(
            "study.html",
            {"request": request, "q": q, "deck_name": row["name"] if row else ""},
        )

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
