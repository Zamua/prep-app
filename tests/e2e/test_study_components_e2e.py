"""Browser side of the study components against a mocked study API.

The offline suite already drives the components through LocalSource.
This module drives the same components through ServerSource, with
every /api/study/* request fulfilled by a page route so the shapes
under test are the wire shapes and nothing depends on a job engine or
an agent.

What it pins:
  - the async-grading path end to end in the DOM: submit renders the
    pending panel, the polling loop keeps it up while the workflow
    runs, and the verdict view replaces it when the grade lands
  - a deterministic verdict never shows the pending panel
  - the typed errors a host branches on: grading_failed, stale_version
    (with the server's version adopted), unauthorized, network
  - Pause during a grade stops the polling loop
  - a session resumed on a verdict advances before serving a card

The harness mounts the real modules into a #harness element and wires
them the way a host does; assertions read the real DOM under it. Every
locator is scoped to #harness so the offline shell that owns the rest
of the page cannot answer for the components under test.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.browser]

DECK = "study-components-e2e"
SID = "sess-e2e-1"
WID = "grade-study-components-e2e-77"

GRADING_PREFIX = f"/api/study/grading/{WID}"

_SHORT_CARD = {
    "question_id": 77,
    "deck_id": 5,
    "type": "short",
    "prompt": "Why does a write-ahead log help durability?",
    "choices": None,
    "skeleton": None,
    "language": None,
}

_MCQ_CARD = {
    "question_id": 78,
    "deck_id": 5,
    "type": "mcq",
    "prompt": "Capital of France?",
    "choices": ["Paris", "Lyon", "Marseille"],
    "skeleton": None,
    "language": None,
}

_REVEALED_SHORT = {
    **_SHORT_CARD,
    "answer": "It makes the intent durable before the data pages move.",
    "rubric": None,
}

_REVEALED_MCQ = {**_MCQ_CARD, "answer": "Paris", "rubric": None}


def _session(version: int, state: str = "awaiting-answer", status: str = "active") -> dict:
    return {
        "id": SID,
        "version": version,
        "status": status,
        "state": state,
        "deck_id": 5,
        "deck_name": DECK,
    }


def _pending(status: str | None = None) -> dict:
    payload = {"poll": f"{GRADING_PREFIX}?sid={SID}", "workflow_id": WID}
    if status:
        payload["status"] = status
    return payload


def _verdict(card: dict, answer: str = "", minutes: int = 1440, version: int = 3) -> dict:
    return {
        "verdict": "right",
        "feedback": "reads right",
        "nextDueMinutes": minutes,
        "idk": False,
        "answer": answer,
        "card": card,
        "session": _session(version, state="showing-result"),
    }


# ---- API mock ----------------------------------------------------------


def _install_api(page, respond):
    """Fulfill every /api/study/* request from `respond`.

    `respond(method, path, body, calls)` returns (status, payload), or
    the string "abort" to fail the request the way a dead network does.
    Page routes are matched ahead of the context's header-injecting
    catch-all, so the app's own routes stay untouched.
    """
    calls: list[dict] = []

    def handler(route, request):
        raw = request.post_data
        body = json.loads(raw) if raw else None
        parsed = urlparse(request.url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        calls.append({"method": request.method, "path": path, "body": body})
        try:
            result = respond(request.method, path, body, calls)
        except Exception as e:  # noqa: BLE001 - an unfulfilled route hangs the page
            result = (500, {"error": {"code": "harness", "message": f"{path}: {e}"}})
        if result == "abort":
            route.abort("failed")
            return
        status, payload = result
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/study/**", handler)
    return calls


def _unexpected(method: str, path: str):
    return 500, {"error": {"code": "unexpected_call", "message": f"{method} {path}"}}


def _polls(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["path"].startswith(GRADING_PREFIX)]


# ---- page harness ------------------------------------------------------

# Mounts ServerSource + the study components and drives them the way a
# host does: begin, render whatever view the source returns, and on a
# {pending} outcome show the pending panel while awaiting settled.
# Errors are recorded on window.__error rather than thrown, so a test
# can read the code a host would branch on.
_MOUNT_JS = """
async ({prefix, deck}) => {
  const src = await import(prefix + "study/source.js");
  const c = await import(prefix + "study/components.js");

  const host = document.createElement("div");
  host.id = "harness";
  document.body.appendChild(host);

  window.__views = [];
  window.__error = null;
  const source = new src.ServerSource({deck});
  window.__source = source;

  const show = (node, name) => {
    host.replaceChildren(node);
    window.__views.push(name);
  };
  const fail = (e) => {
    window.__error = {
      name: e && e.name,
      code: e && e.code,
      message: e && e.message,
      currentVersion: e && e.currentVersion === undefined ? null : e.currentVersion,
    };
    window.__views.push("error:" + (e && e.code));
  };

  const app = {
    // Key order matters: a verdict payload carries the revealed card
    // alongside, so testing `card` first would read it as a fresh card.
    render(view) {
      if (view.pending) return app.pending(null, view.pending, "");
      if (view.verdict) return app.verdict(view);
      if (view.caughtUp) {
        return show(c.caughtUpView(view.caughtUp, {onAdd: () => {}, onBack: () => {}}), "caughtup");
      }
      if (view.card) return app.card(view.card);
      window.__views.push("unhandled");
    },
    card(card) {
      show(
        c.studyCardView(card, {
          onPause: () => window.__views.push("paused"),
          onAnswer: async (answer) => {
            try {
              const out = await source.submit(card, {answer});
              if (out.pending) return app.pending(card, out.pending, answer);
              if (out.selfGrade) {
                return show(
                  c.revealView(out.card || card, answer, {
                    onVerdict: () => {},
                    onPause: () => {},
                  }),
                  "reveal"
                );
              }
              app.verdict(out);
            } catch (e) {
              fail(e);
            }
          },
          onIdk: async () => {
            try {
              app.verdict(await source.submit(card, {idk: true}));
            } catch (e) {
              fail(e);
            }
          },
        }),
        "card"
      );
    },
    pending(card, pending, answer) {
      show(c.pendingView(card, pending, {onPause: () => window.__views.push("paused")}), "pending");
      pending.settled.then((out) => app.verdict({...out, answer: out.answer || answer}), fail);
    },
    verdict(out) {
      show(
        c.verdictView(
          out.card,
          out.verdict,
          out.answer || "",
          {minutes: out.nextDueMinutes, idk: Boolean(out.idk), scheduleNote: null},
          {onNext: () => {}, onPause: () => {}}
        ),
        "verdict"
      );
    },
  };
  window.__app = app;

  try {
    app.render(await source.begin());
  } catch (e) {
    fail(e);
  }
  return window.__views.slice();
}
"""


def _module_prefix(page) -> str:
    """The versioned "@/" URL prefix from the shell's importmap, so
    imports resolve inside evaluate()'d scripts."""
    prefix = page.evaluate(
        "() => JSON.parse(document.querySelector('script[type=importmap]').textContent)"
        ".imports['@/']"
    )
    assert prefix, "shell importmap missing the '@/' entry"
    return prefix


def _mount(page, base_url: str):
    page.goto(base_url + "/offline")
    return page.evaluate(_MOUNT_JS, {"prefix": _module_prefix(page), "deck": DECK})


def _harness(page):
    return page.locator("#harness")


def _views(page) -> list[str]:
    return page.evaluate("() => window.__views")


def _error(page):
    return page.evaluate("() => window.__error")


def _version(page):
    return page.evaluate("() => window.__source.session && window.__source.session.version")


# ---- the async grading path -------------------------------------------


def test_submit_shows_pending_then_polls_to_verdict(offline_server, offline_page):
    """The path the {pending} contract shape exists for: a free-text
    answer starts a workflow, the pending panel holds the screen while
    the source polls, and the verdict view replaces it when the grade
    lands. The panel must be in the DOM before the verdict is, which is
    what the ordered waits pin."""
    offline_server.start()  # idempotent; heals a prior test's failure state
    page = offline_page

    def respond(method, path, body, calls):
        if path.endswith(f"/decks/{DECK}/session"):
            return 200, {"card": _SHORT_CARD, "draft": "", "session": _session(1)}
        if path.endswith(f"/sessions/{SID}/submit"):
            assert body["question_id"] == _SHORT_CARD["question_id"]
            assert body["version"] == 1, "submit must carry the session version"
            return 200, {"pending": _pending(), "session": _session(2, state="grading")}
        if path.startswith(GRADING_PREFIX):
            if len(_polls(calls)) < 3:
                return 200, {"pending": _pending(status="grading")}
            return 200, _verdict(_REVEALED_SHORT, answer="It orders the intent ahead of the data.")
        return _unexpected(method, path)

    calls = _install_api(page, respond)
    _mount(page, offline_server.base_url)
    h = _harness(page)

    h.locator(".study-card textarea").wait_for()
    h.locator("textarea").fill("It orders the intent ahead of the data.")
    h.get_by_role("button", name="Submit").click()

    # Pending first, verdict second: the order of these waits is the pin.
    panel = h.locator(".grading-panel")
    panel.wait_for()
    panel_text = panel.inner_text()
    assert "Reading your answer" in panel_text
    assert "Grading" in panel_text
    assert h.locator(".grading-panel .grading-spinner .dot").count() == 3
    assert h.locator("h1.verdict-headline").count() == 0

    h.locator("h1.verdict-headline").wait_for(timeout=30_000)
    assert h.locator("h1.verdict-headline").inner_text() == "Right."
    assert "1 day" in h.locator(".verdict-sub").inner_text()
    assert h.locator(".grading-panel").count() == 0
    # The revealed card arrives with the verdict, so the compare
    # sections render the model answer the poll delivered.
    assert "It makes the intent durable" in h.inner_text()

    assert _views(page) == ["card", "pending", "verdict"], _views(page)
    assert _error(page) is None

    # More than one poll: the loop ran, rather than one lucky request.
    assert len(_polls(calls)) == 3, [c["path"] for c in calls]
    assert _version(page) == 3


def test_deterministic_verdict_never_shows_pending(offline_server, offline_page):
    """An mcq grades inside the request, so submit answers with the
    verdict outcome and the pending panel must never mount."""
    offline_server.start()
    page = offline_page

    def respond(method, path, body, calls):
        if path.endswith(f"/decks/{DECK}/session"):
            return 200, {"card": _MCQ_CARD, "draft": "", "session": _session(1)}
        if path.endswith(f"/sessions/{SID}/submit"):
            assert body["answer"] == "Paris"
            return 200, _verdict(_REVEALED_MCQ, answer="Paris")
        return _unexpected(method, path)

    _install_api(page, respond)
    _mount(page, offline_server.base_url)
    h = _harness(page)

    h.locator(".study-card .choices").wait_for()
    h.locator("label.choice", has_text="Paris").click()
    h.get_by_role("button", name="Submit").click()
    h.locator("h1.verdict-headline").wait_for()
    assert h.locator("h1.verdict-headline").inner_text() == "Right."
    assert _views(page) == ["card", "verdict"]
    assert _error(page) is None


def test_grading_failure_settles_as_a_typed_error(offline_server, offline_page):
    """A workflow that finishes without a verdict rejects the pending
    promise with the server's code instead of hanging the panel."""
    offline_server.start()
    page = offline_page

    def respond(method, path, body, calls):
        if path.endswith(f"/decks/{DECK}/session"):
            return 200, {"card": _SHORT_CARD, "draft": "", "session": _session(1)}
        if path.endswith(f"/sessions/{SID}/submit"):
            return 200, {"pending": _pending(), "session": _session(2, state="grading")}
        if path.startswith(GRADING_PREFIX):
            return 200, {"failed": {"code": "grading_failed", "message": "the grader gave up"}}
        return _unexpected(method, path)

    _install_api(page, respond)
    _mount(page, offline_server.base_url)
    h = _harness(page)

    h.locator(".study-card textarea").wait_for()
    h.locator("textarea").fill("a guess")
    h.get_by_role("button", name="Submit").click()
    h.locator(".grading-panel").wait_for()

    page.wait_for_function("() => window.__error !== null", timeout=30_000)
    err = _error(page)
    assert err["name"] == "StudySourceError"
    assert err["code"] == "grading_failed"
    assert "the grader gave up" in err["message"]
    assert _views(page) == ["card", "pending", "error:grading_failed"]


def test_pause_during_grading_stops_the_polling_loop(offline_server, offline_page):
    """Pausing mid-grade cancels the client loop; the workflow keeps
    running server-side. No poll may fire after the cancel."""
    offline_server.start()
    page = offline_page

    def respond(method, path, body, calls):
        if path.endswith(f"/decks/{DECK}/session"):
            return 200, {"card": _SHORT_CARD, "draft": "", "session": _session(1)}
        if path.endswith(f"/sessions/{SID}/submit"):
            return 200, {"pending": _pending(), "session": _session(2, state="grading")}
        if path.startswith(GRADING_PREFIX):
            return 200, {"pending": _pending(status="grading")}
        return _unexpected(method, path)

    calls = _install_api(page, respond)
    _mount(page, offline_server.base_url)
    h = _harness(page)

    h.locator(".study-card textarea").wait_for()
    h.locator("textarea").fill("a guess")
    h.get_by_role("button", name="Submit").click()
    h.locator(".grading-panel").wait_for()
    h.locator("button.back").click()

    page.wait_for_function("() => window.__error !== null", timeout=30_000)
    assert _error(page)["code"] == "cancelled"
    assert "paused" in _views(page)
    at_cancel = len(_polls(calls))
    page.wait_for_timeout(3000)
    assert len(_polls(calls)) == at_cancel, "the loop kept polling after cancel"


# ---- the transport failures a host branches on -------------------------


def test_stale_version_surfaces_and_adopts_the_server_version(offline_server, offline_page):
    """A session moved on another device answers 409. The host gets a
    stale_version error carrying the server's version, and the source
    adopts it so the next request is not stale for the same reason."""
    offline_server.start()
    page = offline_page

    def respond(method, path, body, calls):
        if path.endswith(f"/decks/{DECK}/session"):
            return 200, {"card": _SHORT_CARD, "draft": "", "session": _session(1)}
        if path.endswith(f"/sessions/{SID}/submit"):
            return 409, {
                "error": {
                    "code": "stale_version",
                    "message": "session moved on another device",
                    "current_version": 9,
                }
            }
        return _unexpected(method, path)

    _install_api(page, respond)
    _mount(page, offline_server.base_url)
    h = _harness(page)

    h.locator(".study-card textarea").wait_for()
    h.locator("textarea").fill("a guess")
    h.get_by_role("button", name="Submit").click()
    page.wait_for_function("() => window.__error !== null")
    err = _error(page)
    assert err["code"] == "stale_version"
    assert err["currentVersion"] == 9
    assert _version(page) == 9


def test_unauthorized_and_network_failures_are_typed(offline_server, offline_page):
    """A dead session (401) and an unreachable network are different
    codes, so a host can send one to sign-in and retry the other."""
    offline_server.start()
    page = offline_page
    mode = {"value": "unauthorized"}

    def respond(method, path, body, calls):
        if path.endswith(f"/decks/{DECK}/session"):
            return 200, {"card": _SHORT_CARD, "draft": "", "session": _session(1)}
        if path.endswith(f"/sessions/{SID}/submit"):
            if mode["value"] == "abort":
                return "abort"
            return 401, {"detail": "not authenticated"}
        return _unexpected(method, path)

    _install_api(page, respond)
    _mount(page, offline_server.base_url)
    h = _harness(page)

    h.locator(".study-card textarea").wait_for()
    h.locator("textarea").fill("a guess")
    h.get_by_role("button", name="Submit").click()
    page.wait_for_function("() => window.__error !== null")
    assert _error(page)["code"] == "unauthorized"

    mode["value"] = "abort"
    page.evaluate("() => { window.__error = null; }")
    h.get_by_role("button", name="Submit").click()
    page.wait_for_function("() => window.__error !== null")
    assert _error(page)["code"] == "network"


def test_session_resumed_on_a_verdict_advances_before_the_next_card(offline_server, offline_page):
    """A session picked up while parked on a verdict must advance past
    it: reading the same session again would hand back that verdict
    forever, so next() posts advance instead."""
    offline_server.start()
    page = offline_page

    def respond(method, path, body, calls):
        if path.endswith(f"/decks/{DECK}/session"):
            return 200, _verdict(_REVEALED_SHORT, answer="earlier answer", version=4)
        if path.endswith(f"/sessions/{SID}/advance"):
            assert method == "POST"
            assert body["version"] == 4
            return 200, {"card": _MCQ_CARD, "draft": "", "session": _session(5)}
        return _unexpected(method, path)

    calls = _install_api(page, respond)
    _mount(page, offline_server.base_url)
    h = _harness(page)

    h.locator("h1.verdict-headline").wait_for()
    page.evaluate("async () => { window.__app.render(await window.__source.next()); }")
    h.locator(".study-card .choices").wait_for()
    assert h.locator(".study-prompt").inner_text() == "Capital of France?"
    assert calls[-1]["path"].endswith(f"/sessions/{SID}/advance")
    assert _version(page) == 5
