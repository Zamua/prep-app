"""API contracts oracle: `{name, request, response}` pairs for every
route under the JSON and MCP surfaces, recorded against the reader
profile, plus the anonymous cookie lifecycle and `openapi.json`.

Route coverage is asserted from `app.routes`: every route under the
listed prefixes must be hit by at least one pair, so a new route
fails the extractor until it is recorded.

`VOLATILE` names the values another implementation cannot reproduce
(the PAT secret, the VAPID key, zip timestamps inside an `.apkg`,
minted cookie values and slugs); the corpus test compares those by
regex and everything else exactly.
"""

from __future__ import annotations

import base64
import csv
import fnmatch
import io
import json
import re
from datetime import timedelta

from tests.parity.oracles import PARITY_NOW, dump_json, write_corpus
from tests.parity.oracles.harness import Harness, jsonable, scratch_app
from tests.parity.oracles.seed import seed_reader

NAME = "contracts"

PREFIXES = (
    "/api/study",
    "/api/dashboard",
    "/api/offline",
    "/api/instant",
    "/notify",
    "/api/active-workflows-badge",
    "/api/v1",
    "/mcp",
)
EXTRA_ROUTES = (("GET", "/openapi.json"), ("POST", "/forget-device"))

JSON = {"accept": "application/json"}
ORIGIN = {"origin": "https://parity.example.test"}
IP_SIGNED_IN = {"x-real-ip": "198.51.100.7"}
IP_VISITOR = {"x-real-ip": "198.51.100.8"}
IP_SECOND = {"x-real-ip": "198.51.100.9"}

# (pair-name glob, dotted pointer into the pair, regex) -> `<VOLATILE>`.
VOLATILE: tuple[tuple[str, str, str], ...] = (
    ("*", "request.headers.authorization", r"prep_pat_[A-Za-z0-9_-]+"),
    ("*", "request.headers.cookie", r"prep_anon=[^;]+"),
    ("*", "response.set_cookie.*", r"prep_anon=v1\.[^;]+"),
    ("*", "response.set_cookie.*", r"expires=[^;]+"),
    ("settings-api-mint-token", "response.text", r"prep_pat_[A-Za-z0-9_-]+"),
    ("notify-*", "response.json.key", r"^.+$"),
    ("notify-page*", "response.text", r'vapidKey: "[^"]*"'),
    ("instant-*", "response.json.redirect", r"/deck/[a-z0-9]+"),
    (
        "mcp-call-prep_export_deck_apkg",
        "response.json.result.content.0.text",
        r'"apkg_base64": "[^"]+",\n  "byte_count": \d+',
    ),
    ("mcp-call-prep_import_apkg", "request.json.params.arguments.apkg_base64", r"^.+$"),
)

INSTANT_DECK = json.dumps(
    [
        {"q": "Year the Bastille fell?", "a": "1789", "r": r"1789"},
        {"q": "The Estates-General had how many estates?", "a": "three", "r": r"three|3"},
        {"q": "Who was executed in January 1793?", "a": "Louis XVI", "r": r"louis (xvi|16)"},
        {
            "q": "Robespierre led which committee?",
            "a": "Committee of Public Safety",
            "r": r"(committee of )?public safety",
        },
        {"q": "The Directory fell to whom?", "a": "Napoleon", "r": r"napoleon( bonaparte)?"},
    ]
)


def _rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _tool(name: str, args: dict | None = None) -> dict:
    return _rpc("tools/call", {"name": name, "arguments": args or {}}, req_id=7)


def _csv(rows: list[dict]) -> str:
    from prep.decks.io import CSV_COLUMNS

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})
    return buf.getvalue()


def _cookie_value(set_cookie: list[str]) -> str:
    for header in set_cookie:
        m = re.match(r"prep_anon=([^;]+)", header)
        if m:
            return m.group(1)
    raise AssertionError(f"no prep_anon in {set_cookie}")


def record_dashboard(h: Harness, H: dict) -> None:
    h.call("dashboard-overview", "GET", "/api/dashboard/overview", headers=H)
    h.call("dashboard-deck-menus", "GET", "/api/dashboard/deck-menus", headers=H)
    h.call("dashboard-overview-unauthenticated", "GET", "/api/dashboard/overview", headers=JSON)
    h.call("workflow-badge", "GET", "/api/active-workflows-badge", headers=H)


def record_offline(h: Harness, H: dict, ids: dict) -> None:
    h.call("offline-snapshot", "GET", "/api/offline/snapshot", headers=H)
    h.call(
        "offline-sync",
        "POST",
        "/api/offline/sync",
        headers=H,
        json_body={
            "device_id": "parity-device",
            "new_cards": [
                {"client_id": "contract-card", "prompt": "Capital of Peru?", "answer": "Lima"}
            ],
            "reviews": [
                {
                    "client_id": "contract-review",
                    "question_id": ids["questions"]["raft"],
                    "verdict": "right",
                    "user_answer": "leader",
                    "graded_by": "auto",
                    "reviewed_at": (PARITY_NOW - timedelta(minutes=5)).isoformat(),
                }
            ],
        },
    )


def record_study(h: Harness, H: dict, ids: dict) -> None:
    q = ids["questions"]
    r = h.call("study-begin", "POST", "/api/study/decks/capitals/session", headers=H, json_body={})
    sid = r.json()["session"]["id"]
    version = r.json()["session"]["version"]
    h.call(
        "study-begin-resumes",
        "POST",
        "/api/study/decks/capitals/session",
        headers=H,
        json_body={"fresh": False},
    )
    h.call(
        "study-begin-trivia",
        "POST",
        "/api/study/decks/history-trivia/session",
        headers=H,
        json_body={},
    )
    h.call("study-next", "GET", f"/api/study/sessions/{sid}/next", headers=H)
    h.call("study-next-unknown", "GET", "/api/study/sessions/nope/next", headers=H)

    r = h.call(
        "study-draft",
        "POST",
        f"/api/study/sessions/{sid}/draft",
        headers=H,
        json_body={"version": version, "draft": "Par"},
    )
    version = r.json()["version"]
    h.call(
        "study-draft-stale",
        "POST",
        f"/api/study/sessions/{sid}/draft",
        headers=H,
        json_body={"version": 1, "draft": "old"},
    )
    h.call(
        "study-submit-no-version",
        "POST",
        f"/api/study/sessions/{sid}/submit",
        headers=H,
        json_body={"question_id": q["paris"], "idk": True},
    )
    h.call(
        "study-submit-unknown-question",
        "POST",
        f"/api/study/sessions/{sid}/submit",
        headers=H,
        json_body={"question_id": 999999, "version": version, "idk": True},
    )
    r = h.call(
        "study-submit-idk",
        "POST",
        f"/api/study/sessions/{sid}/submit",
        headers=H,
        json_body={"question_id": q["paris"], "version": version, "idk": True},
    )
    version = r.json()["session"]["version"]
    h.call("study-next-showing-result", "GET", f"/api/study/sessions/{sid}/next", headers=H)
    h.call(
        "study-advance-stale",
        "POST",
        f"/api/study/sessions/{sid}/advance",
        headers=H,
        json_body={"version": version - 1},
    )
    r = h.call(
        "study-advance",
        "POST",
        f"/api/study/sessions/{sid}/advance",
        headers=H,
        json_body={"version": version},
    )
    version = r.json()["session"]["version"]
    r = h.call(
        "study-submit-mcq-right",
        "POST",
        f"/api/study/sessions/{sid}/submit",
        headers=H,
        json_body={"question_id": q["tokyo"], "version": version, "answer": "Tokyo"},
    )
    version = r.json()["session"]["version"]
    r = h.call(
        "study-advance-2",
        "POST",
        f"/api/study/sessions/{sid}/advance",
        headers=H,
        json_body={"version": version},
    )
    version = r.json()["session"]["version"]
    r = h.call(
        "study-submit-multi-wrong",
        "POST",
        f"/api/study/sessions/{sid}/submit",
        headers=H,
        json_body={"question_id": q["andes"], "version": version, "answer": json.dumps(["Lima"])},
    )
    version = r.json()["session"]["version"]
    r = h.call(
        "study-advance-3",
        "POST",
        f"/api/study/sessions/{sid}/advance",
        headers=H,
        json_body={"version": version},
    )
    version = r.json()["session"]["version"]
    h.call(
        "study-submit-bad-verdict",
        "POST",
        f"/api/study/sessions/{sid}/submit",
        headers=H,
        json_body={"question_id": q["add"], "version": version, "verdict": "meh"},
    )
    r = h.call(
        "study-submit-self-verdict",
        "POST",
        f"/api/study/sessions/{sid}/submit",
        headers=H,
        json_body={"question_id": q["add"], "version": version, "verdict": "right"},
    )
    version = r.json()["session"]["version"]
    h.call(
        "study-advance-completes",
        "POST",
        f"/api/study/sessions/{sid}/advance",
        headers=H,
        json_body={"version": version},
    )
    h.call("study-next-completed", "GET", f"/api/study/sessions/{sid}/next", headers=H)

    r = h.call(
        "study-begin-fresh",
        "POST",
        "/api/study/decks/distsys/session",
        headers=H,
        json_body={"fresh": True},
    )
    sid2 = r.json()["session"]["id"]
    h.call(
        "study-snooze-preset",
        "POST",
        f"/api/study/sessions/{sid2}/snooze",
        headers=H,
        json_body={"preset": "1d"},
    )
    h.call(
        "study-snooze-custom",
        "POST",
        f"/api/study/sessions/{sid2}/snooze",
        headers=H,
        json_body={"custom": "3", "unit": "hours"},
    )
    h.call(
        "study-snooze-invalid",
        "POST",
        f"/api/study/sessions/{sid2}/snooze",
        headers=H,
        json_body={"preset": "someday"},
    )
    h.call(
        "study-snooze-wake",
        "POST",
        f"/api/study/sessions/{sid2}/snooze",
        headers=H,
        json_body={"preset": "wake"},
    )
    h.call("study-abandon", "POST", f"/api/study/sessions/{sid2}/abandon", headers=H)
    h.call("study-abandon-unknown", "POST", "/api/study/sessions/nope/abandon", headers=H)
    h.call("study-next-abandoned", "GET", f"/api/study/sessions/{sid2}/next", headers=H)

    h.call("study-deck-next", "GET", "/api/study/decks/distsys/next", headers=H)
    h.call("study-deck-next-caught-up", "GET", "/api/study/decks/capitals/next", headers=H)
    h.call("study-deck-next-trivia", "GET", "/api/study/decks/history-trivia/next", headers=H)
    h.call("study-deck-next-unknown", "GET", "/api/study/decks/nope/next", headers=H)
    h.call(
        "study-deck-submit-idk",
        "POST",
        "/api/study/decks/distsys/submit",
        headers=H,
        json_body={"question_id": q["quorum"], "idk": True},
    )
    h.call(
        "study-deck-submit-mcq-wrong",
        "POST",
        "/api/study/decks/distsys/submit",
        headers=H,
        json_body={"question_id": q["raft"], "answer": "follower"},
    )
    h.call(
        "study-deck-submit-unknown-deck",
        "POST",
        "/api/study/decks/nope/submit",
        headers=H,
        json_body={"question_id": q["quorum"], "idk": True},
    )

    h.call(
        "study-author-inbox",
        "POST",
        "/api/study/cards",
        headers=H,
        json_body={"prompt": "Capital of Norway?", "answer": "Oslo"},
    )
    h.call(
        "study-author-deck",
        "POST",
        "/api/study/cards",
        headers=H,
        json_body={
            "prompt": "Capital of Sweden?",
            "answer": "Stockholm",
            "deck_id": ids["decks"]["capitals"],
        },
    )
    h.call(
        "study-author-invalid",
        "POST",
        "/api/study/cards",
        headers=H,
        json_body={"prompt": "  ", "answer": "x"},
    )
    h.call(
        "study-author-trivia-deck",
        "POST",
        "/api/study/cards",
        headers=H,
        json_body={"prompt": "x", "answer": "y", "deck_id": ids["decks"]["history-trivia"]},
    )
    h.call(
        "study-author-unknown-deck",
        "POST",
        "/api/study/cards",
        headers=H,
        json_body={"prompt": "x", "answer": "y", "deck_id": 999999},
    )
    h.call(
        "study-author-malformed", "POST", "/api/study/cards", headers=H, json_body={"answer": "y"}
    )

    h.call("study-grading-malformed-wid", "GET", "/api/study/grading/not-a-grading-id", headers=H)
    h.call(
        "study-grading-foreign-question",
        "GET",
        "/api/study/grading/grade-capitals-q999999-abcdef",
        headers=H,
    )


def record_instant_and_cookies(h: Harness, H: dict) -> None:
    from prep.agent.fake import FakeAgent
    from prep.agent.port import AgentResult
    from prep.auth import anon_cookie
    from prep.instant import service as instant_service

    instant_service.set_instant_agent_factory(
        lambda max_output_tokens=None: FakeAgent(
            next_response=AgentResult(text=INSTANT_DECK, model="parity-model")
        )
    )
    try:
        h.call(
            "instant-invalid-topic",
            "POST",
            "/api/instant/generate",
            headers={**JSON, **IP_SIGNED_IN},
            json_body={"topic": ""},
        )
        h.call(
            "instant-not-json",
            "POST",
            "/api/instant/generate",
            headers={**JSON, **IP_SIGNED_IN, "content-type": "application/json"},
            content=b"nope",
        )
        h.call(
            "instant-signed-in",
            "POST",
            "/api/instant/generate",
            headers={**H, **IP_SIGNED_IN},
            json_body={"topic": "the French Revolution"},
        )
        h.call(
            "instant-signed-in-rate-limited",
            "POST",
            "/api/instant/generate",
            headers={**H, **IP_SIGNED_IN},
            json_body={"topic": "the French Revolution"},
        )
        r = h.call(
            "instant-visitor-mints",
            "POST",
            "/api/instant/generate",
            headers={**JSON, **IP_VISITOR},
            json_body={"topic": "  Roman   emperors\n"},
        )
        cookie = _cookie_value(r.headers.get_list("set-cookie"))
        h.call(
            "cookie-fresh-no-refresh",
            "GET",
            "/api/offline/snapshot",
            headers={**JSON, "cookie": f"prep_anon={cookie}"},
        )
        h.call(
            "instant-anonymous-second-deck",
            "POST",
            "/api/instant/generate",
            headers={**JSON, **IP_SECOND, "cookie": f"prep_anon={cookie}"},
            json_body={"topic": "Greek philosophers"},
        )
        h.clock.set(PARITY_NOW + timedelta(seconds=anon_cookie.REFRESH_AFTER_SECONDS + 1))
        r = h.call(
            "cookie-refreshed-after-window",
            "GET",
            "/api/offline/snapshot",
            headers={**JSON, "cookie": f"prep_anon={cookie}"},
            note=f"clock advanced by REFRESH_AFTER_SECONDS ({anon_cookie.REFRESH_AFTER_SECONDS}) + 1",
        )
        refreshed = _cookie_value(r.headers.get_list("set-cookie"))
        h.call(
            "cookie-refreshed-value-accepted",
            "GET",
            "/api/offline/snapshot",
            headers={**JSON, "cookie": f"prep_anon={refreshed}"},
        )
        h.call(
            "forget-device",
            "POST",
            "/forget-device",
            headers={**JSON, **ORIGIN, "cookie": f"prep_anon={refreshed}"},
        )
        h.call(
            "forget-device-cross-site",
            "POST",
            "/forget-device",
            headers={
                **JSON,
                "origin": "https://evil.example.test",
                "cookie": f"prep_anon={refreshed}",
            },
        )
        h.clock.set(PARITY_NOW)
        h.call(
            "cookie-from-the-future-cleared",
            "GET",
            "/api/offline/snapshot",
            headers={**JSON, "cookie": f"prep_anon={refreshed}"},
            note="clock back at PARITY_NOW; the refreshed iat is beyond FUTURE_SKEW_SECONDS",
        )
        tampered = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
        h.call(
            "cookie-bad-signature-cleared",
            "GET",
            "/api/offline/snapshot",
            headers={**JSON, "cookie": f"prep_anon={tampered}"},
        )
        h.call(
            "cookie-garbage-cleared",
            "GET",
            "/api/offline/snapshot",
            headers={**JSON, "cookie": "prep_anon=not-a-cookie"},
        )
    finally:
        instant_service.set_instant_agent_factory(None)


def record_notify(h: Harness, H: dict) -> None:
    h.call("notify-page", "GET", "/notify", headers=h.headers())
    h.call(
        "notify-prefs-save",
        "POST",
        "/notify/prefs",
        headers=H,
        json_body={"mode": "digest", "digest_hour": 8, "last_digest_date": "2020-01-01"},
    )
    h.call("notify-prefs-invalid", "POST", "/notify/prefs", headers=H, json_body={"mode": "hourly"})
    h.call("notify-prefs-not-object", "POST", "/notify/prefs", headers=H, json_body=[1, 2])
    h.call("notify-vapid-public-key", "GET", "/notify/vapid-public-key", headers=H)
    sub = {
        "endpoint": "https://push.example.test/parity",
        "keys": {"p256dh": "p256", "auth": "auth"},
    }
    h.call("notify-subscribe", "POST", "/notify/subscribe", headers=H, json_body=sub)
    h.call("notify-subscribe-bad", "POST", "/notify/subscribe", headers=H, json_body={"keys": {}})
    h.call("notify-page-one-device", "GET", "/notify", headers=h.headers())
    h.call(
        "notify-unsubscribe",
        "POST",
        "/notify/unsubscribe",
        headers=H,
        json_body={"endpoint": sub["endpoint"]},
    )
    h.call("notify-unsubscribe-missing", "POST", "/notify/unsubscribe", headers=H, json_body={})
    h.call("notify-test-no-devices", "POST", "/notify/test", headers=H)
    h.call("notify-log", "GET", "/notify/log", headers=h.headers())
    h.call("notify-log-all-seen", "GET", "/notify/log", headers=h.headers())
    h.call("notify-unauthenticated", "GET", "/notify/vapid-public-key", headers=JSON)


def record_api_and_mcp(h: Harness, H: dict) -> None:
    r = h.call(
        "settings-api-mint-token",
        "POST",
        "/settings/api/tokens",
        headers=h.headers(),
        data={"label": "parity"},
    )
    token = re.findall(r"prep_pat_[A-Za-z0-9_-]{30,}", r.text)[0]
    B = {**JSON, "authorization": f"Bearer {token}"}

    h.call("v1-decks-no-auth", "GET", "/api/v1/decks", headers=JSON)
    h.call(
        "v1-decks-bad-scheme",
        "GET",
        "/api/v1/decks",
        headers={**JSON, "authorization": "Basic abc"},
    )
    h.call(
        "v1-decks-bad-token",
        "GET",
        "/api/v1/decks",
        headers={**JSON, "authorization": "Bearer prep_pat_definitely_not_real_xxxxxxxxxxxxx"},
    )
    h.call("v1-decks-list", "GET", "/api/v1/decks", headers=B)
    h.call(
        "v1-decks-create",
        "POST",
        "/api/v1/decks",
        headers=B,
        json_body={"name": "api-deck", "context_prompt": "Made over the API."},
    )
    h.call(
        "v1-decks-create-duplicate",
        "POST",
        "/api/v1/decks",
        headers=B,
        json_body={"name": "api-deck"},
    )
    h.call("v1-decks-create-invalid", "POST", "/api/v1/decks", headers=B, json_body={"name": "x"})
    h.call("v1-deck-meta", "GET", "/api/v1/decks/api-deck", headers=B)
    h.call("v1-deck-meta-unknown", "GET", "/api/v1/decks/nope", headers=B)
    h.call("v1-deck-cards", "GET", "/api/v1/decks/capitals/cards", headers=B)
    h.call("v1-deck-cards-unknown", "GET", "/api/v1/decks/nope/cards", headers=B)
    h.call("v1-deck-export-csv", "GET", "/api/v1/decks/capitals/export.csv", headers=B)
    h.call("v1-deck-export-csv-unknown", "GET", "/api/v1/decks/nope/export.csv", headers=B)
    rows = [
        {
            "type": "short",
            "topic": "africa",
            "prompt": "Capital of Ghana?",
            "answer": "Accra",
            "answer_regex": "accra",
        },
        {
            "type": "mcq",
            "prompt": "Capital of Egypt?",
            "answer": "Cairo",
            "choices": "Cairo\nGiza\nLuxor",
        },
        {"type": "short", "prompt": "Capital of Ghana?", "answer": "Accra"},
        {"type": "essay", "prompt": "Bad type", "answer": "x"},
    ]
    h.call(
        "v1-deck-import-csv",
        "POST",
        "/api/v1/decks/api-deck/import-csv",
        headers={**B, "content-type": "text/csv"},
        content=_csv(rows),
    )
    h.call(
        "v1-deck-import-csv-empty",
        "POST",
        "/api/v1/decks/api-deck/import-csv",
        headers={**B, "content-type": "text/csv"},
        content=b"  \n",
    )

    h.call("mcp-no-auth", "POST", "/mcp", headers=JSON, json_body=_rpc("tools/list"))
    h.call(
        "mcp-parse-error",
        "POST",
        "/mcp",
        headers={**B, "content-type": "application/json"},
        content=b"{",
    )
    h.call("mcp-invalid-request", "POST", "/mcp", headers=B, json_body=[1])
    h.call(
        "mcp-initialize",
        "POST",
        "/mcp",
        headers=B,
        json_body=_rpc("initialize", {"protocolVersion": "2025-06-18"}),
    )
    h.call(
        "mcp-notifications-initialized",
        "POST",
        "/mcp",
        headers=B,
        json_body={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    h.call("mcp-tools-list", "POST", "/mcp", headers=B, json_body=_rpc("tools/list", req_id=2))
    h.call(
        "mcp-unknown-method", "POST", "/mcp", headers=B, json_body=_rpc("resources/list", req_id=3)
    )
    h.call("mcp-unknown-tool", "POST", "/mcp", headers=B, json_body=_tool("prep_nope"))
    h.call(
        "mcp-call-prep_list_decks", "POST", "/mcp", headers=B, json_body=_tool("prep_list_decks")
    )
    h.call(
        "mcp-call-prep_get_deck",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_get_deck", {"name": "capitals"}),
    )
    h.call(
        "mcp-call-prep_get_deck-missing-name",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_get_deck", {}),
    )
    h.call(
        "mcp-call-prep_get_deck-unknown",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_get_deck", {"name": "nope"}),
    )
    h.call(
        "mcp-call-prep_list_cards",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_list_cards", {"name": "capitals"}),
    )
    h.call(
        "mcp-call-prep_export_deck_csv",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_export_deck_csv", {"name": "capitals"}),
    )
    h.call(
        "mcp-call-prep_create_deck",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool(
            "prep_create_deck", {"name": "mcp-deck", "context_prompt": "Made over MCP."}
        ),
    )
    h.call(
        "mcp-call-prep_create_deck-duplicate",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_create_deck", {"name": "mcp-deck"}),
    )
    h.call(
        "mcp-call-prep_import_csv",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_import_csv", {"name": "mcp-deck", "csv": _csv(rows[:2])}),
    )
    h.call(
        "mcp-call-prep_rename_deck",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_rename_deck", {"name": "mcp-deck", "new_name": "mcp-renamed"}),
    )
    h.call(
        "mcp-call-prep_set_deck_pinned",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_set_deck_pinned", {"name": "mcp-renamed", "pinned": True}),
    )
    h.call(
        "mcp-call-prep_set_topic_prompt",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool(
            "prep_set_topic_prompt", {"name": "mcp-renamed", "context_prompt": "African capitals."}
        ),
    )
    r = h.call(
        "mcp-call-prep_add_card",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool(
            "prep_add_card",
            {
                "deck": "mcp-renamed",
                "type": "mcq",
                "prompt": "Capital of Kenya?",
                "answer": "Nairobi",
                "choices": ["Nairobi", "Mombasa"],
                "topic": "africa",
            },
        ),
    )
    card_id = json.loads(r.json()["result"]["content"][0]["text"])["id"]
    h.call(
        "mcp-call-prep_add_card-mcq-no-choices",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool(
            "prep_add_card",
            {"deck": "mcp-renamed", "type": "mcq", "prompt": "No choices?", "answer": "x"},
        ),
    )
    h.call(
        "mcp-call-prep_get_card",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_get_card", {"card_id": card_id}),
    )
    h.call(
        "mcp-call-prep_get_card-unknown",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_get_card", {"card_id": 999999}),
    )
    h.call(
        "mcp-call-prep_update_card",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool(
            "prep_update_card",
            {
                "card_id": card_id,
                "type": "short",
                "prompt": "Capital of Kenya?",
                "answer": "Nairobi",
                "answer_regex": "nairobi",
                "explanation": "Since 1963.",
            },
        ),
    )
    h.call(
        "mcp-call-prep_suspend_card",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_suspend_card", {"card_id": card_id, "suspended": True}),
    )
    h.call(
        "mcp-call-prep_export_deck_apkg",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_export_deck_apkg", {"name": "mcp-renamed"}),
    )
    apkg_b64 = json.loads(h.recorded[-1]["response"]["json"]["result"]["content"][0]["text"])[
        "apkg_base64"
    ]
    base64.b64decode(apkg_b64)
    h.call(
        "mcp-call-prep_import_apkg",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_import_apkg", {"name": "mcp-restored", "apkg_base64": apkg_b64}),
    )
    h.call(
        "mcp-call-prep_import_apkg-bad-base64",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_import_apkg", {"name": "mcp-restored", "apkg_base64": "not base64!"}),
    )
    h.call(
        "mcp-call-prep_delete_card",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_delete_card", {"card_id": card_id}),
    )
    h.call(
        "mcp-call-prep_delete_deck",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_delete_deck", {"name": "mcp-renamed"}),
    )
    h.call(
        "mcp-call-prep_delete_deck-unknown",
        "POST",
        "/mcp",
        headers=B,
        json_body=_tool("prep_delete_deck", {"name": "mcp-renamed"}),
    )
    h.call("v1-decks-list-after", "GET", "/api/v1/decks", headers=B)


def _walk_routes(routes):
    """Flatten included routers (FastAPI lists them as one entry
    holding its own `routes`) down to the leaf routes."""
    for route in routes:
        router = getattr(route, "original_router", None)
        if router is not None:
            yield from _walk_routes(router.routes)
        else:
            yield route


def covered_routes(app, pairs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Every route under the prefixes, and the subset no pair hit."""
    from starlette.routing import Match, Route

    routes = [
        r
        for r in _walk_routes(app.routes)
        if isinstance(r, Route)
        and (r.path.startswith(PREFIXES) or any(r.path == p for _, p in EXTRA_ROUTES))
    ]
    hit: set[tuple[str, str]] = set()
    for pair in pairs:
        method, path = pair["request"]["method"], pair["request"]["path"]
        scope = {"type": "http", "method": method, "path": path, "root_path": ""}
        for route in routes:
            match, _ = route.matches(scope)
            if match is Match.FULL:
                hit.add((method, route.path))
    listed = [
        {"method": m, "path": r.path, "name": r.name}
        for r in routes
        for m in sorted(r.methods or [])
        if m not in ("HEAD", "OPTIONS")
    ]
    missing = [entry for entry in listed if (entry["method"], entry["path"]) not in hit]
    return listed, missing


def extract() -> dict[str, str]:
    with scratch_app() as h:
        ids = seed_reader()
        H = {**h.headers(), **JSON}
        record_dashboard(h, H)
        record_offline(h, H, ids)
        record_study(h, H, ids)
        record_instant_and_cookies(h, H)
        record_notify(h, H)
        record_api_and_mcp(h, H)
        h.call("openapi", "GET", "/openapi.json", headers=JSON)
        h.call("workflow-badge-unauthenticated", "GET", "/api/active-workflows-badge", headers=JSON)
        pairs = jsonable(h.recorded)
        listed, missing = covered_routes(h.client.app, pairs)
    assert not missing, f"routes with no recorded pair: {missing}"
    names = [p["name"] for p in pairs]
    assert len(names) == len(set(names)), "pair names must be unique"
    for pair in pairs:
        assert pair["response"]["status"] < 500, (pair["name"], pair["response"]["status"])
    header = {
        "profile": "reader",
        "ids": ids,
        "routes": listed,
        "volatile": [{"pairs": g, "pointer": p, "regex": r} for g, p, r in VOLATILE],
        "comparison": (
            "JSON responses compared as parsed values, HTML responses by DOM equivalence, "
            "volatile pointers by regex"
        ),
    }
    return {"pairs.json": dump_json({"header": header, "pairs": pairs})}


def is_volatile_match(name: str, glob: str) -> bool:
    return fnmatch.fnmatchcase(name, glob)


def main() -> None:
    root = write_corpus(NAME, extract())
    print(f"wrote {root}")


if __name__ == "__main__":
    main()
