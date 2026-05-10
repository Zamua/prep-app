"""Quick end-to-end render test for the question_new + question_edit
templates after macro extraction. Confirms both routes return 200."""


def test_question_new_renders(client, initialized_db):
    from prep.decks.repo import DeckRepo

    DeckRepo().create(initialized_db, "qftest")
    r = client.get("/deck/qftest/question/new")
    assert r.status_code == 200, r.text[:200]
    assert "Add card" in r.text
    assert "Cancel" in r.text


def test_question_edit_renders(client, initialized_db):
    from prep.decks.entities import NewQuestion, QuestionType
    from prep.decks.repo import DeckRepo, QuestionRepo

    deck_id = DeckRepo().create(initialized_db, "qftest")
    qid = QuestionRepo().add(
        initialized_db,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt="?", answer="A"),
    )
    r = client.get(f"/question/{qid}/edit")
    assert r.status_code == 200, r.text[:200]
    assert "Save changes" in r.text
    # Edit form shows the answer-regex field.
    assert "Answer regex" in r.text
