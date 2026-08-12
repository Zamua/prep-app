"""The signed-in study loop end to end: the real study shell, the real
online host, the real JSON API, a real database.

The component suite (test_study_components_e2e.py) proves the views
against a mocked API; this proves the wiring the mock cannot: that the
shell boots the host, that the host's calls match what the server
actually serves, and that answering really advances the session.

The seeded deck and its server are session-scoped and mutate as cards
are answered, so this file keeps ONE flow test rather than several that
would depend on each other's leftovers.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.browser]


OFFLINE_E2E_LOGIN = "offline-e2e@example.com"
OFFLINE_E2E_NAME = "Offline Tester"


@pytest.fixture
def online_page(browser_session, offline_server):
    """An authed page with service workers BLOCKED at the context.

    This suite navigates repeatedly, and once a service worker
    controls the origin it re-issues navigations itself; Playwright's
    route interception does not reach those, so the injected identity
    header is dropped and every navigation 401s. Blocking service
    workers is the harness fix, not a product one: real deploys
    authenticate with cookies or a proxy header, which the browser
    sends on service-worker fetches too. The study shell has no
    service-worker dependency (unlike the offline shell, whose suite
    keeps them on deliberately)."""
    ctx = browser_session.new_context(
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Mobile/15E148 Safari/604.1"
        ),
        viewport={"width": 393, "height": 852},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
        service_workers="block",
    )
    ctx.set_default_timeout(15_000)
    ctx.set_default_navigation_timeout(15_000)
    base = offline_server.base_url

    def _inject_header(route, request):
        if request.url.startswith(base):
            route.continue_(
                headers={
                    **request.headers,
                    "tailscale-user-login": OFFLINE_E2E_LOGIN,
                    "tailscale-user-name": OFFLINE_E2E_NAME,
                }
            )
        else:
            route.continue_()

    ctx.route("**/*", _inject_header)
    page = ctx.new_page()
    try:
        yield page
    finally:
        ctx.close()


def _study_text(page) -> str:
    return page.locator("[data-study-root]").inner_text().lower()


def test_signed_in_study_loop_runs_on_the_shared_components(offline_server, online_page):
    """Land on the deck's study URL, answer the mcq, read the verdict,
    advance, and survive a reload. Every screen is a shared component
    driven by ServerSource against the real API."""
    page = online_page
    offline_server.start()
    base = offline_server.base_url

    page.goto(f"{base}/study/offline-e2e")

    # The server sent a shell; the host filled it from the API.
    page.wait_for_selector(".study-card")
    assert "capital of france?" in _study_text(page)
    # Most-overdue first, the same ordering rule the offline queue uses.
    assert page.locator("label.choice").count() == 3

    page.locator("label.choice", has_text="Paris").click()
    page.get_by_role("button", name="Submit").click()

    page.wait_for_selector("h1.verdict-headline")
    assert page.locator("h1.verdict-headline").inner_text() == "Right."
    # Section eyebrows render uppercase via CSS, so compare case-blind.
    verdict = _study_text(page)
    assert "the question" in verdict
    assert "capital of france?" in verdict
    assert "next review in" in verdict
    # An mcq verdict marks the grid instead of printing a model answer.
    assert "your pick" in verdict
    assert "correct" in verdict

    # Advancing moves the session server-side, not just the view.
    page.get_by_role("button", name="Next card").click()
    page.wait_for_selector(".study-card")
    after_advance = _study_text(page)
    assert "capital of france?" not in after_advance

    # A reload resumes the same card: session state lives on the server,
    # so the shell rebuilds the identical screen.
    page.reload()
    page.wait_for_selector(".study-card")
    assert page.locator(".study-prompt").inner_text().lower() in after_advance


def test_study_shell_carries_no_card_content(offline_server, online_page):
    """The shell is a mount point: card text arrives from the API, so
    the served HTML must not contain it. Pins that the server template
    was really replaced rather than kept alongside.

    Asserts on the NAVIGATION RESPONSE, not page.content(): the host
    replaces the mount as soon as it boots, so the live DOM no longer
    shows what the server sent."""
    page = online_page
    offline_server.start()
    base = offline_server.base_url

    response = page.goto(f"{base}/study/offline-e2e")
    body = response.text()
    assert "data-study-root" in body
    assert "Studying needs JavaScript enabled" in body
    assert "Capital of France?" not in body
