"""Client-side smoke against a LOCAL celld node, so the importmap
regression class has coverage that runs without a deploy.

test_browser_smoke.py covers the same class plus the htmx transform
flows, but those need a funded deploy, so that suite only runs against
a deployed target and skips without credentials. The bug it guards (an
inline `<script type="module">` that stops parsing, taking every
behavior in that block with it) needs nothing but a real server and a
real browser, which the local harness already provides. Keeping it
here means a regression is caught on any machine, at any time, rather
than only when someone points the suite at the fleet.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.browser]

# Pages whose inline module scripts must parse and execute. The study
# shell is the newest member: it boots the study loop from an inline
# `import "@/study/online-host.js"`, so a broken importmap or a bad
# module path leaves a permanently empty mount.
_PAGES = [
    ("index", "/"),
    ("deck", "/deck/offline-e2e"),
    ("study-shell", "/study/offline-e2e"),
    ("offline-shell", "/offline"),
]


def _collect(page):
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed: list[str] = []

    page.on(
        "console",
        lambda m: console_errors.append(m.text) if m.type == "error" else None,
    )
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on(
        "requestfailed",
        lambda r: failed.append(r.url) if "/static/" in r.url else None,
    )
    return console_errors, page_errors, failed


@pytest.mark.parametrize("page_id,path", _PAGES)
def test_inline_module_scripts_execute(offline_server, offline_page, page_id, path):
    offline_server.start()
    page = offline_page
    console_errors, page_errors, failed = _collect(page)

    page.goto(f"{offline_server.base_url}{path}", wait_until="domcontentloaded")
    page.wait_for_timeout(1_500)

    assert not page_errors, f"{page_id}: uncaught page errors: {page_errors}"
    assert not failed, f"{page_id}: failed static requests: {failed}"
    # The offline shell probes the snapshot API while signed out on the
    # landing path; that 401 is by design and not a module failure.
    real_errors = [e for e in console_errors if "401" not in e and "Unauthorized" not in e]
    assert not real_errors, f"{page_id}: console errors: {real_errors}"


def test_study_shell_boots_its_host(offline_server, offline_page):
    """The strongest form of the importmap pin for the study surface:
    not just 'no errors' but 'the inline module actually ran', proven
    by the mount being replaced with a real card."""
    offline_server.start()
    page = offline_page
    page.goto(f"{offline_server.base_url}/study/offline-e2e", wait_until="domcontentloaded")
    page.wait_for_selector(".study-card", timeout=15_000)
    assert "Loading your cards" not in page.locator("[data-study-root]").inner_text()


def test_dashboard_boots_its_host(offline_server, offline_page):
    """Same pin for the dashboard: the deck list is client-rendered, so
    the shell's fallback note has to be gone and real rows in its
    place."""
    offline_server.start()
    page = offline_page
    page.goto(f"{offline_server.base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector(".deck-list .deck-card", timeout=15_000)
    assert page.locator("[data-dashboard-fallback]").count() == 0


def test_dashboard_says_so_when_its_module_never_loads(offline_server, offline_page):
    """The failure the client-rendered dashboard cannot otherwise
    report. A module that 404s or times out fires no error the region
    can see and <noscript> does not apply (scripting IS on), so a
    silent shell reads as "you have no decks". The fallback note is
    the only thing standing between the user and that."""
    offline_server.start()
    page = offline_page
    page.route("**/dashboard/online-host.js", lambda route: route.abort())
    page.goto(f"{offline_server.base_url}/", wait_until="domcontentloaded")

    note = page.locator("[data-dashboard-fallback]")
    assert note.count() == 1
    assert page.locator(".deck-list .deck-card").count() == 0
    # The wait state first, then the honest answer once it is clear the
    # mount is not coming.
    assert note.inner_text().strip() == "Loading your decks…"
    page.wait_for_function(
        "() => document.querySelector('[data-dashboard-fallback]')"
        ".textContent.includes('did not load')",
        timeout=15_000,
    )
    assert "Reload the page" in note.inner_text()
