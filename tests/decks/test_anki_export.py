"""Anki .apkg export — verify the format Anki accepts.

The real proof is "Anki imports it cleanly," which we can't run in a
unit test. Best proxy: round-trip through our own importer, since
that mirrors Anki's parsing closely enough to catch broken zips,
missing collection files, malformed flds, or HTML escaping bugs.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile

from fastapi.testclient import TestClient

from prep.auth.repo import UserRepo
from prep.decks.anki import apkg_to_deck
from prep.decks.anki_export import deck_to_apkg
from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo


def _seed_deck(user_id: str = "alice@example.com", deck_name: str = "src") -> int:
    UserRepo().upsert(external_id=user_id, email=user_id)
    deck_id = DeckRepo().get_or_create(user_id, deck_name)
    QuestionRepo().add(
        user_id,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="What is 2+2?", answer="4"),
    )
    QuestionRepo().add(
        user_id,
        deck_id,
        NewQuestion(
            type=QuestionType.SHORT,
            prompt="Capital of France?",
            answer="Paris",
            explanation="It has been since 987 CE.",
        ),
    )
    return deck_id


def test_apkg_is_a_valid_zip(initialized_db: str):
    deck_id = _seed_deck()
    blob = deck_to_apkg("alice@example.com", deck_id, "src")
    assert zipfile.is_zipfile(io.BytesIO(blob))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
    assert "collection.anki21" in names
    assert "media" in names


def test_apkg_collection_has_expected_tables(initialized_db: str):
    """Anki refuses imports lacking col / notes / cards / revlog."""
    deck_id = _seed_deck()
    blob = deck_to_apkg("alice@example.com", deck_id, "src")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        coll_bytes = zf.read("collection.anki21")
    # Open the inner sqlite to inspect.
    import tempfile
    from pathlib import Path

    tf = tempfile.NamedTemporaryFile(suffix=".anki21", delete=False)
    tf.write(coll_bytes)
    tf.close()
    try:
        conn = sqlite3.connect(tf.name)
        conn.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"col", "notes", "cards", "revlog", "graves"} <= tables
        # One col row, two notes, two cards.
        assert conn.execute("SELECT COUNT(*) FROM col").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2
        conn.close()
    finally:
        Path(tf.name).unlink(missing_ok=True)


def test_apkg_round_trip_through_our_importer(initialized_db: str):
    """Export from one user, import into another. The note text
    should make it across — that's the round-trip guarantee."""
    src_deck_id = _seed_deck(user_id="src@example.com", deck_name="src")
    blob = deck_to_apkg("src@example.com", src_deck_id, "src")

    UserRepo().upsert(external_id="dst@example.com", email="dst@example.com")
    outcome = apkg_to_deck(
        "dst@example.com",
        "dst",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert outcome.inserted == 2
    dst_id = DeckRepo().find_id("dst@example.com", "dst")
    assert dst_id is not None
    cards = QuestionRepo().list_in_deck("dst@example.com", dst_id)
    prompts = {c.prompt for c in cards}
    assert "What is 2+2?" in prompts
    assert "Capital of France?" in prompts


def test_apkg_export_includes_explanation_on_back(initialized_db: str):
    """Explanation should ride along on the back so nothing's lost."""
    deck_id = _seed_deck()
    blob = deck_to_apkg("alice@example.com", deck_id, "src")
    # Re-import + check the answer field on the dst side.
    UserRepo().upsert(external_id="dst@example.com", email="dst@example.com")
    apkg_to_deck(
        "dst@example.com",
        "dst",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    dst_id = DeckRepo().find_id("dst@example.com", "dst")
    cards = QuestionRepo().list_in_deck("dst@example.com", dst_id)
    paris = next(c for c in cards if c.prompt == "Capital of France?")
    assert "987 CE" in paris.answer


# ---- HTTP route --------------------------------------------------------


def test_export_route_returns_apkg(client: TestClient, initialized_db: str):
    # The fixture user is "testuser@example.com". Seed a deck under
    # that user.
    deck_id = DeckRepo().get_or_create("testuser@example.com", "exp-test")
    QuestionRepo().add(
        "testuser@example.com",
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="route Q", answer="route A"),
    )
    r = client.get("/deck/exp-test/export.apkg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert 'filename="exp-test.apkg"' in r.headers["content-disposition"]
    assert zipfile.is_zipfile(io.BytesIO(r.content))


def test_export_route_idor_safe(client: TestClient, initialized_db: str):
    """Asking for someone else's deck by name → 404."""
    UserRepo().upsert(external_id="other@example.com", email="other@example.com")
    DeckRepo().create("other@example.com", "their-deck")
    r = client.get("/deck/their-deck/export.apkg")
    assert r.status_code == 404
