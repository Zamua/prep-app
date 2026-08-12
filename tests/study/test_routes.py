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


def test_session_view_renders_the_study_shell(client: TestClient, initialized_db: str):
    """The session URL serves the shell bound to that session; the card
    itself arrives from the JSON API, so no prompt is in this HTML."""
    _seed_srs_deck(initialized_db)
    r = client.post("/study/study-rt/begin", follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[-1]
    rv = client.get(f"/session/{sid}")
    assert rv.status_code == 200
    assert "data-study-root" in rv.text
    assert f'data-session-id="{sid}"' in rv.text
    assert 'data-deck="study-rt"' in rv.text
    assert "2+2?" not in rv.text


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


def test_sessionless_study_renders_the_study_shell(client: TestClient, initialized_db: str):
    """GET /study/{name} (no /begin) serves the shell bound to the deck
    with no session id; the host studies the deck through the API."""
    _seed_srs_deck(initialized_db)
    r = client.get("/study/study-rt")
    assert r.status_code == 200
    assert "data-study-root" in r.text
    assert 'data-deck="study-rt"' in r.text
    assert 'data-session-id=""' in r.text


def test_legacy_study_empty_when_no_due(client: TestClient, initialized_db: str):
    """No questions in the deck → study_empty.html instead of crashing."""
    DeckRepo().create(initialized_db, "empty-deck")
    r = client.get("/study/empty-deck")
    assert r.status_code == 200
    # The empty-state template doesn't render the prompt heading the
    # study card uses.
    assert "2+2?" not in r.text


# ---- /grading/{wid} ---------------------------------------------------
#
# Grading used to be its own htmx polling page. The study shell owns an
# in-flight grade now, so this URL only has to keep old links working:
# it redirects into the shell, and it must not honour a workflow id
# naming someone else's deck or question.


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


def test_grading_url_redirects_into_the_study_shell(client: TestClient, initialized_db: str):
    _, wid = _seed_grading_wid(initialized_db)
    r = client.get(f"/grading/{wid}", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].endswith("/study/go-systems")


def test_grading_url_with_a_live_session_returns_to_that_session(
    client: TestClient, initialized_db: str
):
    _seed_srs_deck(initialized_db)
    begin = client.post("/study/study-rt/begin", follow_redirects=False)
    sid = begin.headers["location"].rsplit("/", 1)[-1]
    _, wid = _seed_grading_wid(initialized_db)
    r = client.get(f"/grading/{wid}?sid={sid}", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].endswith(f"/session/{sid}")


def test_grading_url_malformed_workflow_id_400(client: TestClient, initialized_db: str):
    r = client.get("/grading/not-a-grade-id", follow_redirects=False)
    assert r.status_code == 400


def test_grading_url_idor_other_user_404(client: TestClient, initialized_db: str):
    """A wid naming bob's deck + question is refused for the test user.
    Without the ownership check the redirect would land on
    /study/bobs-deck, whose get-or-create would mint that deck in the
    caller's account."""
    from prep.auth.repo import UserRepo

    UserRepo().upsert("bob@example.com")
    bob_deck_id = DeckRepo().create("bob@example.com", "bobs-deck")
    bob_qid = QuestionRepo().add(
        "bob@example.com",
        bob_deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="?", answer="A"),
    )
    wid = f"grade-bobs-deck-q{bob_qid}-abc1234567"

    r = client.get(f"/grading/{wid}", follow_redirects=False)
    assert r.status_code == 404
    assert DeckRepo().find_id(initialized_db, "bobs-deck") is None


# ---- /session/<sid>/snooze -----------------------------------------


def test_session_snooze_with_preset_redirects_home_and_hides(
    client: TestClient, initialized_db: str
):
    _seed_srs_deck(initialized_db)
    r = client.post("/study/study-rt/begin", follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[-1]
    r2 = client.post(f"/session/{sid}/snooze", data={"preset": "1h"}, follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"].endswith("/")
    # Now hidden from list_recent.
    recents = SessionRepo().list_recent(initialized_db)
    assert all(s.id != sid for s in recents)


def test_session_snooze_with_custom_duration(client: TestClient, initialized_db: str):
    _seed_srs_deck(initialized_db)
    r = client.post("/study/study-rt/begin", follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[-1]
    r2 = client.post(
        f"/session/{sid}/snooze",
        data={"custom": "2", "unit": "days"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    recents = SessionRepo().list_recent(initialized_db)
    assert all(s.id != sid for s in recents)


def test_session_snooze_bad_preset_returns_400(client: TestClient, initialized_db: str):
    _seed_srs_deck(initialized_db)
    r = client.post("/study/study-rt/begin", follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[-1]
    r2 = client.post(f"/session/{sid}/snooze", data={"preset": "garbage"}, follow_redirects=False)
    assert r2.status_code == 400


def test_session_snooze_preset_wake_clears(client: TestClient, initialized_db: str):
    from datetime import datetime, timedelta, timezone

    _seed_srs_deck(initialized_db)
    r = client.post("/study/study-rt/begin", follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[-1]
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    SessionRepo().snooze(initialized_db, sid, future)
    r2 = client.post(f"/session/{sid}/snooze", data={"preset": "wake"}, follow_redirects=False)
    assert r2.status_code == 303
    # Wake cleared the snooze — session is back in list_recent.
    assert any(s.id == sid for s in SessionRepo().list_recent(initialized_db))
