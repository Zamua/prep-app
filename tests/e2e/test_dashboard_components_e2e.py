"""Browser side of the shared dashboard components.

The components in static/js/dashboard/ are the one implementation both
dashboards run: the signed-in page (dashboard/online-host.js) over the
overview its shell embeds, the offline shell over IndexedDB. That the
two render the same markup is pinned in test_dashboard_parity_e2e.py;
what this suite pins is the port and the views themselves, against a
real IndexedDB in a real browser.

What it pins:
  - the DeckSource contract as LocalSource answers it against a seeded
    IndexedDB: per-deck due/total, the pin and the deck order it can
    only get from the snapshot, the aggregate that includes cards filed
    under no deck, nextDueMinutes, unsynced counts
  - a card due in the FUTURE counts in neither due total
  - the components rendering that payload: deck rows, counts, and the
    due strip's callback
  - every class the shared views render is a class the stylesheet
    styles, asserted against the CSS FILES so the pin cannot rot into
    a self-referential copy of the JS. A renamed class that leaves the
    dashboard rendering unstyled fails here.

The modules are imported into a real page (the offline shell, whose
importmap resolves the versioned module URLs) and mounted into a
#harness element. Every locator is scoped to #harness so the shell's
own render cannot answer for the components under test.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.browser]

CSS_ROOT = Path(__file__).resolve().parents[2] / "static" / "css"

_CSS_CLASS = re.compile(r"\.([A-Za-z0-9_-]+)")

# Layout hooks with no rules of their own: they exist so a host can
# find a region, and the look comes from their children. Everything
# else the components render has to be styled.
_UNSTYLED_CONTAINERS = {"prelude", "decks-section-pinned", "due-strip"}

# Classes the shared prelude owns.
PRELUDE_CLASSES = ["prelude", "eyebrow", "display", "lede"]

# Classes the shared deck list owns when it has decks to show. The
# per-row overflow menu is deliberately absent: its rows are server
# routes, so the host builds it and passes it in.
DECK_LIST_CLASSES = [
    "decks-section",
    "decks-section-pinned",
    "section-eyebrow",
    "rule",
    "deck-list",
    "deck-card",
    "deck-card-pinned",
    "deck-link",
    "deck-numeral",
    "deck-body",
    "deck-type-eyebrow",
    "deck-name",
    "deck-pin-mark",
    "deck-stats",
    "stat-due",
    "is-active",
    "stat-value",
    "stat-label",
    "stat-divider",
    "stat-total",
    "deck-mastery-mini",
    "deck-mastery-mini-label",
    "mastery-bar",
    "mastery-bar--mini",
    "mastery-bar__fill",
    "mastery-bar__fill--right",
    "mastery-bar__fill--wrong",
    "deck-actions",
    "deck-action-pill",
    "deck-action-icon",
]

# Classes the deck list owns on a brand-new account.
EMPTY_STATE_CLASSES = [
    "empty-state",
    "index-empty-state",
    "empty-headline",
    "empty-sub",
    "btn",
    "btn-primary",
    "index-empty-cta",
]


def _styled_classes() -> set[str]:
    found: set[str] = set()
    for path in sorted(CSS_ROOT.rglob("*.css")):
        found.update(_CSS_CLASS.findall(path.read_text(encoding="utf-8")))
    return found


# ---- page harness ------------------------------------------------------

_SEED_JS = """
async ({prefix, seed}) => {
  const store = await import(prefix + "offline/store.js");
  await store.metaPut("owner", seed.owner);
  for (const deck of seed.decks) await store.put("decks", deck);
  for (const card of seed.cards) await store.put("cards", card);
  for (const card of seed.local_cards) await store.put("local_cards", card);
  for (const review of seed.outbox) await store.put("outbox_reviews", review);
  return true;
}
"""

# Reads the port, then renders what it returned. Both halves land on
# window so one page load covers the contract and the DOM.
_LOCAL_JS = """
async ({prefix}) => {
  const local = await import(prefix + "dashboard/local-source.js");
  const views = await import(prefix + "dashboard/components.js");
  const host = document.createElement("div");
  host.id = "harness";
  document.body.appendChild(host);

  const overview = await new local.LocalSource().overview();
  window.__studied = false;
  host.appendChild(views.preludeView(overview, {status: "Reading a snapshot on this device."}));
  host.appendChild(views.dueStripView(overview, {onStudy: () => {window.__studied = true;}}));
  host.appendChild(views.deckListView(overview));
  return overview;
}
"""

_SYNTHETIC_JS = """
async ({prefix, raw, actions}) => {
  const src = await import(prefix + "dashboard/source.js");
  const views = await import(prefix + "dashboard/components.js");
  const host = document.createElement("div");
  host.id = "harness";
  document.body.appendChild(host);

  const overview = src.normalizeOverview(raw);
  host.appendChild(views.preludeView(overview));
  host.appendChild(
    views.deckListView(overview, {actions, deckHref: (d) => "/deck/" + d.slug})
  );

  const blank = document.createElement("div");
  blank.id = "harness-empty";
  document.body.appendChild(blank);
  blank.appendChild(views.deckListView(src.normalizeOverview({decks: []})));
  return overview;
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


def _open_shell(page, base_url: str) -> str:
    """Load the offline shell on empty storage. With no owner stamped
    the shell renders its empty state and returns before any sync, so
    nothing races the seed this suite writes next."""
    page.goto(f"{base_url}/offline", wait_until="domcontentloaded")
    page.wait_for_selector("#offline-root .prelude", timeout=15_000)
    # The shell's own prelude comes from study/dom.js with no line
    # break, the default the dashboard headline opts out of. Pinned
    # here so the shared helper cannot gain a break for everyone.
    headline = page.locator("#offline-root .prelude .display")
    assert headline.text_content().strip() == "Nothing cached yet."
    assert headline.locator("br").count() == 0
    return _module_prefix(page)


def _seed() -> dict:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=2)).isoformat()
    future = (now + timedelta(minutes=90)).isoformat()
    return {
        "owner": {
            "user_id": "dashboard-e2e@example.com",
            "display_name": "Dashboard Tester",
            "snapshot_at": now.isoformat(),
            "build": "test",
        },
        # `total` and `pinned_at` are the snapshot's, not derivable
        # from the cards: deck 1 holds one suspended question this
        # device has no card for, and deck 2 is pinned. Insertion order
        # is deliberately NOT the render order.
        "decks": [
            {
                "id": 1,
                "name": "capitals",
                "display_name": "Capitals",
                "pinned_at": None,
                "total": 3,
            },
            {
                "id": 2,
                "name": "second-deck",
                "display_name": None,
                "pinned_at": "2030-01-01T00:00:00+00:00",
                "total": 2,
            },
        ],
        "cards": [
            {"question_id": 11, "deck_id": 1, "type": "short", "prompt": "a", "next_due": past},
            {"question_id": 12, "deck_id": 1, "type": "short", "prompt": "b", "next_due": future},
            {"question_id": 13, "deck_id": 2, "type": "short", "prompt": "c", "next_due": past},
        ],
        # deck_id null: an authored card filed under no deck. It counts
        # in the aggregate and in no deck row.
        "local_cards": [
            {
                "client_id": "lc-1",
                "deck_id": 1,
                "prompt": "d",
                "answer": "d",
                "local_next_due": None,
            },
            {
                "client_id": "lc-2",
                "deck_id": None,
                "prompt": "e",
                "answer": "e",
                "local_next_due": None,
            },
        ],
        "outbox": [
            {"client_id": "r-1", "verdict": "right", "reviewed_at": past},
            {"client_id": "r-2", "verdict": "wrong", "reviewed_at": past},
        ],
    }


# ---- the port against a seeded IndexedDB -------------------------------


def test_local_source_shapes_and_renders_the_offline_overview(offline_server, offline_page):
    offline_server.start()
    page = offline_page
    prefix = _open_shell(page, offline_server.base_url)
    assert page.evaluate(_SEED_JS, {"prefix": prefix, "seed": _seed()}) is True

    overview = page.evaluate(_LOCAL_JS, {"prefix": prefix})

    assert overview["user"] == {"display_name": "Dashboard Tester", "is_anonymous": False}
    assert overview["unsynced"] == {"reviews": 2, "cards": 2}
    decks = {d["slug"]: d for d in overview["decks"]}
    assert set(decks) == {"capitals", "second-deck"}
    # "in deck" is the snapshot's question count (a suspended question
    # this device holds no card for is in it) plus the cards authored
    # here, which are in no snapshot yet. `due` is what this device
    # holds and can study: one past-due snapshot card and one authored
    # card, with the card due in 90 minutes in neither.
    assert (decks["capitals"]["due"], decks["capitals"]["total"]) == (2, 4)
    assert (decks["second-deck"]["due"], decks["second-deck"]["total"]) == (1, 2)
    # The pin is the snapshot's too: nothing in the card rows says it.
    assert decks["second-deck"]["pinned"] is True
    assert decks["capitals"]["pinned"] is False
    # Pinned first, the server's order, not IndexedDB key order.
    assert [d["slug"] for d in overview["decks"]] == ["second-deck", "capitals"]
    # A missing display_name falls back to the slug, so a view always
    # has something to render.
    assert decks["second-deck"]["display_name"] == "second-deck"
    assert decks["capitals"]["deck_type"] == "srs"
    assert decks["capitals"]["trivia_stats"] is None
    # The aggregate counts the deckless authored card, which no row does.
    assert (overview["due"], overview["total"]) == (4, 5)
    assert sum(d["due"] for d in overview["decks"]) == 3
    assert 80 <= overview["nextDueMinutes"] <= 91

    rows = page.locator("#harness .deck-card")
    assert rows.count() == 2
    # The pinned deck leads, in its own section and carrying the mark.
    assert page.locator("#harness .decks-section-pinned .deck-card").count() == 1
    assert rows.nth(0).locator(".deck-name").text_content().strip() == "second-deck"
    assert rows.nth(0).locator(".deck-pin-mark").count() == 1

    capitals = rows.nth(1)
    assert capitals.locator(".deck-name").text_content().strip() == "Capitals"
    # deck-list.css lowercases .deck-name. Rendered text differing from
    # the DOM text is the proof that the server dashboard's stylesheet
    # reaches this client-rendered card at all.
    assert capitals.locator(".deck-name").inner_text().strip() == "capitals"
    assert capitals.locator(".stat-due .stat-value").inner_text().strip() == "2"
    assert capitals.locator(".stat-total .stat-value").inner_text().strip() == "4"
    assert "is-active" in (capitals.locator(".stat-due").get_attribute("class") or "")
    # Numerals restart per section, so the unpinned section's first row
    # is I even though it is the second card on the screen.
    assert capitals.locator(".deck-numeral").inner_text().strip() == "I"
    # No host supplied deck URLs, so the row renders no link target
    # rather than a broken one.
    assert capitals.locator(".deck-link").get_attribute("href") is None

    assert (
        page.locator("#harness .dashboard-status").inner_text().strip()
        == "Reading a snapshot on this device."
    )
    assert page.evaluate("() => window.__studied") is False
    page.locator("#harness .due-strip .btn-primary").click()
    assert page.evaluate("() => window.__studied") is True


def test_due_strip_shows_the_wait_when_nothing_is_due(offline_server, offline_page):
    """Nothing due renders the wait, not a study button with an empty
    queue behind it."""
    offline_server.start()
    page = offline_page
    prefix = _open_shell(page, offline_server.base_url)
    note = page.evaluate(
        """
        async ({prefix}) => {
          const src = await import(prefix + "dashboard/source.js");
          const views = await import(prefix + "dashboard/components.js");
          const host = document.createElement("div");
          host.id = "harness";
          document.body.appendChild(host);
          const overview = src.normalizeOverview({decks: [], nextDueMinutes: 120});
          host.appendChild(views.dueStripView(overview, {onStudy: () => {}}));
          return host.querySelector(".due-strip-note").textContent;
        }
        """,
        {"prefix": prefix},
    )
    assert note == "The next card comes due in 2 hr."
    assert page.locator("#harness .due-strip .btn-primary").count() == 0


# ---- class parity with the server dashboard ----------------------------


def test_dashboard_components_render_only_styled_classes(offline_server, offline_page):
    offline_server.start()
    page = offline_page
    prefix = _open_shell(page, offline_server.base_url)

    required = PRELUDE_CLASSES + DECK_LIST_CLASSES + EMPTY_STATE_CLASSES
    styled = _styled_classes()
    assert len(styled) > 100, "stylesheet class scrape came back suspiciously empty"
    unstyled = [c for c in required if c not in styled and c not in _UNSTYLED_CONTAINERS]
    assert not unstyled, f"classes the components render that no stylesheet styles: {unstyled}"

    raw = {
        "user": {"display_name": "Ada", "is_anonymous": False},
        "decks": [
            {
                "id": 1,
                "name": "pinned-deck",
                "display_name": "Pinned deck",
                "due": 3,
                "total": 9,
                "pinned": True,
            },
            {
                "id": 2,
                "slug": "trivia-deck",
                "display_name": "Trivia deck",
                "deck_type": "trivia",
                "due": 0,
                "total": 12,
                "trivia_stats": {"mastered": 4, "wrong": 2, "total": 12},
            },
            {"id": 3, "name": "plain", "display_name": "Plain deck", "due": 0, "total": 4},
        ],
        "nextDueMinutes": 120,
    }
    actions = [
        {"glyph": "+", "label": "new deck", "href": "/decks/new"},
        {"icon": "sparkle", "label": "edit with AI", "href": "/reorganize"},
    ]
    overview = page.evaluate(_SYNTHETIC_JS, {"prefix": prefix, "raw": raw, "actions": actions})
    # Aggregates absent from the payload are summed from the rows.
    assert (overview["due"], overview["total"]) == (3, 25)

    for cls in PRELUDE_CLASSES + DECK_LIST_CLASSES:
        assert page.locator(f"#harness .{cls}").count() > 0, f"components never rendered .{cls}"
    for cls in EMPTY_STATE_CLASSES:
        assert (
            page.locator(f"#harness-empty .{cls}").count() > 0
        ), f"the empty state never rendered .{cls}"

    # The dashboard headline breaks before its italic beat, the way
    # templates/index.html sets it.
    headline = page.locator("#harness .prelude .display")
    assert headline.locator("br").count() == 1
    assert headline.locator("em").text_content().strip() == "of questions"
    assert headline.text_content().strip() == "A standing libraryof questions."

    # The pin-toggle route swaps this element by id; the client render
    # has to keep offering it.
    assert page.locator("#harness #deck-lists").count() == 1
    # The pinned deck is its own section, ahead of the rest.
    assert page.locator("#harness .decks-section-pinned .deck-card").count() == 1
    assert (
        page.locator("#harness .decks-section:not(.decks-section-pinned) .deck-card").count() == 2
    )
    assert page.locator("#harness .deck-card-pinned .deck-pin-mark svg").count() == 1
    # A trivia deck shows mastery instead of due/total; an SRS deck the
    # reverse. Same component, decided by the row's data.
    trivia = page.locator("#harness .deck-card", has=page.locator(".deck-mastery-mini"))
    assert trivia.count() == 1
    assert trivia.locator(".deck-stats").count() == 0
    assert trivia.locator(".deck-type-eyebrow-trivia").text_content().strip() == "trivia"
    assert page.locator("#harness .deck-card .deck-stats").count() == 2
    assert (
        page.locator("#harness .deck-mastery-mini-label").text_content().strip() == "4/12 mastered"
    )
    assert (
        page.locator("#harness .deck-card").nth(1).locator(".deck-link").get_attribute("href")
        == "/deck/trivia-deck"
    )
    assert page.locator("#harness .deck-action-pill").count() == 2
