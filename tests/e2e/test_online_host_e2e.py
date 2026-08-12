"""The real online host, driven through the real study shell, with the
API mocked at the network boundary.

Why this file exists: test_study_components_e2e.py mounts the views
through a hand-written harness, so it proves the VIEWS but cannot fail
on a defect inside online-host.js (its harness re-derives the branch
order and threads its own answer through). These tests load
`/study/<deck>` and let the shell boot the actual host, so the
assertions run through the host's own `apply` and `handleError`.

Mocking the API rather than the upstream keeps the paths that matter
here (self-grade, grading failure) reachable without a Temporal worker.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.browser]

DECK = "offline-e2e"

_CARD = {
    "question_id": 4242,
    "deck_id": 1,
    "type": "short",
    "prompt": "Define quorum.",
    "choices": None,
    "skeleton": None,
    "language": None,
}

_SESSION = {"id": "sess-host", "version": 3, "state": "awaiting-answer", "status": "active"}


@pytest.fixture
def host_page(browser_session, offline_server):
    """A page on the local server with service workers blocked, so the
    injected identity header survives navigation (see
    test_online_study_e2e for the full reasoning)."""
    ctx = browser_session.new_context(
        viewport={"width": 393, "height": 852},
        is_mobile=True,
        has_touch=True,
        service_workers="block",
    )
    ctx.set_default_timeout(15_000)
    base = offline_server.base_url

    def _inject(route, request):
        if request.url.startswith(base):
            route.continue_(
                headers={
                    **request.headers,
                    "tailscale-user-login": "offline-e2e@example.com",
                    "tailscale-user-name": "Offline Tester",
                }
            )
        else:
            route.continue_()

    ctx.route("**/*", _inject)
    page = ctx.new_page()
    try:
        yield page
    finally:
        ctx.close()


def _json_route(page, pattern, handler):
    """Route an API path to a python handler returning (status, body)."""

    def _fulfil(route):
        status, body = handler(route.request)
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(body),
        )

    page.route(pattern, _fulfil)


def test_self_grade_records_what_the_user_actually_wrote(offline_server, host_page):
    """The self-grade verdict must carry the user's own text.

    The host used to read a key the server never sends, so every
    self-graded review was stored with an empty answer. Asserting on
    the submitted body is what makes that visible."""
    page = host_page
    offline_server.start()
    base = offline_server.base_url
    submits: list[dict] = []

    def _begin(_req):
        return 200, {"card": _CARD, "draft": "", "session": _SESSION}

    def _submit(req):
        body = json.loads(req.post_data or "{}")
        submits.append(body)
        if "verdict" in body:
            return 200, {
                "verdict": body["verdict"],
                "nextDueMinutes": 10,
                "idk": False,
                "answer": body.get("answer", ""),
                "card": _CARD,
                "session": {**_SESSION, "state": "showing-result"},
            }
        # First submit: no deterministic grader for this card.
        return 200, {
            "selfGrade": True,
            "answer": body.get("answer", ""),
            "card": {**_CARD, "answer": "A majority of replicas."},
            "session": _SESSION,
        }

    _json_route(page, "**/api/study/decks/*/session", _begin)
    _json_route(page, "**/api/study/sessions/*/submit", _submit)

    page.goto(f"{base}/study/{DECK}")
    page.wait_for_selector(".study-card")
    page.locator("[data-study-root] textarea").first.fill("more than half the replicas")
    page.get_by_role("button", name="Submit").click()

    # Reveal screen: the user's words, not the model answer.
    page.wait_for_selector(".study-card")
    revealed = page.locator("[data-study-root]").inner_text().lower()
    assert "more than half the replicas" in revealed
    assert "a majority of replicas" in revealed

    page.get_by_role("button", name="Right").first.click()
    page.wait_for_selector("h1.verdict-headline")

    verdict_submit = [b for b in submits if "verdict" in b]
    assert verdict_submit, f"no verdict submit recorded; got {submits}"
    assert verdict_submit[0]["answer"] == "more than half the replicas"


def test_a_failed_grade_stops_instead_of_looping(offline_server, host_page):
    """A grading workflow that dies must not spin the loop.

    The server keeps answering {pending} while the session sits in
    'grading', so a host that recovers by re-reading would poll
    forever. The bounded recovery has to end at a screen with a way
    out."""
    page = host_page
    offline_server.start()
    base = offline_server.base_url
    polls = {"n": 0}

    wedged = {"on": False}
    pending_body = {
        "pending": {"poll": f"{base}/api/study/grading/grade-x-q4242-abc", "workflow_id": "w"},
        "session": {**_SESSION, "state": "grading"},
    }

    def _begin(_req):
        return 200, {"card": _CARD, "draft": "", "session": _SESSION}

    def _next(_req):
        # A server stuck in 'grading' keeps handing back pending. That
        # is exactly the state that made the loop spin, so the client
        # must survive it on its own.
        if wedged["on"]:
            return 200, pending_body
        return 200, {"card": _CARD, "draft": "", "session": _SESSION}

    def _submit(_req):
        wedged["on"] = True
        return 200, pending_body

    def _poll(_req):
        polls["n"] += 1
        return 200, {"failed": {"code": "grading_failed", "message": "the grader returned nothing"}}

    _json_route(page, "**/api/study/decks/*/session", _begin)
    _json_route(page, "**/api/study/sessions/*/next", _next)
    _json_route(page, "**/api/study/sessions/*/submit", _submit)
    _json_route(page, "**/api/study/grading/**", _poll)

    page.goto(f"{base}/study/{DECK}")
    page.wait_for_selector(".study-card")
    page.locator("[data-study-root] textarea").first.fill("something")
    page.get_by_role("button", name="Submit").click()

    # Give an unbounded loop plenty of room to prove itself.
    page.wait_for_timeout(6_000)
    text = page.locator("[data-study-root]").inner_text().lower()
    assert "stuck" in text, f"expected the dead-end screen, got: {text[:200]!r}"
    # A way out, not just an apology.
    assert page.get_by_role("button", name="Back to deck").count() == 1
    assert polls["n"] <= 6, f"polled {polls['n']} times: the loop is not bounded"


def test_verdict_offers_the_chat_handoff(offline_server, host_page):
    """The server composes prefilled provider URLs on every verdict;
    the verdict screen has to actually show them."""
    page = host_page
    offline_server.start()
    base = offline_server.base_url

    def _begin(_req):
        return 200, {"card": _CARD, "draft": "", "session": _SESSION}

    def _submit(_req):
        return 200, {
            "verdict": "right",
            "nextDueMinutes": 10,
            "idk": False,
            "answer": "quorum",
            "card": _CARD,
            "session": {**_SESSION, "state": "showing-result"},
            "handoff": {
                "message": "Discuss this card...",
                "urls": {
                    "claude": "https://claude.ai/new?q=x",
                    "chatgpt": "https://chatgpt.com/?q=x",
                },
                "providers": {"claude": "Claude", "chatgpt": "ChatGPT"},
                "default": "claude",
            },
        }

    _json_route(page, "**/api/study/decks/*/session", _begin)
    _json_route(page, "**/api/study/sessions/*/submit", _submit)

    page.goto(f"{base}/study/{DECK}")
    page.wait_for_selector(".study-card")
    page.locator("[data-study-root] textarea").first.fill("quorum")
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector("h1.verdict-headline")

    discuss = page.locator("[data-study-root] .discuss")
    assert discuss.count() == 1, "the verdict screen dropped the chat handoff"
    assert (
        page.locator("[data-study-root] .discuss-option").first.get_attribute("href")
        == "https://claude.ai/new?q=x"
    )
