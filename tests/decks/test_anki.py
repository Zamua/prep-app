"""Anki .apkg import — parser unit tests + route smoke."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from prep.auth.repo import UserRepo
from prep.decks.anki import _strip_html, apkg_to_deck
from prep.decks.repo import DeckRepo, QuestionRepo

# ---- HTML stripping ----------------------------------------------------


def test_strip_html_preserves_line_breaks():
    s = "Hello<br>World"
    assert _strip_html(s) == "Hello\nWorld"


def test_strip_html_drops_media_and_tags():
    s = '<p>What is <b>this</b>?</p><img src="x.png">[sound:bell.mp3]'
    out = _strip_html(s)
    assert out == "What is this?"


def test_strip_html_decodes_entities():
    assert _strip_html("a &amp; b &lt;3&gt;") == "a & b <3>"


def test_strip_html_empty_in_empty_out():
    assert _strip_html("") == ""
    assert _strip_html(None) == ""  # type: ignore[arg-type]


# ---- .apkg fixture helpers --------------------------------------------


def _make_apkg(notes: list[tuple[str, str]]) -> bytes:
    """Build a minimal .apkg with the given (front, back) note pairs.
    Returns the raw bytes."""
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tf.close()
    try:
        conn = sqlite3.connect(tf.name)
        conn.execute(
            """
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY,
                flds TEXT NOT NULL
            )
            """
        )
        for i, (front, back) in enumerate(notes, start=1):
            conn.execute(
                "INSERT INTO notes (id, flds) VALUES (?, ?)",
                (i, f"{front}\x1f{back}"),
            )
        conn.commit()
        conn.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(tf.name, "collection.anki21")
        return buf.getvalue()
    finally:
        Path(tf.name).unlink(missing_ok=True)


def _make_apkg_legacy(notes: list[tuple[str, str]]) -> bytes:
    """Same as _make_apkg but uses collection.anki2 instead of anki21
    — verifies the older-format fallback works."""
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tf.close()
    try:
        conn = sqlite3.connect(tf.name)
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, flds TEXT NOT NULL)")
        for i, (front, back) in enumerate(notes, start=1):
            conn.execute(
                "INSERT INTO notes (id, flds) VALUES (?, ?)",
                (i, f"{front}\x1f{back}"),
            )
        conn.commit()
        conn.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(tf.name, "collection.anki2")
        return buf.getvalue()
    finally:
        Path(tf.name).unlink(missing_ok=True)


# ---- parser end-to-end -------------------------------------------------


def test_apkg_imports_notes(initialized_db: str):
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    blob = _make_apkg(
        [
            ("What is 2+2?", "4"),
            ("Capital of France?", "Paris"),
        ]
    )
    out = apkg_to_deck(
        "alice@example.com",
        "math-and-geography",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 2
    assert out.skipped_duplicates == 0
    assert out.cloze_skipped == 0
    assert out.errors == []


def test_apkg_legacy_collection_format(initialized_db: str):
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    blob = _make_apkg_legacy([("Q", "A")])
    out = apkg_to_deck(
        "alice@example.com",
        "legacy",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 1


def test_apkg_skips_cloze_notes(initialized_db: str):
    """Cloze deletions need a full Anki rendering pipeline; we skip
    them in v1 rather than mangle them. The count surfaces in the
    outcome so the user knows what was dropped."""
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    blob = _make_apkg(
        [
            ("The capital of {{c1::France}} is {{c2::Paris}}", "extra"),
            ("Normal note", "Normal answer"),
        ]
    )
    out = apkg_to_deck(
        "alice@example.com",
        "mixed",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 1
    assert out.cloze_skipped == 1


def test_apkg_strips_html_in_fields(initialized_db: str):
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    blob = _make_apkg([("<p>What is <b>bold</b>?</p>", "<i>Strong text</i>")])
    apkg_to_deck(
        "alice@example.com",
        "html",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    deck_id = DeckRepo().find_id("alice@example.com", "html")
    assert deck_id is not None
    cards = QuestionRepo().list_in_deck("alice@example.com", deck_id)
    assert len(cards) == 1
    assert "<" not in cards[0].prompt
    assert cards[0].prompt == "What is bold?"


def test_apkg_dedup_on_repeat_import(initialized_db: str):
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    blob = _make_apkg([("Q1", "A1"), ("Q2", "A2")])
    apkg_to_deck(
        "alice@example.com",
        "dedup-test",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    second = apkg_to_deck(
        "alice@example.com",
        "dedup-test",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert second.inserted == 0
    assert second.skipped_duplicates == 2


def test_apkg_rejects_non_zip(initialized_db: str):
    """Pasting random bytes (or a CSV) should error cleanly, not
    blow up."""
    import pytest

    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    with pytest.raises(ValueError, match="not a valid .apkg"):
        apkg_to_deck(
            "alice@example.com",
            "x",
            b"this,is,csv\n1,2,3\n",
            deck_repo=DeckRepo(),
            question_repo=QuestionRepo(),
        )


def test_apkg_rejects_zip_without_collection(initialized_db: str):
    """A zip with no collection.anki2 / .anki21 is not a real .apkg."""
    import pytest

    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.txt", "hi")
    with pytest.raises(ValueError, match="no collection"):
        apkg_to_deck(
            "alice@example.com",
            "x",
            buf.getvalue(),
            deck_repo=DeckRepo(),
            question_repo=QuestionRepo(),
        )


def test_apkg_handles_note_with_only_front(initialized_db: str):
    """Notes with empty back field land in errors (no flashcard
    content), not inserted."""
    UserRepo().upsert(external_id="alice@example.com", email="alice@example.com")
    blob = _make_apkg([("Front only", "")])
    out = apkg_to_deck(
        "alice@example.com",
        "x",
        blob,
        deck_repo=DeckRepo(),
        question_repo=QuestionRepo(),
    )
    assert out.inserted == 0
    assert any("no back-side content" in e for e in out.errors)


# ---- HTTP route smoke --------------------------------------------------


def test_route_imports_apkg(client: TestClient, initialized_db: str):
    """Multipart upload to /decks/import-anki creates the deck and
    inserts the notes."""
    blob = _make_apkg([("RouteQ", "RouteA")])
    r = client.post(
        "/decks/import-anki",
        data={"name": "from-anki"},
        files={"file": ("deck.apkg", blob, "application/octet-stream")},
        follow_redirects=False,
    )
    # The route renders the outcome page (or redirects to it).
    assert r.status_code in (200, 303)
    deck_id = DeckRepo().find_id("testuser@example.com", "from-anki")
    assert deck_id is not None
    cards = QuestionRepo().list_in_deck("testuser@example.com", deck_id)
    assert any(c.prompt == "RouteQ" for c in cards)


def test_route_idor_safe(client: TestClient, initialized_db: str):
    """An imported deck lands under the authenticated user, not
    anyone else."""
    UserRepo().upsert(external_id="other@example.com", email="other@example.com")
    blob = _make_apkg([("OnlyMine", "Secret")])
    client.post(
        "/decks/import-anki",
        data={"name": "private-stuff"},
        files={"file": ("deck.apkg", blob, "application/octet-stream")},
    )
    # The deck exists under the fixture user, not under "other".
    assert DeckRepo().find_id("testuser@example.com", "private-stuff") is not None
    assert DeckRepo().find_id("other@example.com", "private-stuff") is None
