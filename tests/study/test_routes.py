"""HTTP route tests for the study bounded context.

Drive the router through FastAPI's TestClient — exercises the full
stack from HTTP request → router → service → repo → sqlite. Covers
the session lifecycle, abandon, advance, the legacy no-session study
path, and the trivia-deck guardrail.

Free-text grading routes (`/grading/{wid}`) and the workflow polling
loop are out of scope for this file — they need a Temporal client or
a much larger fake; integration coverage lives in the e2e suite.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prep.decks.entities import DeckType, NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.study.entities import SessionStatus
from prep.study.repo import SessionRepo


def _seed_srs_deck(initialized_db: str, name: str = "study-rt") -> tuple[str, int, int]:
    user = initialized_db
    deck_id = DeckRepo().create(user, name)
    qid = QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.MCQ,
            prompt="2+2?",
            answer="4",
            choices=["3", "4", "5"],
        ),
    )
    return user, deck_id, qid


def test_begin_creates_session_and_redirects(client: TestClient, initialized_db: str):
    """POST /study/{name}/begin → 303 to /session/<sid>; a fresh active
    session shows up in the repo."""
    _seed_srs_deck(initialized_db)
    r = client.post("/study/study-rt/begin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/session/") or "/session/" in r.headers["location"]
    sids = SessionRepo().list_recent(initialized_db, limit=5)
    assert any(s.status is SessionStatus.ACTIVE for s in sids)


def test_begin_resumes_existing_session_when_available(client: TestClient, initialized_db: str):
    """A second begin without ?fresh=1 should land on the same session
    as the first — the auto-resume rail."""
    _seed_srs_deck(initialized_db)
    r1 = client.post("/study/study-rt/begin", follow_redirects=False)
    sid1 = r1.headers["location"].rsplit("/", 1)[-1]
    r2 = client.post("/study/study-rt/begin", follow_redirects=False)
    sid2 = r2.headers["location"].rsplit("/", 1)[-1]
    assert sid1 == sid2


def test_begin_with_fresh_abandons_old_session(client: TestClient, initialized_db: str):
    """?fresh=1 → abandon the prior session, start a new one."""
    _seed_srs_deck(initialized_db)
    r1 = client.post("/study/study-rt/begin", follow_redirects=False)
    sid1 = r1.headers["location"].rsplit("/", 1)[-1]
    r2 = client.post("/study/study-rt/begin?fresh=1", follow_redirects=False)
    sid2 = r2.headers["location"].rsplit("/", 1)[-1]
    assert sid1 != sid2
    # Old session is now abandoned.
    s = SessionRepo().get(initialized_db, sid1)
    assert s is not None
    assert s.status is SessionStatus.ABANDONED


def test_begin_refuses_trivia_deck(client: TestClient, initialized_db: str):
    """Trivia decks are notification-driven — /study/{name}/begin should
    400 rather than silently create an empty SRS session."""
    deck_repo = DeckRepo()
    deck_repo.create_trivia(initialized_db, "trivia-deck", topic="things", interval_minutes=30)
    # Sanity: deck created as trivia.
    deck_id = deck_repo.find_id(initialized_db, "trivia-deck")
    assert deck_id is not None
    assert deck_repo.get_type(initialized_db, deck_id) is DeckType.TRIVIA
    r = client.post("/study/trivia-deck/begin", follow_redirects=False)
    assert r.status_code == 400


def test_session_view_renders_active_session(client: TestClient, initialized_db: str):
    _seed_srs_deck(initialized_db)
    r = client.post("/study/study-rt/begin", follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[-1]
    rv = client.get(f"/session/{sid}")
    assert rv.status_code == 200
    # Prompt should appear in the rendered card.
    assert "2+2?" in rv.text


def test_session_view_404_for_unknown_session(client: TestClient, initialized_db: str):
    r = client.get("/session/never-existed")
    assert r.status_code == 404


def test_session_abandon_redirects_to_deck(client: TestClient, initialized_db: str):
    _seed_srs_deck(initialized_db)
    r = client.post("/study/study-rt/begin", follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[-1]
    r2 = client.post(f"/session/{sid}/abandon", follow_redirects=False)
    assert r2.status_code == 303
    assert "/deck/study-rt" in r2.headers["location"]
    s = SessionRepo().get(initialized_db, sid)
    assert s is not None
    assert s.status is SessionStatus.ABANDONED


def test_legacy_study_renders_due_card(client: TestClient, initialized_db: str):
    """GET /study/{name} (no /begin) → renders the no-session study
    template with one due card pre-loaded."""
    _seed_srs_deck(initialized_db)
    r = client.get("/study/study-rt")
    assert r.status_code == 200
    assert "2+2?" in r.text


def test_legacy_study_empty_when_no_due(client: TestClient, initialized_db: str):
    """No questions in the deck → study_empty.html instead of crashing."""
    DeckRepo().create(initialized_db, "empty-deck")
    r = client.get("/study/empty-deck")
    assert r.status_code == 200
    # The empty-state template doesn't render the prompt heading the
    # study card uses.
    assert "2+2?" not in r.text


# ---- /grading/{wid}/fragment ------------------------------------------
#
# The htmx polling fragment. Distinct from the plan/transform fragments
# in two ways: (1) the partial polls every 1.5s, not 2s; (2) on terminal
# completion the route returns an empty body + HX-Redirect to the
# canonical /grading/{wid} URL so htmx triggers a full-page navigation
# into result.html (which has the verdict logic). Tests stub
# `prep.temporal_client.get_grade_progress` + describe_workflow so we
# don't need a Temporal server in the loop.


def _afake(value):
    """Async fake — coroutine factory that ignores args and resolves to
    `value`."""

    async def fn(*_a, **_kw):
        return value

    return fn


def _seed_grading_wid(initialized_db: str, deck_name: str = "go-systems") -> tuple[int, str]:
    """Returns (qid, wid). The wid format is `grade-<deck>-q<qid>-<rand>`."""
    user = initialized_db
    deck_id = DeckRepo().create(user, deck_name)
    qid = QuestionRepo().add(
        user,
        deck_id,
        NewQuestion(
            type=QuestionType.SHORT,
            prompt="Define quorum.",
            answer="A majority of replicas needed to commit.",
        ),
    )
    wid = f"grade-{deck_name}-q{qid}-abc1234567"
    return qid, wid


def test_grading_fragment_mid_flow_keeps_polling(
    monkeypatch, client: TestClient, initialized_db: str
):
    """status=grading is non-terminal → hx-trigger present, the
    'Reading your answer' headline + status text render."""
    from prep import temporal_client

    _, wid = _seed_grading_wid(initialized_db)
    monkeypatch.setattr(temporal_client, "get_grade_progress", _afake({"status": "grading"}))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "RUNNING"}))

    r = client.get(f"/grading/{wid}/fragment")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'hx-trigger="every' in r.text
    # Status text reflects the in-flight phase.
    assert "grading" in r.text.lower()


def test_grading_fragment_terminal_returns_hx_redirect(
    monkeypatch, client: TestClient, initialized_db: str
):
    """status=done → response is empty + HX-Redirect points at the
    canonical /grading/{wid} URL so htmx does a full navigation into
    result.html. No body needs to render."""
    from prep import temporal_client

    _, wid = _seed_grading_wid(initialized_db)
    monkeypatch.setattr(temporal_client, "get_grade_progress", _afake({"status": "done"}))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "COMPLETED"}))

    r = client.get(f"/grading/{wid}/fragment")
    assert r.status_code == 200
    assert "HX-Redirect" in r.headers or "hx-redirect" in r.headers
    target = r.headers.get("HX-Redirect") or r.headers.get("hx-redirect")
    assert target.endswith(f"/grading/{wid}")
    # And no polling marker — the redirect takes over.
    assert 'hx-trigger="every' not in r.text


def test_grading_fragment_terminal_with_sid_preserves_query_param(
    monkeypatch, client: TestClient, initialized_db: str
):
    """When the fragment was invoked from inside a study session, the
    sid query param must be carried into the HX-Redirect target so the
    follow-up page reconciles the session row."""
    from prep import temporal_client

    _, wid = _seed_grading_wid(initialized_db)
    monkeypatch.setattr(temporal_client, "get_grade_progress", _afake({"status": "done"}))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "COMPLETED"}))

    r = client.get(f"/grading/{wid}/fragment?sid=sess-xyz")
    assert r.status_code == 200
    target = r.headers.get("HX-Redirect") or r.headers.get("hx-redirect")
    assert target is not None
    assert f"/grading/{wid}?sid=sess-xyz" in target


def test_grading_fragment_idor_other_user_404(monkeypatch, client: TestClient, initialized_db: str):
    """A grade wid for bob's question → 404 for alice (the test
    client user). The route's `q_repo.get(uid, qid)` check fires
    before any temporal call."""
    from prep.auth.repo import UserRepo
    from prep import temporal_client

    UserRepo().upsert("bob@example.com")
    bob_deck_id = DeckRepo().create("bob@example.com", "bobs-deck")
    bob_qid = QuestionRepo().add(
        "bob@example.com",
        bob_deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="?", answer="A"),
    )
    wid = f"grade-bobs-deck-q{bob_qid}-abc1234567"
    monkeypatch.setattr(temporal_client, "get_grade_progress", _afake({"status": "grading"}))

    r = client.get(f"/grading/{wid}/fragment")
    assert r.status_code == 404


def test_grading_fragment_is_html_not_json(monkeypatch, client: TestClient, initialized_db: str):
    """Future-refactor guard: mid-flow body must stay HTML."""
    from prep import temporal_client

    _, wid = _seed_grading_wid(initialized_db)
    monkeypatch.setattr(temporal_client, "get_grade_progress", _afake({"status": "grading"}))
    monkeypatch.setattr(temporal_client, "describe_workflow", _afake({"status": "RUNNING"}))

    r = client.get(f"/grading/{wid}/fragment")
    assert r.status_code == 200
    assert not r.headers["content-type"].startswith("application/json")
