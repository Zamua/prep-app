"""FastAPI app for the interview-prep flashcard tool.

Routes:
  GET  /                                -> deck list
  GET  /deck/{name}                     -> deck overview (questions + Add More + Study)
  POST /deck/{name}/add                 -> kick off a Temporal workflow that generates N cards;
                                           redirects to the generation status page
  GET  /generation/{wid}                -> live progress page (polls the workflow's getProgress query)
  GET  /generation/{wid}/status         -> JSON progress (consumed by the polling JS)
  POST /generation/{wid}/cancel         -> send the cancelGeneration signal
  GET  /study/{name}                    -> next due card
  POST /study/{name}                    -> submit answer, grade, advance SRS
  POST /question/{id}/suspend / unsuspend
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import generator
import grader
import temporal_client

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# When fronted by Caddy at a path prefix, set ROOT_PATH so generated URLs include it.
import os
ROOT_PATH = os.environ.get("ROOT_PATH", "")

app = FastAPI(root_path=ROOT_PATH)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    """Build a RedirectResponse whose Location header includes the request's
    root_path. FastAPI's RedirectResponse takes the URL verbatim — it does
    NOT auto-prepend root_path — so a bare /deck/foo would land outside the
    /prep/ Caddy route and the user gets a white screen. Hit on 2026-04-26.
    """
    prefix = request.scope.get("root_path", "") or ""
    if path.startswith("/"):
        return RedirectResponse(f"{prefix}{path}", status_code=status_code)
    return RedirectResponse(f"{prefix}/{path}", status_code=status_code)

db.init()
# Seed the known decks at boot so they show up even before any questions exist.
for known in generator.DECK_CONTEXT:
    db.get_or_create_deck(known)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "decks": db.list_decks()},
    )


@app.get("/deck/{name}", response_class=HTMLResponse)
def deck_view(request: Request, name: str):
    deck_id = db.get_or_create_deck(name)
    questions = db.list_questions(deck_id)
    return templates.TemplateResponse(
        "deck.html",
        {
            "request": request,
            "deck_name": name,
            "questions": questions,
            "due_count": sum(1 for q in questions
                              if not q["suspended"] and q["next_due"] and q["next_due"] <= db.now()),
        },
    )


@app.post("/deck/{name}/add")
async def deck_add(request: Request, name: str, count: int = Form(5)):
    if name not in generator.DECK_CONTEXT:
        raise HTTPException(400, f"Unknown deck '{name}'. Add it to generator.DECK_CONTEXT.")
    count = max(1, min(count, 15))
    try:
        result = await temporal_client.start_generation(name, count)
    except Exception as e:
        raise HTTPException(500, f"failed to start workflow: {e}")
    return _redirect(request, f"/generation/{result.workflow_id}")


@app.get("/generation/{wid}", response_class=HTMLResponse)
async def generation_view(request: Request, wid: str):
    progress = await temporal_client.get_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    deck_name = wid.split("-", 2)[1] if wid.startswith("gen-") else ""
    return templates.TemplateResponse(
        "generation.html",
        {
            "request": request,
            "wid": wid,
            "deck_name": deck_name,
            "progress": progress,
            "desc": desc,
        },
    )


@app.get("/generation/{wid}/status")
async def generation_status(wid: str):
    progress = await temporal_client.get_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    return JSONResponse({"progress": progress, "desc": desc})


@app.post("/generation/{wid}/cancel")
async def generation_cancel(request: Request, wid: str):
    try:
        await temporal_client.cancel_generation(wid)
    except Exception as e:
        raise HTTPException(500, f"cancel failed: {e}")
    return _redirect(request, f"/generation/{wid}")


@app.get("/study/{name}", response_class=HTMLResponse)
def study(request: Request, name: str):
    deck_id = db.get_or_create_deck(name)
    due = db.due_questions(deck_id, limit=1)
    if not due:
        return templates.TemplateResponse(
            "study_empty.html",
            {"request": request, "deck_name": name},
        )
    return templates.TemplateResponse(
        "study.html",
        {"request": request, "deck_name": name, "q": due[0]},
    )


@app.post("/study/{name}", response_class=HTMLResponse)
async def study_submit(request: Request, name: str):
    form = await request.form()
    qid = int(form["question_id"])
    qtype = form["type"]
    idk = form.get("idk") == "1"

    if idk:
        user_answer = ""
    elif qtype == "mcq":
        user_answer = form.get("choice", "")
    elif qtype == "multi":
        user_answer = json.dumps(sorted(form.getlist("choice")))
    else:
        user_answer = form.get("answer", "")

    question = db.get_question(qid)
    if not question:
        raise HTTPException(404, "question not found")

    verdict = grader.grade(question, user_answer, idk=idk)
    state = db.record_review(
        qid, verdict["result"], user_answer,
        notes=verdict.get("feedback", ""),
    )

    # Pre-parse user/correct answers for the template so it can render type-
    # appropriate UI without doing JSON-decoding inside Jinja.
    picked_set: list[str] = []
    correct_set: list[str] = []
    if qtype in ("mcq", "multi"):
        try:
            if qtype == "multi":
                picked_set = json.loads(user_answer) if user_answer else []
                correct_set = json.loads(question["answer"]) if question.get("answer") else []
            else:  # mcq
                picked_set = [user_answer] if user_answer else []
                correct_set = [question["answer"]] if question.get("answer") else []
        except (json.JSONDecodeError, TypeError):
            picked_set, correct_set = [], []

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "deck_name": name,
            "q": question,
            "user_answer": user_answer,
            "idk": idk,
            "verdict": verdict,
            "state": state,
            # For mcq/multi: parsed sets; template uses them to colour each choice.
            "picked_set": picked_set,
            "correct_set": correct_set,
        },
    )


@app.post("/question/{qid}/suspend")
def suspend(request: Request, qid: int):
    db.set_suspended(qid, True)
    q = db.get_question(qid)
    deck_id = q["deck_id"]
    with db.cursor() as c:
        name = c.execute("SELECT name FROM decks WHERE id=?", (deck_id,)).fetchone()["name"]
    return _redirect(request, f"/deck/{name}")


@app.post("/question/{qid}/unsuspend")
def unsuspend(request: Request, qid: int):
    db.set_suspended(qid, False)
    q = db.get_question(qid)
    deck_id = q["deck_id"]
    with db.cursor() as c:
        name = c.execute("SELECT name FROM decks WHERE id=?", (deck_id,)).fetchone()["name"]
    return _redirect(request, f"/deck/{name}")
