"""The real online host, driven through the real study shell, with the
API mocked at the network boundary.

Why this file exists: test_study_components_e2e.py mounts the views
through a hand-written harness, so it proves the VIEWS but cannot fail
on a defect inside online-host.js (its harness re-derives the branch
order and threads its own answer through). These tests load
`/study/<deck>` and let the shell boot the actual host, so the
assertions run through the host's own `apply` and `handleError`.

Mocking the API rather than the upstream keeps the paths that matter
here (self-grade, grading failure) reachable with no agent funded.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.celld_node import OFFLINE_E2E_LOGIN, OFFLINE_E2E_NAME, identity_headers

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
                headers={**request.headers, **identity_headers(OFFLINE_E2E_LOGIN, OFFLINE_E2E_NAME)}
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

    # Reveal screen: the user's words, not the model answer. Waited on by
    # the blurb only that screen carries: `.study-card` is already on the
    # page, so it would match the pre-submit DOM.
    page.wait_for_selector(".study-card .offline-selfgrade-blurb")
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


_CODE_CARD = {
    "question_id": 909,
    "deck_id": 1,
    "type": "code",
    "prompt": "Write a function that reverses a slice in place.",
    "choices": None,
    "skeleton": "func reverse(xs []int) {\n\t// your code\n}\n",
    "language": "go",
}


def test_code_cards_get_the_editor_back(offline_server, host_page):
    """A `code` card mounts CodeMirror over the textarea, with the
    toolbar (input mode, copy, reset). The swap to shared components
    dropped this entirely; the textarea remains the value carrier so
    the answer still submits either way."""
    page = host_page
    offline_server.start()
    base = offline_server.base_url
    submits: list[dict] = []

    def _begin(_req):
        # The server seeds an untouched code card's draft with the
        # skeleton (prep/study/api.py), so the mock does too.
        return 200, {"card": _CODE_CARD, "draft": _CODE_CARD["skeleton"], "session": _SESSION}

    def _submit(req):
        submits.append(json.loads(req.post_data or "{}"))
        return 200, {
            "selfGrade": True,
            "answer": submits[-1].get("answer", ""),
            "card": {**_CODE_CARD, "answer": "reverse in place"},
            "session": _SESSION,
        }

    _json_route(page, "**/api/study/decks/*/session", _begin)
    _json_route(page, "**/api/study/sessions/*/submit", _submit)

    page.goto(f"{base}/study/{DECK}")
    page.wait_for_selector(".study-card")

    # The editor mounts asynchronously over the textarea.
    page.wait_for_selector(".cm-mount .cm-editor", timeout=15_000)
    assert page.locator(".code-action-mode select").count() == 1
    assert page.locator("button.code-action.btn-async").count() == 1
    # Skeleton present on this card, so the reset control is offered.
    assert page.locator("button.code-action").count() == 2
    # The skeleton seeded the editor.
    assert "func reverse" in page.locator(".cm-mount .cm-content").inner_text()

    # Typing in CodeMirror reaches the textarea the form submits.
    page.locator(".cm-mount .cm-content").click()
    page.keyboard.type("// done")
    page.get_by_role("button", name="Submit").click()
    page.wait_for_selector(".study-card")
    assert submits, "nothing was submitted"
    assert "// done" in submits[0]["answer"]


def test_pending_screen_shows_what_the_grader_reported(offline_server, host_page):
    """A grader that is running but unhappy (a busy shared tier telling
    the user to add their own key) must reach the screen. The old
    polling fragment printed it on every poll; the pending payload
    carries it now."""
    page = host_page
    offline_server.start()
    base = offline_server.base_url
    note = "free tier is busy - add your own key in Settings"

    def _begin(_req):
        return 200, {"card": _CARD, "draft": "", "session": _SESSION}

    def _submit(_req):
        return 200, {
            "pending": {
                "poll": f"{base}/api/study/grading/grade-x-q4242-abc",
                "workflow_id": "w",
                "error": note,
            },
            "session": {**_SESSION, "state": "grading"},
        }

    def _poll(_req):
        return 200, {
            "pending": {
                "poll": f"{base}/api/study/grading/grade-x-q4242-abc",
                "workflow_id": "w",
                "status": "grading",
                "error": note,
            }
        }

    _json_route(page, "**/api/study/decks/*/session", _begin)
    _json_route(page, "**/api/study/sessions/*/submit", _submit)
    _json_route(page, "**/api/study/grading/**", _poll)

    page.goto(f"{base}/study/{DECK}")
    page.wait_for_selector(".study-card")
    page.locator("[data-study-root] textarea").first.fill("something")
    page.get_by_role("button", name="Submit").click()

    page.wait_for_selector(".grading-panel")
    assert note in page.locator(".grading-panel").inner_text()
