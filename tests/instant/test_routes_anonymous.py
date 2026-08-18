"""POST /api/instant/generate as the only path that mints an account.

Drives the real app with the anonymous-cookie provider installed, so
the cookie, the stored deck and the ledger attribution are asserted
end to end.
"""

from __future__ import annotations

import json
import time

import pytest

from prep.agent.fake import FakeAgent
from prep.agent.port import AgentResult
from prep.auth import anon_cookie as ac
from prep.auth import limits
from prep.auth.port import ResolvedUser
from prep.auth.providers import set_provider
from prep.auth.providers.anon import AnonymousFallbackProvider
from prep.infrastructure import db as infra_db
from prep.instant import repo as instant_repo
from prep.instant.service import DISPLAY_NAME_MAX_CHARS
from tests.auth.test_anon_provider import _Inner

URL = "/api/instant/generate"
IP = {"x-real-ip": "198.51.100.7"}
MASTER = "11" * 32
DAY = 86400

SIGNED_IN = ResolvedUser(
    external_id="user_2abc",
    email="a@example.com",
    display_name="Alice",
    profile_pic_url=None,
    provider="inner",
)


def _deck_text(n: int = 5) -> str:
    return json.dumps(
        [{"q": f"Question {i}?", "a": f"answer {i}", "r": f"answer {i}"} for i in range(n)]
    )


def _fake(text: str | None = None) -> FakeAgent:
    return FakeAgent(next_response=AgentResult(text=text or _deck_text(), model="fake-model"))


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.setenv(ac.MASTER_ENV, MASTER)


@pytest.fixture
def visitor(client, secret):
    """A deploy that can mint: no provider identity, a cookie secret."""
    set_provider(AnonymousFallbackProvider(_Inner()))
    yield client
    set_provider(None)


@pytest.fixture
def signed_in(client, secret):
    set_provider(AnonymousFallbackProvider(_Inner(user=SIGNED_IN)))
    yield client
    set_provider(None)


def cookie_header(value: str) -> dict[str, str]:
    """Per-request cookie as a raw header; TestClient's cookies= kwarg is deprecated."""
    return {"cookie": f"{ac.COOKIE_NAME}={value}"}


def anon_user_ids() -> list[str]:
    with infra_db.cursor() as c:
        return [
            r["tailscale_login"]
            for r in c.execute("SELECT tailscale_login FROM users WHERE is_anonymous = 1")
        ]


def decks_of(user_id: str) -> list[dict]:
    with infra_db.cursor() as c:
        return [dict(r) for r in c.execute("SELECT * FROM decks WHERE user_id = ?", (user_id,))]


def ledger() -> list[dict]:
    with infra_db.cursor() as c:
        return [dict(r) for r in c.execute("SELECT * FROM instant_generations ORDER BY id")]


def set_cookie_headers(response) -> list[str]:
    return [
        raw
        for raw in response.headers.get_list("set-cookie")
        if raw.startswith(f"{ac.COOKIE_NAME}=")
    ]


def cookie_value(response) -> str | None:
    raws = set_cookie_headers(response)
    if not raws:
        return None
    assert len(raws) == 1, raws
    return raws[0].split(";", 1)[0].split("=", 1)[1]


# ---- the mint ---------------------------------------------------------------


def test_a_successful_generation_mints_one_user_and_one_deck(visitor, instant_factory):
    instant_factory(lambda **kw: _fake())
    r = visitor.post(URL, json={"topic": "Postgres MVCC"}, headers=IP)

    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "ok"

    minted = anon_user_ids()
    assert len(minted) == 1
    decks = decks_of(minted[0])
    assert len(decks) == 1
    assert body["redirect"] == f"/deck/{decks[0]['name']}"
    assert decks[0]["display_name"] == "Postgres MVCC"

    with infra_db.cursor() as c:
        questions = c.execute(
            "SELECT COUNT(*) AS n FROM questions WHERE user_id = ?", (minted[0],)
        ).fetchone()["n"]
        cards = c.execute(
            "SELECT COUNT(*) AS n FROM cards JOIN questions q ON q.id = cards.question_id"
            " WHERE q.user_id = ?",
            (minted[0],),
        ).fetchone()["n"]
    assert (questions, cards) == (5, 5)

    cookie = ac.verify_cookie(cookie_value(r))
    assert cookie is not None
    assert cookie.external_id == minted[0]


def test_the_redirect_carries_the_root_path(visitor, instant_factory):
    """A bare /deck/<slug> would land outside a prefixed mount."""
    from fastapi.testclient import TestClient

    import prep.app as app_mod

    instant_factory(lambda **kw: _fake())
    with TestClient(app_mod.app, root_path="/prep") as mounted:
        r = mounted.post(URL, json={"topic": "t"}, headers=IP)

    assert r.status_code == 200
    slug = decks_of(anon_user_ids()[0])[0]["name"]
    assert r.json()["redirect"] == f"/prep/deck/{slug}"


def test_a_long_topic_is_truncated_in_the_display_name_not_in_the_slug(visitor, instant_factory):
    instant_factory(lambda **kw: _fake())
    topic = "x" * 500
    r = visitor.post(URL, json={"topic": topic}, headers=IP)

    assert r.status_code == 200
    deck = decks_of(anon_user_ids()[0])[0]
    assert deck["display_name"] == topic[:DISPLAY_NAME_MAX_CHARS]
    assert len(deck["name"]) == 8
    assert r.json()["redirect"] == f"/deck/{deck['name']}"


def test_a_non_latin_topic_still_gets_an_opaque_slug(visitor, instant_factory):
    instant_factory(lambda **kw: _fake())
    r = visitor.post(URL, json={"topic": "日本語の歴史"}, headers=IP)

    assert r.status_code == 200
    deck = decks_of(anon_user_ids()[0])[0]
    assert deck["display_name"] == "日本語の歴史"
    assert len(deck["name"]) == 8
    assert deck["name"].isascii()


def test_the_minted_cookie_turns_the_landing_into_the_dashboard(visitor, instant_factory):
    instant_factory(lambda **kw: _fake())
    r = visitor.post(URL, json={"topic": "Postgres MVCC"}, headers=IP)
    assert r.status_code == 200

    home = visitor.get("/", headers=cookie_header(cookie_value(r)))

    assert home.status_code == 200
    assert home.template.name == "index.html"
    assert "Postgres MVCC" in home.text
    assert "data-instant-start" not in home.text


# ---- what must NOT mint -----------------------------------------------------


def test_a_failed_generation_mints_nothing(visitor, instant_factory):
    instant_factory(lambda **kw: _fake("a poem, not JSON"))
    r = visitor.post(URL, json={"topic": "t"}, headers=IP)

    assert r.status_code == 502
    assert anon_user_ids() == []
    assert set_cookie_headers(r) == []


def test_a_rate_limited_request_mints_nothing(visitor, instant_factory):
    instant_factory(lambda **kw: _fake())
    assert visitor.post(URL, json={"topic": "t"}, headers=IP).status_code == 200
    before = anon_user_ids()

    r = visitor.post(URL, json={"topic": "t"}, headers=IP)
    assert r.status_code == 429
    assert r.json()["kind"] == "rate_limited"
    assert anon_user_ids() == before


def test_an_invalid_topic_mints_nothing(visitor, instant_factory):
    instant_factory(lambda **kw: _fake())
    r = visitor.post(URL, json={"topic": "   "}, headers=IP)

    assert r.status_code == 422
    assert anon_user_ids() == []
    assert ledger() == []


def test_a_deploy_with_no_cookie_secret_refuses_a_signed_out_generation(
    client, instant_factory, monkeypatch
):
    monkeypatch.delenv(ac.SECRET_ENV, raising=False)
    monkeypatch.delenv(ac.MASTER_ENV, raising=False)
    monkeypatch.delenv("PREP_DEFAULT_USER", raising=False)
    set_provider(_Inner())
    try:
        instant_factory(lambda **kw: _fake())
        r = client.post(URL, json={"topic": "t"}, headers=IP)
    finally:
        set_provider(None)

    assert r.status_code == 503
    assert r.json()["kind"] == "not_configured"
    assert anon_user_ids() == []
    assert ledger() == []


# ---- the returning visitor --------------------------------------------------


def test_a_valid_cookie_reuses_the_account_and_sets_no_cookie(visitor, instant_factory):
    instant_factory(lambda **kw: _fake())
    first = visitor.post(URL, json={"topic": "One"}, headers=IP)
    assert first.status_code == 200
    minted = anon_user_ids()[0]

    second = visitor.post(
        URL,
        json={"topic": "Two"},
        headers={"x-real-ip": "198.51.100.8", **cookie_header(ac.mint_cookie(minted))},
    )

    assert second.status_code == 200
    assert anon_user_ids() == [minted]
    assert len(decks_of(minted)) == 2
    assert set_cookie_headers(second) == []
    assert second.json()["redirect"] != first.json()["redirect"]


def test_a_cookie_past_the_rolling_window_is_re_minted_for_the_same_account(
    visitor, instant_factory
):
    instant_factory(lambda **kw: _fake())
    assert visitor.post(URL, json={"topic": "One"}, headers=IP).status_code == 200
    minted = anon_user_ids()[0]
    aging = ac.mint_cookie(minted, issued_at=int(time.time()) - 31 * DAY)

    second = visitor.post(
        URL,
        json={"topic": "Two"},
        headers={"x-real-ip": "198.51.100.8", **cookie_header(aging)},
    )

    assert second.status_code == 200
    refreshed = ac.verify_cookie(cookie_value(second))
    assert refreshed is not None
    assert refreshed.external_id == minted
    assert anon_user_ids() == [minted]


def test_a_cookie_for_a_deleted_user_mints_a_fresh_account(visitor, instant_factory):
    instant_factory(lambda **kw: _fake())
    dead = "anon:" + "ab" * 16

    r = visitor.post(
        URL, json={"topic": "t"}, headers={**IP, **cookie_header(ac.mint_cookie(dead))}
    )

    assert r.status_code == 200
    minted = anon_user_ids()
    assert minted != [dead]
    assert len(minted) == 1
    # One header only: the stale-cookie delete must not follow the
    # cookie that names the account this response just created.
    cookie = ac.verify_cookie(cookie_value(r))
    assert cookie is not None
    assert cookie.external_id == minted[0]


def test_a_signed_in_request_mints_nothing_and_owns_the_deck(signed_in, instant_factory):
    instant_factory(lambda **kw: _fake())
    r = signed_in.post(
        URL,
        json={"topic": "t"},
        headers={**IP, **cookie_header(ac.mint_cookie("anon:" + "cd" * 16))},
    )

    assert r.status_code == 200
    assert anon_user_ids() == []
    decks = decks_of(SIGNED_IN.external_id)
    assert len(decks) == 1
    assert r.json()["redirect"] == f"/deck/{decks[0]['name']}"
    # The cookie names no row, so the merge resolves as anon_missing and
    # the response clears the dead pointer. One header, not a re-mint.
    raws = set_cookie_headers(r)
    assert len(raws) == 1 and "Max-Age=0" in raws[0]


# ---- the row cap at the endpoint --------------------------------------------


def test_an_account_at_the_deck_cap_is_refused_and_keeps_its_decks(
    visitor, instant_factory, monkeypatch
):
    monkeypatch.setattr(limits, "ANON_MAX_DECKS", 1)
    instant_factory(lambda **kw: _fake())
    assert visitor.post(URL, json={"topic": "One"}, headers=IP).status_code == 200
    minted = anon_user_ids()[0]

    r = visitor.post(
        URL,
        json={"topic": "Two"},
        headers={"x-real-ip": "198.51.100.8", **cookie_header(ac.mint_cookie(minted))},
    )

    assert r.status_code == 429
    assert r.json()["kind"] == "deck_limit"
    assert r.json()["message"]
    assert len(decks_of(minted)) == 1
    assert [row["outcome"] for row in ledger()] == ["ok", "failed_spent"]


# ---- ledger attribution -----------------------------------------------------


class _LedgerReadingAgent:
    """Reads the pending reservation while the generation is in
    flight: the only moment the reserve-time state is observable."""

    def __init__(self):
        self.pending: list[dict] = []

    async def run(self, prompt, *, model=None, reasoning=None, timeout_s=120.0):
        self.pending = ledger()
        return AgentResult(text=_deck_text(), model="fake-model")


def test_the_minting_request_reserves_null_and_is_back_stamped(visitor, instant_factory):
    agent = _LedgerReadingAgent()
    instant_factory(lambda **kw: agent)

    r = visitor.post(URL, json={"topic": "t"}, headers=IP)

    assert r.status_code == 200
    assert [row["user_id"] for row in agent.pending] == [None]
    assert [row["outcome"] for row in agent.pending] == ["pending"]
    minted = anon_user_ids()[0]
    rows = ledger()
    assert [row["user_id"] for row in rows] == [minted]
    assert [row["outcome"] for row in rows] == ["ok"]


def test_a_returning_visitor_carries_the_id_at_reserve_time(visitor, instant_factory):
    instant_factory(lambda **kw: _fake())
    assert visitor.post(URL, json={"topic": "One"}, headers=IP).status_code == 200
    minted = anon_user_ids()[0]

    agent = _LedgerReadingAgent()
    instant_factory(lambda **kw: agent)
    second = visitor.post(
        URL,
        json={"topic": "Two"},
        headers={"x-real-ip": "198.51.100.8", **cookie_header(ac.mint_cookie(minted))},
    )

    assert second.status_code == 200
    assert agent.pending[-1]["user_id"] == minted
    assert agent.pending[-1]["outcome"] == "pending"


def test_a_crash_between_mint_and_resolve_leaves_a_pending_row_that_counts_per_ip(
    visitor, instant_factory, monkeypatch
):
    monkeypatch.setenv("PREP_INSTANT_BURST_LIMIT", "10")
    monkeypatch.setenv("PREP_INSTANT_PER_IP_PER_DAY", "1")
    instant_factory(lambda **kw: _fake())

    def locked(reservation_id, outcome, *, cards=None, user_id=None):
        raise RuntimeError("crashed before the ledger resolve")

    healthy = instant_repo.resolve
    monkeypatch.setattr(instant_repo, "resolve", locked)
    first = visitor.post(URL, json={"topic": "One"}, headers=IP)
    assert first.status_code == 200
    rows = ledger()
    assert [row["outcome"] for row in rows] == ["pending"]
    assert rows[0]["user_id"] is None

    monkeypatch.setattr(instant_repo, "resolve", healthy)
    second = visitor.post(URL, json={"topic": "Two"}, headers=IP)
    assert second.status_code == 429
    assert second.json()["scope"] == "day"
