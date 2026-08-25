"""Template contexts for the golden HTML renderer.

Every page template and partial appears at least once; the branchy
ones (the three progress partials, the diff card, the deck page,
the settings pages) appear once per branch. Each entry is
`(template, context_name, context, base_overrides)`; the renderer
merges `base_context(**base_overrides)` under `context`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from tests.parity.oracles import (
    PARITY_BUILD_ID,
    PARITY_NOW,
    PARITY_USER,
    PARITY_USER_NAME,
)

NOW = PARITY_NOW.isoformat()
EARLIER = "2026-03-13T09:30:00+00:00"
MONTH_AGO = "2026-02-12T15:00:00+00:00"
DUE_SOON = "2026-03-14T16:45:00+00:00"
DUE_TOMORROW = "2026-03-15T15:00:00+00:00"
DUE_LATER = "2026-03-21T15:00:00+00:00"
FOREVER = "2099-01-01T00:00:00+00:00"

XSS = "</script><script>alert(1)</script>"

USER = {
    "tailscale_login": PARITY_USER,
    "display_name": PARITY_USER_NAME,
    "email": PARITY_USER,
    "profile_pic_url": None,
    "created_at": MONTH_AGO,
    "last_seen_at": NOW,
    "is_anonymous": 0,
    "editor_input_mode": "vim",
    "desired_retention": None,
}

ANON_USER = {
    "tailscale_login": "anon:" + "ab" * 16,
    "display_name": "Guest",
    "email": None,
    "profile_pic_url": None,
    "created_at": EARLIER,
    "last_seen_at": NOW,
    "is_anonymous": 1,
    "editor_input_mode": None,
    "desired_retention": None,
}

DECK_DISPLAY = {
    "capitals": "World Capitals",
    "distsys": "Distributed Systems",
    "history-trivia": "World History Trivia",
    "xss": XSS,
    "inbox": "inbox",
}

SIGN_IN = "https://accounts.example.test/sign-in"
SIGN_UP = "https://accounts.example.test/sign-up"
SIGN_OUT = "https://accounts.example.test/sign-out"


def base_context(
    *,
    user: dict | None = USER,
    agent_available: bool = True,
    auth_provider: str = "tailscale",
    sign_in_url: str = "",
    sign_up_url: str = "",
    sign_out_url: str = "",
    notif_unseen_count: int = 0,
) -> dict:
    """The nine context-processor names, supplied explicitly."""
    return {
        "user": user,
        "agent_available": agent_available,
        "static_css_mtime": PARITY_BUILD_ID,
        "auth_provider": auth_provider,
        "sign_in_url": sign_in_url,
        "sign_up_url": sign_up_url,
        "sign_out_url": sign_out_url,
        "clerk_publishable_key": None,
        "clerk_frontend_api_host": None,
        "notif_unseen_count": notif_unseen_count,
        "deck_display": lambda slug: DECK_DISPLAY.get(slug, slug) if slug else "",
    }


def fake_request(path: str = "/") -> SimpleNamespace:
    return SimpleNamespace(
        scope={"root_path": ""},
        url=SimpleNamespace(scheme="https", netloc="parity.example.test", path=path),
        headers={},
        cookies={},
        state=SimpleNamespace(),
    )


CLERK = {
    "auth_provider": "clerk",
    "sign_in_url": SIGN_IN,
    "sign_up_url": SIGN_UP,
    "sign_out_url": SIGN_OUT,
}


@dataclass(frozen=True)
class Ctx:
    template: str
    name: str
    context: dict = field(default_factory=dict)
    base: dict = field(default_factory=dict)


# ---- shared fixtures -----------------------------------------------------


def card(
    qid: int,
    qtype: str,
    prompt: str,
    answer: str,
    *,
    topic: str | None = None,
    choices: list[str] | None = None,
    rubric: str | None = None,
    skeleton: str | None = None,
    language: str | None = None,
    answer_regex: str | None = None,
    explanation: str | None = None,
    suspended: bool = False,
    step: int = 0,
    next_due: str | None = NOW,
    last_review: str | None = None,
    rights: int = 0,
    attempts: int = 0,
) -> SimpleNamespace:
    """A `DeckCard`-shaped object (attribute access plus
    `choices_list`)."""
    return SimpleNamespace(
        id=qid,
        type=qtype,
        topic=topic,
        prompt=prompt,
        choices=choices,
        choices_list=choices or [],
        answer=answer,
        rubric=rubric,
        skeleton=skeleton,
        language=language,
        answer_regex=answer_regex,
        explanation=explanation,
        suspended=suspended,
        step=step,
        next_due=next_due,
        last_review=last_review,
        rights=rights,
        attempts=attempts,
    )


SRS_CARDS = [
    card(
        41,
        "short",
        "Capital of **France**?",
        "Paris",
        topic="europe",
        answer_regex=r"paris",
        explanation="Paris has been the capital since 987.",
        step=3,
        next_due=EARLIER,
        last_review=MONTH_AGO,
        rights=4,
        attempts=5,
    ),
    card(
        42,
        "mcq",
        "Which city is the capital of Japan?",
        "Tokyo",
        topic="asia",
        choices=["Tokyo", "Osaka", "Kyoto", "Nagoya"],
        step=1,
        next_due=DUE_TOMORROW,
        last_review=EARLIER,
        rights=1,
        attempts=2,
    ),
    card(
        43,
        "multi",
        "Which of these are capitals?",
        json.dumps(["Lima", "Quito"]),
        topic="south-america",
        choices=["Lima", "Quito", "Cusco", "Guayaquil"],
        rubric="- both Andean capitals",
        step=0,
    ),
    card(
        44,
        "code",
        "Write `add(a, b)` returning the sum.\n\n```python\ndef add(a, b):\n    ...\n```",
        "def add(a, b):\n    return a + b",
        topic="python",
        skeleton="def add(a, b):\n    pass",
        language="python",
        rubric="- returns a + b\n- no side effects",
        step=5,
        next_due=DUE_LATER,
        last_review=EARLIER,
        rights=9,
        attempts=9,
    ),
    card(
        45,
        "short",
        "A very long prompt " + "that keeps going " * 20 + "until it is truncated on the card.",
        "long",
        suspended=True,
        step=2,
        next_due=None,
        rights=0,
        attempts=1,
    ),
]

TRIVIA_CARDS = [
    card(51, "short", "Who painted the Mona Lisa?", "Leonardo da Vinci", next_due=None),
    card(
        52, "short", "Year the Berlin Wall fell?", "1989", next_due=None, explanation="November 9."
    ),
]


def deck_meta(
    deck_id: int,
    *,
    pinned: bool = False,
    display_name: str | None = None,
    context_prompt: str = "",
    notifications_enabled: bool = True,
    interval_minutes: int | None = None,
    session_size: int = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        deck_id=deck_id,
        pinned=pinned,
        display_name=display_name,
        context_prompt=context_prompt,
        notifications_enabled=notifications_enabled,
        interval_minutes=interval_minutes,
        session_size=session_size,
    )


RETENTION_PRESETS = (
    (0.80, "80% — Relaxed", "Fewer reviews; more cards slip through."),
    (0.85, "85% — Mild", "Slightly less frequent; light maintenance."),
    (0.90, "90% — Default", "Anki's default. Balances retention vs work."),
    (0.95, "95% — Strict", "Tighter recall, more frequent reviews."),
)

LONG_TOPIC = (
    "Distributed systems consensus: Paxos, Raft, quorum intersection, leader election, "
    "linearizability versus sequential consistency, and the failure modes of each."
)


def menu_deck(
    deck_id: int,
    name: str,
    display: str | None,
    *,
    total: int,
    due: int,
    deck_type: str = "srs",
    pinned: bool = False,
    notifications_enabled: bool = True,
    interval_minutes: int | None = None,
    session_size: int = 3,
) -> dict:
    """`DeckSummary.model_dump()` plus the meta fields the overflow
    menu reads through `deck_meta`."""
    return {
        "id": deck_id,
        "name": name,
        "display_name": display,
        "total": total,
        "due": due,
        "deck_type": deck_type,
        "pinned": pinned,
        "deck_id": deck_id,
        "notifications_enabled": notifications_enabled,
        "interval_minutes": interval_minutes,
        "session_size": session_size,
    }


MENU_DECKS = [
    menu_deck(1, "capitals", "World Capitals", total=5, due=2, pinned=True),
    menu_deck(2, "distsys", "Distributed Systems", total=0, due=0),
    menu_deck(
        3,
        "history-trivia",
        "World History Trivia",
        total=2,
        due=0,
        deck_type="trivia",
        interval_minutes=45,
        session_size=5,
    ),
]


def overview(decks: list[dict], *, anonymous: bool = False, next_due: int | None = 105) -> dict:
    return {
        "user": {
            "display_name": "Guest" if anonymous else PARITY_USER_NAME,
            "is_anonymous": anonymous,
        },
        "decks": decks,
        "due": sum(d["due"] for d in decks),
        "total": sum(d["total"] for d in decks),
        "nextDueMinutes": next_due,
        "unsynced": None,
    }


def overview_deck(d: dict, trivia_stats: dict | None = None) -> dict:
    return {
        "id": d["id"],
        "slug": d["name"],
        "display_name": d["display_name"],
        "due": d["due"],
        "total": d["total"],
        "deck_type": d["deck_type"],
        "pinned": d["pinned"],
        "trivia_stats": trivia_stats,
    }


OVERVIEW_DECKS = [
    overview_deck(MENU_DECKS[0]),
    overview_deck(MENU_DECKS[1]),
    overview_deck(MENU_DECKS[2], {"total": 2, "unanswered": 1, "wrong": 0, "mastered": 1}),
]


def recent_session(
    sid: str,
    deck: str,
    display: str | None,
    *,
    state: str = "awaiting-answer",
    current_type: str | None = "short",
    device_label: str | None = "iPhone",
) -> dict:
    return {
        "id": sid,
        "deck_id": 1,
        "deck_name": deck,
        "deck_display_name": display,
        "last_active": EARLIER,
        "status": "active",
        "state": state,
        "device_label": device_label,
        "current_question_id": 41,
        "current_prompt": "Capital of France?",
        "current_type": current_type,
        "snoozed_until": None,
    }


RECENT_SESSIONS = [
    recent_session("s1parity00000001", "capitals", "World Capitals"),
    recent_session("s1parity00000002", "distsys", None, state="grading", device_label=None),
    recent_session("s1parity00000003", "capitals", "World Capitals", state="showing-result"),
    recent_session("s1parity00000004", "distsys", "Distributed Systems", current_type=None),
]

ACTIVE_TRIVIA = [
    {
        "deck_name": "history-trivia",
        "deck_display": "World History Trivia",
        "deck_id": 3,
        "remaining": 2,
        "total": 3,
        "last_active": EARLIER,
        "queue_param": "52,51",
        "done_param": "51:r",
    }
]

SNOOZED = [
    {
        "kind": "srs",
        "id": "s1parity00000009",
        "deck_name": "capitals",
        "deck_display": "World Capitals",
        "snoozed_until": DUE_SOON,
    },
    {
        "kind": "trivia",
        "deck_name": "history-trivia",
        "deck_display": None,
        "snoozed_until": DUE_LATER,
    },
    {
        "kind": "srs",
        "id": "s1parity00000010",
        "deck_name": "distsys",
        "deck_display": "Distributed Systems",
        "snoozed_until": FOREVER,
    },
]


def plan_item(
    title: str, brief: str, qtype: str | None = "short", topic: str | None = None, language=None
):
    return {"title": title, "brief": brief, "type": qtype, "topic": topic, "language": language}


PLAN = [
    plan_item("Consensus basics", "Why quorums must intersect.", "short", "consensus"),
    plan_item("Raft leader election", "Terms, votes, split votes.", "mcq", "raft"),
    plan_item("Implement a log append", "A minimal append-entries handler.", "code", "raft", "go"),
    plan_item("Untyped item", "No type, no topic.", None),
]


def plan_progress(status: str | None, **extra) -> dict:
    progress = {"status": status, "plan": PLAN, "total": 4, "round": 1}
    progress.update(extra)
    return progress


PLAN_STATUSES = {
    "starting": plan_progress(None, plan=None, total=None),
    "planning": plan_progress("planning", plan=None),
    "replanning-round-2": plan_progress("replanning", round=2),
    "awaiting-feedback": plan_progress("awaiting_feedback"),
    "awaiting-feedback-round-2": plan_progress("awaiting_feedback", round=2),
    "awaiting-feedback-one-card": plan_progress("awaiting_feedback", plan=PLAN[:1], total=1),
    "accepting": plan_progress("accepting"),
    "generating": plan_progress("generating", generated_count=2),
    "generating-none-yet": plan_progress("generating", generated_count=0),
    "applying": plan_progress("applying", generated_count=4),
    "rejecting": plan_progress("rejecting"),
    "done": plan_progress("done", result={"added_ids": [61, 62, 63, 64]}),
    "done-one-card": plan_progress("done", result={"added_ids": [61]}),
    "rejected": plan_progress("rejected"),
    "failed": plan_progress("failed", error="the model returned no plan"),
    "gone": plan_progress("gone", plan=None),
}


def mod_diff(
    qid: int, deck: str, changed: dict[str, str] | None, *, old: dict | None = None
) -> dict:
    base_old = {
        "type": "short",
        "topic": "europe",
        "prompt": "Capital of France?",
        "answer": "Paris",
        "rubric": "",
        "skeleton": "",
        "language": "",
        "explanation": "",
        "answer_regex": "",
    }
    if old:
        base_old.update(old)
    new = dict(base_old)
    if changed:
        new.update(changed)
    return {"question_id": qid, "deck_name": deck, "old": base_old, "new": new}


DIFF_FIELDS = (
    "prompt",
    "answer",
    "answer_regex",
    "explanation",
    "topic",
    "rubric",
    "type",
    "skeleton",
    "language",
)

DIFF_CARD_STATES: dict[str, dict] = {
    f"changed-{f}": mod_diff(41, "capitals", {f: f"new {f} value"}) for f in DIFF_FIELDS
}
DIFF_CARD_STATES["unchanged"] = mod_diff(42, "capitals", None)
DIFF_CARD_STATES["cleared-field"] = mod_diff(43, "capitals", {"topic": ""})
DIFF_CARD_STATES["all-changed"] = mod_diff(
    44,
    "distsys",
    {f: f"after {f}" for f in DIFF_FIELDS},
    old={"rubric": "- before", "skeleton": "x = 1", "language": "python"},
)


def addition(prompt: str, qtype: str = "short", dest: str | None = None) -> dict:
    return {"type": qtype, "prompt": prompt, "dest_deck": dest}


def transform_plan(*, reorganize: bool = False, overflow: bool = True) -> dict:
    adds = [
        addition(f"Addition {i} " + "with a long prompt " * 12, "short", "distsys")
        for i in range(8 if overflow else 2)
    ]
    adds += [
        addition("Capital of Peru?", "mcq", "capitals"),
        addition("Quorum size for n=5?", "short", "distsys"),
    ]
    dels = list(range(101, 109 if overflow else 103))
    plan = {
        "notes": "Split the consensus material out and tighten the wording.",
        "modifications": [{"question_id": 41}, {"question_id": 44}, {"question_id": 45}],
        "additions": adds,
        "deletions": dels,
    }
    if reorganize:
        plan.update(
            {
                "new_decks": [
                    {"name": "consensus", "deck_type": "srs", "topic": LONG_TOPIC},
                    {"name": "pub-quiz", "deck_type": "trivia", "topic": None},
                    {"name": "untyped", "deck_type": None, "topic": "short topic"},
                ],
                "deck_renames": [{"deck_id": 2, "new_name": "distributed-systems"}],
                "card_moves": [
                    {"question_id": qid, "dest_deck": "consensus"}
                    for qid in range(201, 210 if overflow else 203)
                ]
                + [{"question_id": 44, "dest_deck": "capitals"}],
                "deck_deletions": [3, 99],
            }
        )
    return plan


def transform_ctx(
    scope: str,
    status: str | None,
    *,
    plan: dict | None = None,
    result: dict | None = None,
    error: str | None = None,
    deck: str | None = "distsys",
) -> dict:
    progress: dict = {"status": status}
    if plan is not None:
        progress["plan"] = plan
    if result is not None:
        progress["result"] = result
    if error:
        progress["error"] = error
    reorganize = scope == "reorganize"
    return {
        "wid": f"transform-{scope}-PARITY01",
        "scope": scope,
        "target_id": 2 if scope != "card" else 41,
        "deck_name": deck if not reorganize else "",
        "progress": progress,
        "desc": {},
        "modification_diffs": [
            mod_diff(41, "capitals", {"prompt": "Capital city of France?"}),
            mod_diff(44, "distsys", {"rubric": "- returns a + b", "skeleton": "def add(a, b):"}),
            mod_diff(45, "distsys", None),
        ],
        "deletion_decks": {101: "capitals", 102: "distsys", 103: "capitals", 104: "distsys"},
        "move_source_decks": {201: "distsys", 202: "distsys", 203: "capitals", 44: "distsys"},
        "deck_id_to_name": {3: "history-trivia"},
    }


TRANSFORM_STATUSES = (
    "computing",
    "awaiting_apply",
    "applying",
    "rejecting",
    "done",
    "rejected",
    "gone",
    "failed",
    "",
)


def transform_contexts() -> list[Ctx]:
    out: list[Ctx] = []
    for scope in ("card", "deck", "reorganize"):
        for status in TRANSFORM_STATUSES:
            label = status or "starting"
            kwargs: dict = {}
            if status in ("awaiting_apply", "applying", "rejecting"):
                kwargs["plan"] = transform_plan(reorganize=scope == "reorganize")
            if status == "done":
                kwargs["plan"] = transform_plan(reorganize=scope == "reorganize", overflow=False)
                kwargs["result"] = {"modified_ids": [41, 44], "added_ids": [61], "deleted_ids": []}
            if status == "failed":
                kwargs["error"] = "the model timed out"
            ctx = transform_ctx(scope, status, **kwargs)
            out.append(Ctx("transform.html", f"{scope}-{label}", ctx))
            out.append(Ctx("partials/transform_progress.html", f"{scope}-{label}", ctx))
    out.append(
        Ctx(
            "partials/transform_progress.html",
            "deck-done-no-deck",
            transform_ctx(
                "deck",
                "done",
                result={"modified_ids": [], "added_ids": [], "deleted_ids": [7]},
                deck="",
            ),
        )
    )
    out.append(
        Ctx(
            "partials/transform_progress.html",
            "reorganize-awaiting-apply-no-overflow",
            transform_ctx(
                "reorganize", "awaiting_apply", plan=transform_plan(reorganize=True, overflow=False)
            ),
        )
    )
    return out


def trivia_gen_progress(status: str | None, **extra) -> dict:
    progress = {"status": status, "total": 25, "generated_count": 0, "inserted": 0}
    progress.update(extra)
    return progress


TRIVIA_GEN_STATUSES = {
    "starting": trivia_gen_progress(None, total=None),
    "generating": trivia_gen_progress("generating", generated_count=7),
    "generating-no-total": trivia_gen_progress("generating", total=0, generated_count=3),
    "applying": trivia_gen_progress("applying", generated_count=25, inserted=12),
    "applying-no-inserted": trivia_gen_progress("applying", generated_count=25, inserted=0),
    "done": trivia_gen_progress("done", generated_count=25, inserted=24),
    "failed": trivia_gen_progress("failed", error="upstream returned 429"),
    "unknown-status": trivia_gen_progress("retrying"),
}


def trivia_result(
    *,
    correct: bool,
    given: str = "Leonardo",
    idk: bool = False,
    feedback: str | None = None,
    regraded: bool = False,
    overridden: bool = False,
    regex_updated: bool = False,
) -> dict:
    return {
        "correct": correct,
        "given": given,
        "expected": "Leonardo da Vinci",
        "feedback": feedback,
        "idk": idk,
        "regraded": regraded,
        "overridden": overridden,
        "regex_updated": regex_updated,
    }


EXPLORE = {
    "handoff_providers": {
        "claude": {"label": "Claude", "url": "https://claude.ai/new?q={q}"},
        "chatgpt": {"label": "ChatGPT", "url": "https://chatgpt.com/?q={q}"},
    },
    "handoff_urls": {
        "claude": "https://claude.ai/new?q=Who+painted+the+Mona+Lisa%3F",
        "chatgpt": "https://chatgpt.com/?q=Who+painted+the+Mona+Lisa%3F",
    },
    "google_search_url": "https://www.google.com/search?q=Who+painted+the+Mona+Lisa%3F",
}

TRIVIA_Q = TRIVIA_CARDS[0]
TRIVIA_Q_EXPLAINED = card(
    52,
    "short",
    "Year the Berlin Wall fell?\n\n- one line\n- another",
    "1989",
    next_due=None,
    explanation="November 9, 1989.",
)


def trivia_card_ctx(q, result, *, session: bool, **extra) -> dict:
    ctx = {"q": q, "deck_name": "history-trivia", "result": result}
    if session:
        ctx.update(
            {
                "session_position": 2,
                "session_total": 3,
                "session_remaining": "51",
                "session_done": "52:r",
            }
        )
    ctx.update(extra)
    return ctx


def session_done_results() -> list[dict]:
    return [
        {
            "verdict": "r",
            "prompt": "Who painted the Mona Lisa?",
            "answer": "Leonardo da Vinci",
            "explanation": None,
        },
        {
            "verdict": "w",
            "prompt": "Year the Berlin Wall fell?",
            "answer": "1989",
            "explanation": "November 9.",
        },
    ]


def workflow(
    wid: str,
    wtype: str,
    status: str,
    *,
    deck_name: str | None = "capitals",
    display: str | None = "World Capitals",
    url_path: str = "/plan/x",
) -> SimpleNamespace:
    from prep.workflows.entities import is_action_required, is_terminal

    return SimpleNamespace(
        workflow_id=wid,
        workflow_type=wtype,
        status=status,
        deck_name=deck_name,
        deck_display_name=display,
        url_path=url_path,
        is_action_required=is_action_required(status),
        is_terminal=is_terminal(status),
        display_status={
            "awaiting_apply": "review",
            "awaiting_feedback": "review plan",
            "done": "done",
            "failed": "failed",
            "rejected": "cancelled",
            "asking_ai": "asking AI",
        }.get(status, status or "starting"),
        display_label=display
        or deck_name
        or ("reorganize" if wtype == "transform" else wtype.replace("_", " ")),
    )


def notif_entry(eid: int, source: str, sent_at: str, *, seen: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=eid,
        user_id=PARITY_USER,
        sent_at=sent_at,
        title=f"{source} nudge",
        body="3 cards are due in World Capitals.",
        url="/deck/capitals",
        source=source,
        seen_at=NOW if seen else None,
    )


def prefs(mode: str = "off", *, quiet: bool = False) -> dict:
    return {
        "mode": mode,
        "digest_hour": 9,
        "tz": "America/New_York",
        "threshold": 3,
        "quiet_hours_enabled": quiet,
        "quiet_start_hour": 22,
        "quiet_end_hour": 8,
        "last_digest_date": None,
        "last_when_ready_at": None,
    }


def byok_section(
    provider: str, label: str, prefix: str, console: str, *, metadata: dict | None, active: bool
) -> dict:
    return {
        "provider": provider,
        "info": SimpleNamespace(
            provider=provider,
            label=label,
            short_label=provider.split("-")[0],
            key_prefixes=(prefix,),
            console_url=console,
            default_model="model",
        ),
        "metadata": SimpleNamespace(**metadata) if metadata else None,
        "is_active": active,
    }


def byok_sections(*, connected: bool) -> list[dict]:
    anthropic_meta = (
        {"key_prefix": "sk-ant-api03-…x9zT", "created_at": MONTH_AGO, "last_used_at": EARLIER}
        if connected
        else None
    )
    openai_meta = (
        {"key_prefix": "sk-…abcd", "created_at": EARLIER, "last_used_at": None}
        if connected
        else None
    )
    return [
        byok_section(
            "claude-subscription",
            "Claude subscription",
            "sk-ant-oat01-",
            "https://docs.claude.com/en/docs/agent-sdk/auth#claude-app-tokens",
            metadata=None,
            active=False,
        ),
        byok_section(
            "anthropic-api",
            "Anthropic",
            "sk-ant-api03-",
            "https://console.anthropic.com/settings/keys",
            metadata=anthropic_meta,
            active=connected,
        ),
        byok_section(
            "openai-api",
            "OpenAI",
            "sk-",
            "https://platform.openai.com/api-keys",
            metadata=openai_meta,
            active=False,
        ),
        byok_section(
            "openrouter-api",
            "OpenRouter",
            "sk-or-v1-",
            "https://openrouter.ai/keys",
            metadata=None,
            active=False,
        ),
    ]


def settings_agent_ctx(
    *,
    connected: bool = False,
    logged_in: bool = False,
    free_tier: bool = False,
    error: str | None = None,
    flash: str | None = None,
    byok_error: str | None = None,
    byok_flash: str | None = None,
) -> dict:
    status = (
        {"kind": "sdk", "logged_in": True}
        if logged_in
        else {"kind": "unconfigured", "logged_in": False, "reason": "no token"}
    )
    return {
        "status": status,
        "error": error,
        "flash": flash,
        "byok_sections": byok_sections(connected=connected),
        "byok_error": byok_error,
        "byok_flash": byok_flash,
        "free_tier_configured": free_tier,
    }


def token(tid: int, label: str | None, last_used: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=tid,
        user_id=PARITY_USER,
        label=label,
        key_prefix=f"prep_pat_Ab…{tid:04d}",
        created_at=MONTH_AGO,
        last_used_at=last_used,
    )


QUESTION_FORM = {
    "type": "code",
    "topic": "python",
    "prompt": "Write add(a, b).",
    "choices": "",
    "answer": "def add(a, b):\n    return a + b",
    "rubric": "- returns the sum",
    "language": "python",
    "skeleton": "def add(a, b):\n    pass",
    "answer_regex": "",
}

MCQ_FORM = {
    "type": "mcq",
    "topic": "asia",
    "prompt": "Capital of Japan?",
    "choices": "Tokyo\nOsaka\nKyoto",
    "answer": "Tokyo",
    "rubric": "",
    "language": "",
    "skeleton": "",
    "answer_regex": "tokyo|tokio",
}


def import_outcome(
    name: str, *, inserted: int, dups: int = 0, errors: list[str] | None = None, **extra
) -> SimpleNamespace:
    return SimpleNamespace(
        deck_id=9,
        deck_name=name,
        inserted=inserted,
        skipped_duplicates=dups,
        errors=errors or [],
        **extra,
    )


# ---- the registry --------------------------------------------------------


def all_contexts() -> list[Ctx]:
    out: list[Ctx] = []

    # base.html: the masthead branches.
    out += [
        Ctx("base.html", "signed-in"),
        Ctx("base.html", "signed-in-clerk-badge", base={**CLERK, "notif_unseen_count": 3}),
        Ctx("base.html", "anonymous", base={"user": ANON_USER, **CLERK}),
        Ctx("base.html", "anonymous-no-sign-in", base={"user": ANON_USER}),
        Ctx("base.html", "signed-out", base={"user": None}),
        Ctx("base.html", "no-editor-mode", base={"user": {**USER, "editor_input_mode": None}}),
    ]

    # index.html
    empty_index = {
        "dashboard_overview": overview([], next_due=None),
        "menu_decks": [],
        "recent_sessions": [],
        "active_trivia_sessions": [],
        "snoozed_sessions": [],
    }
    populated_index = {
        "dashboard_overview": overview(OVERVIEW_DECKS),
        "menu_decks": MENU_DECKS,
        "recent_sessions": RECENT_SESSIONS,
        "active_trivia_sessions": ACTIVE_TRIVIA,
        "snoozed_sessions": SNOOZED,
    }
    xss_deck = menu_deck(7, "xss", XSS, total=1, due=1)
    out += [
        Ctx("index.html", "empty", empty_index),
        Ctx("index.html", "empty-no-agent", empty_index, base={"agent_available": False}),
        Ctx("index.html", "populated", populated_index),
        Ctx(
            "index.html",
            "decks-only",
            {
                **populated_index,
                "recent_sessions": [],
                "active_trivia_sessions": [],
                "snoozed_sessions": [],
            },
        ),
        Ctx(
            "index.html",
            "anonymous",
            {**populated_index, "dashboard_overview": overview(OVERVIEW_DECKS, anonymous=True)},
            base={"user": ANON_USER, **CLERK, "agent_available": False},
        ),
        Ctx(
            "index.html",
            "xss-deck-name",
            {
                "dashboard_overview": overview([overview_deck(xss_deck)]),
                "menu_decks": [xss_deck],
                "recent_sessions": [recent_session("s1parity00000042", "xss", XSS)],
                "active_trivia_sessions": [],
                "snoozed_sessions": [
                    {
                        "kind": "srs",
                        "id": "s1parity00000043",
                        "deck_name": "xss",
                        "deck_display": XSS,
                        "snoozed_until": DUE_SOON,
                    }
                ],
            },
        ),
    ]

    # deck.html
    srs_deck = {
        "deck_name": "capitals",
        "questions": SRS_CARDS,
        "deck_type": "srs",
        "deck_meta": deck_meta(1, pinned=True, display_name="World Capitals"),
        "trivia": None,
        "trivia_stats": None,
        "due_count": 2,
        "deck_retention": 0.95,
        "user_retention": 0.85,
        "retention_presets": RETENTION_PRESETS,
    }
    trivia_meta = deck_meta(
        3,
        display_name="World History Trivia",
        context_prompt=LONG_TOPIC,
        interval_minutes=45,
        session_size=5,
    )
    trivia_deck = {
        "deck_name": "history-trivia",
        "questions": TRIVIA_CARDS,
        "deck_type": "trivia",
        "deck_meta": trivia_meta,
        "trivia": trivia_meta,
        "trivia_stats": {"total": 2, "unanswered": 1, "wrong": 0, "mastered": 1},
        "due_count": 0,
        "deck_retention": None,
        "user_retention": None,
        "retention_presets": None,
    }
    out += [
        Ctx("deck.html", "srs-populated", srs_deck),
        Ctx(
            "deck.html",
            "srs-nothing-due",
            {
                **srs_deck,
                "due_count": 0,
                "deck_retention": None,
                "user_retention": None,
                "deck_meta": deck_meta(1, notifications_enabled=False),
            },
        ),
        Ctx(
            "deck.html",
            "srs-empty",
            {
                **srs_deck,
                "questions": [],
                "due_count": 0,
                "deck_retention": None,
                "deck_meta": deck_meta(1),
            },
        ),
        Ctx(
            "deck.html",
            "srs-empty-no-agent",
            {**srs_deck, "questions": [], "due_count": 0, "deck_meta": deck_meta(1)},
            base={"agent_available": False},
        ),
        Ctx("deck.html", "trivia", trivia_deck),
        Ctx(
            "deck.html",
            "trivia-paused-no-topic",
            {
                **trivia_deck,
                "deck_meta": deck_meta(3, notifications_enabled=False, interval_minutes=None),
                "trivia_stats": {"total": 0, "unanswered": 0, "wrong": 0, "mastered": 0},
                "questions": [],
            },
        ),
        Ctx(
            "deck.html",
            "xss-deck-name",
            {
                **srs_deck,
                "deck_name": "xss",
                "deck_meta": deck_meta(7, display_name=XSS),
                "questions": SRS_CARDS[:1],
            },
        ),
    ]

    # deck forms
    out += [
        Ctx(
            "deck_edit_ai.html", "srs", {"deck_name": "capitals", "deck_type": "srs", "error": None}
        ),
        Ctx(
            "deck_edit_ai.html",
            "trivia-error",
            {"deck_name": "history-trivia", "deck_type": "trivia", "error": "The AI is busy."},
        ),
        Ctx("deck_export.html", "srs", {"deck_name": "capitals", "deck_type": "srs"}),
        Ctx("deck_import_anki.html", "form", {"outcome": None, "error": None}),
        Ctx("deck_import_anki.html", "error", {"outcome": None, "error": "Pick a file to upload."}),
        Ctx(
            "deck_import_anki.html",
            "outcome-clean",
            {"outcome": import_outcome("anatomy", inserted=1, cloze_skipped=0), "error": None},
        ),
        Ctx(
            "deck_import_anki.html",
            "outcome-noisy",
            {
                "outcome": import_outcome(
                    "anatomy",
                    inserted=12,
                    dups=3,
                    cloze_skipped=2,
                    errors=["note 7: empty front", "note 9: empty back"],
                ),
                "error": None,
            },
        ),
        Ctx("deck_import_csv.html", "form", {"outcome": None, "error": None}),
        Ctx(
            "deck_import_csv.html",
            "error",
            {"outcome": None, "error": "Deck name must be 2-30 chars."},
        ),
        Ctx(
            "deck_import_csv.html",
            "outcome-clean",
            {"outcome": import_outcome("capitals", inserted=1), "error": None},
        ),
        Ctx(
            "deck_import_csv.html",
            "outcome-noisy",
            {
                "outcome": import_outcome(
                    "capitals", inserted=4, dups=1, errors=["row 3: missing answer"]
                ),
                "error": None,
            },
        ),
        Ctx("deck_import_prepdeck.html", "form", {"outcome": None, "error": None}),
        Ctx(
            "deck_import_prepdeck.html",
            "error",
            {"outcome": None, "error": "A deck named capitals already exists."},
        ),
        Ctx(
            "deck_import_prepdeck.html",
            "outcome-full",
            {
                "outcome": import_outcome(
                    "capitals-restore",
                    inserted=5,
                    dups=1,
                    reviews_inserted=12,
                    queue_rows_inserted=3,
                    errors=["card 9: unknown type"],
                ),
                "error": None,
            },
        ),
        Ctx(
            "deck_import_prepdeck.html",
            "outcome-nothing",
            {
                "outcome": import_outcome(
                    "capitals-restore", inserted=0, reviews_inserted=0, queue_rows_inserted=0
                ),
                "error": None,
            },
        ),
        Ctx("deck_new_chooser.html", "default", {}),
        Ctx("deck_new_srs.html", "agent", {"name_value": "", "context_value": "", "error": None}),
        Ctx(
            "deck_new_srs.html",
            "no-agent",
            {"name_value": "", "context_value": "", "error": None},
            base={"agent_available": False},
        ),
        Ctx(
            "deck_new_srs.html",
            "error",
            {
                "name_value": "World Capitals",
                "context_value": "Every capital city.",
                "error": "A deck with that name exists.",
            },
        ),
        Ctx(
            "deck_new_trivia.html",
            "agent",
            {"name_value": "", "topic_value": "", "interval_value": 30, "error": None},
        ),
        Ctx(
            "deck_new_trivia.html",
            "no-agent",
            {"name_value": "", "topic_value": "", "interval_value": 30, "error": None},
            base={"agent_available": False},
        ),
        Ctx(
            "deck_new_trivia.html",
            "error",
            {
                "name_value": "Pub Quiz",
                "topic_value": "1990s music",
                "interval_value": 90,
                "error": "Interval must be 1-720.",
            },
        ),
        Ctx(
            "deck_split.html",
            "srs",
            {
                "deck_name": "capitals",
                "deck_type": "srs",
                "cards": SRS_CARDS,
                "source_topic": "",
                "error": None,
                "form": {"new_name": "", "new_topic": "", "selected_ids": set()},
            },
        ),
        Ctx(
            "deck_split.html",
            "trivia-error",
            {
                "deck_name": "history-trivia",
                "deck_type": "trivia",
                "cards": TRIVIA_CARDS,
                "source_topic": LONG_TOPIC,
                "error": "Pick at least one card.",
                "form": {"new_name": "renaissance", "new_topic": "", "selected_ids": {51}},
            },
        ),
        Ctx(
            "deck_split.html",
            "empty",
            {
                "deck_name": "distsys",
                "deck_type": "srs",
                "cards": [],
                "source_topic": "",
                "error": None,
                "form": {"new_name": "", "new_topic": "", "selected_ids": set()},
            },
        ),
    ]

    # error.html
    out += [
        Ctx(
            "error.html",
            "404",
            {
                "status_code": 404,
                "headline": "Not found.",
                "blurb": "We couldn't find what you were looking for. Maybe a typo, or the link is stale.",
                "path": "/deck/missing",
            },
        ),
        Ctx(
            "error.html",
            "429",
            {
                "status_code": 429,
                "headline": "Busy right now.",
                "blurb": "More requests than the service can take at the moment.",
                "path": "/deck/capitals/transform",
            },
        ),
        Ctx(
            "error.html",
            "500",
            {
                "status_code": 500,
                "headline": "Something broke.",
                "blurb": "Sorry — that's on our end. The error has been logged.",
                "path": "/_parity/raise",
            },
        ),
        Ctx(
            "error.html",
            "bare",
            {"status_code": 403, "headline": "Forbidden.", "blurb": "", "path": ""},
            base={"user": None},
        ),
    ]

    # landing.html
    landing = {"instant_enabled": True, "topic_placeholder": "the French Revolution"}
    out += [
        Ctx("landing.html", "instant", landing, base={"user": None, **CLERK}),
        Ctx(
            "landing.html",
            "marketing-sign-in",
            {**landing, "instant_enabled": False},
            base={"user": None, **CLERK},
        ),
        Ctx("landing.html", "marketing-no-sign-in", landing, base={"user": None}),
    ]

    # notify
    out += [
        Ctx(
            "notify_settings.html",
            "off-no-devices",
            {"prefs": prefs("off"), "devices": 0, "vapid_key": "BParityVapidKey"},
        ),
        Ctx(
            "notify_settings.html",
            "digest-one-device",
            {"prefs": prefs("digest"), "devices": 1, "vapid_key": "BParityVapidKey"},
        ),
        Ctx(
            "notify_settings.html",
            "when-ready-quiet",
            {
                "prefs": prefs("when-ready", quiet=True),
                "devices": 2,
                "vapid_key": "BParityVapidKey",
            },
        ),
        Ctx("notify/log.html", "empty", {"entries": []}),
        Ctx(
            "notify/log.html",
            "entries",
            {
                "entries": [
                    notif_entry(3, "trivia", "2026-03-14T14:59:30+00:00", seen=False),
                    notif_entry(2, "srs-when-ready", "2026-03-14T12:00:00+00:00"),
                    notif_entry(1, "srs-digest", MONTH_AGO),
                    notif_entry(0, "manual", "2024-12-01T09:00:00+00:00"),
                ]
            },
        ),
    ]

    out.append(Ctx("offline.html", "default", {"build": PARITY_BUILD_ID}, base={"user": None}))

    # plan
    for label, progress in PLAN_STATUSES.items():
        ctx = {"wid": "plan-capitals-PARITY01", "deck_name": "capitals", "progress": progress}
        out.append(Ctx("plan.html", label, ctx))
        out.append(Ctx("partials/plan_progress.html", label, ctx))

    out.append(Ctx("privacy.html", "default", {}, base={"user": None}))

    # question forms
    q41 = SRS_CARDS[0]
    out += [
        Ctx(
            "question_edit.html",
            "code",
            {"deck_name": "distsys", "q": SRS_CARDS[3], "form": QUESTION_FORM, "error": None},
        ),
        Ctx(
            "question_edit.html",
            "mcq-error",
            {
                "deck_name": "capitals",
                "q": q41,
                "form": MCQ_FORM,
                "error": "Choices must include the answer.",
            },
        ),
        Ctx("question_new.html", "empty", {"deck_name": "capitals", "form": {}, "error": None}),
        Ctx(
            "question_new.html",
            "error",
            {"deck_name": "capitals", "form": MCQ_FORM, "error": "Prompt is required."},
        ),
    ]

    out.append(Ctx("reauth.html", "default", {}, base={"user": None, **CLERK}))

    # reorganize
    reorg_decks = [
        {"name": "capitals", "deck_type": "srs", "total": 5, "topic": ""},
        {"name": "distsys", "deck_type": "srs", "total": 1, "topic": LONG_TOPIC},
        {"name": "history-trivia", "deck_type": "trivia", "total": 2, "topic": "world history"},
    ]
    out += [
        Ctx(
            "reorganize.html",
            "decks",
            {"decks": reorg_decks, "form": {"prompt": ""}, "error": None},
        ),
        Ctx("reorganize.html", "empty", {"decks": [], "form": {"prompt": ""}, "error": None}),
        Ctx(
            "reorganize.html",
            "error",
            {
                "decks": reorg_decks,
                "form": {"prompt": "merge everything"},
                "error": "The AI is busy.",
            },
        ),
    ]

    # settings
    out += [
        Ctx("settings_account.html", "clean", {"error": None}, base=CLERK),
        Ctx(
            "settings_account.html",
            "error",
            {"error": "That doesn't match your account ID."},
            base=CLERK,
        ),
        Ctx(
            "settings_account.html",
            "no-display-name",
            {"error": None},
            base={**CLERK, "user": {**USER, "display_name": None}},
        ),
        Ctx("settings_agent.html", "none", settings_agent_ctx(), base={"agent_available": False}),
        Ctx("settings_agent.html", "connected", settings_agent_ctx(connected=True)),
        Ctx("settings_agent.html", "free-tier", settings_agent_ctx(free_tier=True)),
        Ctx(
            "settings_agent.html",
            "free-tier-with-key",
            settings_agent_ctx(connected=True, free_tier=True),
        ),
        Ctx(
            "settings_agent.html",
            "deploy-wide",
            settings_agent_ctx(logged_in=True, flash="Connected."),
        ),
        Ctx(
            "settings_agent.html",
            "deploy-wide-error",
            settings_agent_ctx(
                error="Token rejected.", byok_error="Key rejected.", byok_flash="Saved."
            ),
        ),
        Ctx("settings_agent.html", "clerk", settings_agent_ctx(connected=True), base=CLERK),
        Ctx(
            "settings_api.html",
            "no-tokens",
            {"tokens": [], "created_plaintext": None, "flash": None},
        ),
        Ctx(
            "settings_api.html",
            "tokens",
            {
                "tokens": [token(1, "laptop", EARLIER), token(2, None, None)],
                "created_plaintext": None,
                "flash": "Token revoked.",
            },
        ),
        Ctx(
            "settings_api.html",
            "created",
            {
                "tokens": [token(3, "claude desktop", None)],
                "created_plaintext": "prep_pat_PARITYPLAINTEXT0000000000000000",
                "flash": None,
            },
        ),
    ]
    for mode in ("vanilla", "vim", "emacs"):
        out.append(Ctx("settings_editor.html", mode, {"current_mode": mode, "saved": False}))
    out.append(Ctx("settings_editor.html", "saved", {"current_mode": "vim", "saved": True}))
    for value, _label, _blurb in RETENTION_PRESETS:
        out.append(
            Ctx(
                "settings_srs.html",
                f"{int(value * 100)}",
                {"current": value, "presets": RETENTION_PRESETS, "saved": False},
            )
        )
    out.append(
        Ctx(
            "settings_srs.html",
            "saved",
            {"current": 0.90, "presets": RETENTION_PRESETS, "saved": True},
        )
    )
    out.append(
        Ctx(
            "settings_srs.html",
            "off-preset",
            {"current": 0.93, "presets": RETENTION_PRESETS, "saved": False},
        )
    )

    out.append(
        Ctx(
            "sign_out_interstitial.html",
            "default",
            {"redirect_url": "/"},
            base={"user": None, **CLERK},
        )
    )

    out += [
        Ctx(
            "study_shell.html",
            "session",
            {"deck_name": "capitals", "session_id": "s1parity00000001", "sign_in_url": SIGN_IN},
        ),
        Ctx(
            "study_shell.html",
            "no-session",
            {"deck_name": "capitals", "session_id": None, "sign_in_url": None},
        ),
    ]

    out += transform_contexts()
    for label, d in DIFF_CARD_STATES.items():
        out.append(Ctx("partials/transform_diff_card.html", label, {"d": d}))

    # trivia
    for label, progress in TRIVIA_GEN_STATUSES.items():
        ctx = {"wid": "trivia-gen-PARITY01", "deck_name": "history-trivia", "progress": progress}
        out.append(Ctx("trivia/generating.html", label, ctx))
        out.append(Ctx("partials/trivia_generating_progress.html", label, ctx))
    out += [
        Ctx("trivia/card.html", "standalone", trivia_card_ctx(TRIVIA_Q, None, session=False)),
        Ctx(
            "trivia/card.html",
            "session-question",
            trivia_card_ctx(TRIVIA_Q_EXPLAINED, None, session=True),
        ),
        Ctx(
            "trivia/card.html",
            "standalone-right",
            trivia_card_ctx(
                TRIVIA_Q,
                trivia_result(correct=True, given="Leonardo da Vinci"),
                session=False,
                **EXPLORE,
            ),
        ),
        Ctx(
            "trivia/card.html",
            "session-right-feedback",
            trivia_card_ctx(
                TRIVIA_Q_EXPLAINED,
                trivia_result(correct=True, feedback="Close enough: the surname is optional."),
                session=True,
                **EXPLORE,
            ),
        ),
        Ctx(
            "trivia/card.html",
            "session-wrong-dispute",
            trivia_card_ctx(
                TRIVIA_Q, trivia_result(correct=False, given="Raphael"), session=True, **EXPLORE
            ),
        ),
        Ctx(
            "trivia/card.html",
            "session-wrong-last-card",
            {
                **trivia_card_ctx(
                    TRIVIA_Q, trivia_result(correct=False, given="Raphael"), session=True
                ),
                "session_remaining": "",
                "session_done": "",
            },
        ),
        Ctx(
            "trivia/card.html",
            "session-idk",
            trivia_card_ctx(
                TRIVIA_Q_EXPLAINED, trivia_result(correct=False, given="", idk=True), session=True
            ),
        ),
        Ctx(
            "trivia/card.html",
            "standalone-empty-answer",
            trivia_card_ctx(TRIVIA_Q, trivia_result(correct=False, given=""), session=False),
        ),
        Ctx(
            "trivia/card.html",
            "regraded",
            trivia_card_ctx(
                TRIVIA_Q,
                trivia_result(
                    correct=True,
                    regraded=True,
                    regex_updated=True,
                    feedback="Accepted on re-grade.",
                ),
                session=True,
                **EXPLORE,
            ),
        ),
        Ctx(
            "trivia/card.html",
            "overridden",
            trivia_card_ctx(
                TRIVIA_Q, trivia_result(correct=True, overridden=True), session=True, **EXPLORE
            ),
        ),
        Ctx(
            "trivia/card.html",
            "regex-updated",
            trivia_card_ctx(
                TRIVIA_Q, trivia_result(correct=True, regex_updated=True), session=False, **EXPLORE
            ),
        ),
        Ctx(
            "trivia/session_done.html",
            "results",
            {
                "deck_name": "history-trivia",
                "results": session_done_results(),
                "right_count": 1,
                "total": 2,
            },
        ),
        Ctx(
            "trivia/session_done.html",
            "empty",
            {"deck_name": "history-trivia", "results": [], "right_count": 0, "total": 0},
        ),
    ]

    # partials
    out += [
        Ctx("partials/deck_menus.html", "decks", {"menu_decks": MENU_DECKS}),
        Ctx(
            "partials/deck_menus.html",
            "decks-no-agent",
            {"menu_decks": MENU_DECKS},
            base={"agent_available": False},
        ),
        Ctx("partials/deck_menus.html", "empty", {"menu_decks": []}),
        Ctx("partials/deck_menus.html", "xss-deck-name", {"menu_decks": [xss_deck]}),
        Ctx(
            "partials/deck_overflow_menu.html",
            "srs-inline",
            {
                "deck_name": "capitals",
                "deck_type": "srs",
                "deck_meta": deck_meta(1, pinned=True),
                "has_cards": True,
                "delete_inline": True,
            },
        ),
        Ctx(
            "partials/deck_overflow_menu.html",
            "trivia-link",
            {
                "deck_name": "history-trivia",
                "deck_type": "trivia",
                "deck_meta": trivia_meta,
                "has_cards": False,
            },
            base={"agent_available": False},
        ),
        Ctx("partials/notif_edit.html", "enabled-defaults", {"deck_meta": deck_meta(3)}),
        Ctx(
            "partials/notif_edit.html",
            "paused-custom",
            {
                "deck_meta": deck_meta(
                    3, notifications_enabled=False, interval_minutes=90, session_size=10
                )
            },
        ),
        Ctx(
            "partials/notif_edit.html",
            "hourly",
            {"deck_meta": deck_meta(3, interval_minutes=120, session_size=1)},
        ),
        Ctx("partials/pin_form.html", "pinned", {"deck_name": "capitals", "pinned": True}),
        Ctx("partials/pin_form.html", "unpinned", {"deck_name": "capitals", "pinned": False}),
        Ctx("partials/pwa_install_nudge.html", "default", {}),
        Ctx("partials/sheet_duration.html", "default", {}),
        Ctx("partials/workflow_badge.html", "empty", {"workflows": []}),
        Ctx(
            "partials/workflow_badge.html",
            "active",
            {
                "workflows": [
                    workflow("w1", "plan", "planning", url_path="/plan/w1"),
                    workflow(
                        "w2",
                        "trivia_gen",
                        "generating",
                        deck_name="history-trivia",
                        display=None,
                        url_path="/trivia/gen/w2",
                    ),
                ]
            },
        ),
        Ctx(
            "partials/workflow_badge.html",
            "one-active",
            {"workflows": [workflow("w1", "grading", "asking_ai", url_path="/session/x")]},
        ),
        Ctx(
            "partials/workflow_badge.html",
            "all-done",
            {
                "workflows": [
                    workflow("w1", "transform", "done", url_path="/transform/w1"),
                    workflow("w2", "plan", "failed", url_path="/plan/w2"),
                ]
            },
        ),
        Ctx(
            "partials/workflow_badge.html",
            "mixed",
            {
                "workflows": [
                    workflow(
                        "w1",
                        "transform",
                        "awaiting_apply",
                        deck_name=None,
                        display=None,
                        url_path="/transform/w1",
                    ),
                    workflow("w2", "plan", "awaiting_feedback", url_path="/plan/w2"),
                    workflow(
                        "w3",
                        "transform",
                        "computing",
                        deck_name="distsys",
                        display=None,
                        url_path="/transform/w3",
                    ),
                    workflow("w4", "trivia_gen", "rejected", url_path="/trivia/gen/w4"),
                    workflow("w5", "grading", "", url_path="/session/s"),
                ]
            },
        ),
    ]
    return out
