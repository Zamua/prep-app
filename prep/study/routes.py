"""HTTP routes for the study bounded context.

Sessions, draft autosave, answer submission (sync + async grading),
advance, abandon, the no-session study flow, and the grading polling
page. All routes hold a SessionRepo / QuestionRepo / DeckRepo via
Depends, and call into prep.study.service for orchestration.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from prep import chat_handoff
from prep import db as _legacy_db
from prep.auth import current_user
from prep.decks.entities import DeckType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.domain import grading
from prep.study import service
from prep.study.entities import SessionState, SessionStatus
from prep.study.repo import ReviewRepo, SessionRepo
from prep.web import responses
from prep.web.templates import templates

router = APIRouter()


# ---- per-request repo dependencies --------------------------------------


def _session_repo() -> SessionRepo:
    return SessionRepo()


def _review_repo() -> ReviewRepo:
    return ReviewRepo()


def _deck_repo() -> DeckRepo:
    return DeckRepo()


def _question_repo() -> QuestionRepo:
    return QuestionRepo()


# ---- handoff context (chat-handoff payload for the result page) --------


def _handoff_ctx(
    *,
    deck_name: str,
    q: dict,
    user_answer: str,
    verdict: dict | None,
    idk: bool,
    picked_set: list[str],
    correct_set: list[str],
) -> dict:
    """Build the AI-chat-handoff payload that result.html embeds as data
    attributes. Same shape across all three result-rendering paths."""
    msg = chat_handoff.build_message(
        deck_name=deck_name,
        q=q,
        user_answer=user_answer,
        verdict=verdict,
        idk=idk,
        picked_set=picked_set,
        correct_set=correct_set,
    )
    return {
        "handoff_message": msg,
        "handoff_urls": chat_handoff.provider_urls(msg),
        "handoff_default_provider": chat_handoff.DEFAULT_PROVIDER,
        "handoff_providers": chat_handoff.CHAT_PROVIDERS,
    }


def _stale_response(request: Request, sid: str, current_version: int):
    """Render the 'session moved on another device' 409 page. Used by
    submit / advance handlers when the version check fails."""
    return templates.TemplateResponse(
        "session_stale.html",
        {
            "request": request,
            "session_id": sid,
            "current_version": current_version,
        },
        status_code=409,
    )


# ---- session lifecycle --------------------------------------------------


@router.post("/study/{name}/begin")
def session_begin(
    request: Request,
    name: str,
    fresh: int = 0,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
):
    """Auto-resume an active session on this deck, or create a fresh one.
    Pass ?fresh=1 to abandon any existing active session and start over."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.get_or_create(uid, name)
    # Trivia decks are notification-driven — they have no SRS state
    # and the per-card answer flow lives in /trivia/*. Refuse the
    # study path so a stale bookmark doesn't create an empty SRS
    # session against a deck that has no `cards` rows.
    if deck_repo.get_type(uid, deck_id) is DeckType.TRIVIA:
        raise HTTPException(400, "trivia decks are notification-driven; no study sessions")
    if not fresh:
        existing = service.find_active_session(session_repo, uid, deck_id)
        if existing:
            return responses.redirect(request, f"/session/{existing.id}")
    if fresh:
        existing = service.find_active_session(session_repo, uid, deck_id)
        if existing:
            service.abandon_session(session_repo, uid, existing.id)
    label = session_repo.device_label_from_ua(request.headers.get("user-agent"))
    sid = service.start_session(session_repo, uid, deck_id, label)
    return responses.redirect(request, f"/session/{sid}")


@router.get("/session/{sid}", response_class=HTMLResponse)
def session_view(
    request: Request,
    sid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Render the session — branches by status + state."""
    uid = user["tailscale_login"]
    s = service.get_session(session_repo, uid, sid)
    if s is None:
        raise HTTPException(404, "session not found")
    deck_name = deck_repo.find_name(uid, s.deck_id) or ""

    # Terminal status branches.
    if s.status is SessionStatus.COMPLETED:
        return templates.TemplateResponse(
            "session_completed.html",
            {"request": request, "session": s.model_dump(), "deck_name": deck_name},
        )
    if s.status is SessionStatus.ABANDONED:
        return responses.redirect(request, f"/deck/{deck_name}")

    # showing-result: render the post-answer view from the cached
    # verdict + state on the session row.
    if s.state is SessionState.SHOWING_RESULT:
        qid = s.last_answered_qid
        q_entity = q_repo.get(uid, qid) if qid else None
        q = q_entity.model_dump() if q_entity is not None else None
        if q is not None and q_entity is not None:
            # Templates expect choices_list (a list, not a JSON string).
            q["choices_list"] = q_entity.choices or []
        verdict = s.last_answered_verdict or {}
        st = s.last_answered_state or {}
        # Pull the most recent user_answer from reviews — single source
        # of truth, no extra column needed.
        with _legacy_db.cursor() as c:
            r = c.execute(
                "SELECT user_answer FROM reviews WHERE question_id = ? ORDER BY id DESC LIMIT 1",
                (qid,),
            ).fetchone()
            user_answer = r["user_answer"] if r else ""
        idk = user_answer == ""
        picked_set, correct_set = _picked_correct_sets(q, user_answer)
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "deck_name": deck_name,
                "q": q,
                "user_answer": user_answer,
                "idk": idk,
                "verdict": verdict,
                "state": st,
                "picked_set": picked_set,
                "correct_set": correct_set,
                "session_id": sid,
                "session_version": s.version,
                **_handoff_ctx(
                    deck_name=deck_name,
                    q=q,
                    user_answer=user_answer,
                    verdict=verdict,
                    idk=idk,
                    picked_set=picked_set,
                    correct_set=correct_set,
                ),
            },
        )

    # grading: bounce to the polling page; on completion the
    # /grading/{wid} handler reconciles back into the session.
    if s.state is SessionState.GRADING:
        return responses.redirect(request, f"/grading/{s.current_grading_workflow_id}")

    # awaiting-answer: render the session-aware study card. If no due
    # card is left, transition to completed via a synchronous bump.
    q_entity = q_repo.get(uid, s.current_question_id) if s.current_question_id else None
    if q_entity is None:
        with _legacy_db.cursor() as c:
            c.execute(
                "UPDATE study_sessions SET status='completed', "
                "       version = version + 1, last_active = ? "
                " WHERE id = ? AND user_id = ?",
                (_legacy_db.now(), sid, uid),
            )
        return responses.redirect(request, f"/session/{sid}")
    q = q_entity.model_dump()
    q["choices_list"] = q_entity.choices or []
    return templates.TemplateResponse(
        "session.html",
        {
            "request": request,
            "user": user,
            "session": s.model_dump(),
            "deck_name": deck_name,
            "q": q,
            "draft": s.current_draft or (q_entity.skeleton or ""),
        },
    )


def _picked_correct_sets(q: dict | None, user_answer: str) -> tuple[list[str], list[str]]:
    """Mcq/multi need parsed picked + correct lists for the answer-grid
    rendering on the result page. Anything else gets empty lists."""
    if q is None:
        return [], []
    qtype = q.get("type")
    if qtype not in ("mcq", "multi"):
        return [], []
    try:
        if qtype == "multi":
            picked = json.loads(user_answer) if user_answer else []
            correct = json.loads(q["answer"]) if q.get("answer") else []
        else:
            picked = [user_answer] if user_answer else []
            correct = [q["answer"]] if q.get("answer") else []
        return picked, correct
    except (json.JSONDecodeError, TypeError):
        return [], []


# ---- session mutations --------------------------------------------------


@router.post("/session/{sid}/draft")
async def session_draft(
    request: Request,
    sid: str,
    user: dict = Depends(current_user),
    session_repo: SessionRepo = Depends(_session_repo),
):
    """Autosave endpoint. Body: {version: int, draft: str}.
    Returns {version: new} or 409 with {current_version: int}."""
    body = await request.json()
    try:
        new_v = service.update_draft(
            session_repo,
            user["tailscale_login"],
            sid,
            body.get("draft", ""),
            int(body["version"]),
        )
    except _legacy_db.StaleVersionError as e:
        return JSONResponse(
            {"error": "stale", "current_version": e.current_version},
            status_code=409,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return JSONResponse({"version": new_v})


@router.post("/session/{sid}/submit", response_class=HTMLResponse)
async def session_submit(
    request: Request,
    sid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Submit an answer for the current card. Branches by qtype +
    agent availability between the synchronous grading path (mcq /
    multi / idk) and the temporal-orchestrated grading path
    (code / short, when an agent is connected) — falls back to a
    self-grade form when no agent is around."""
    form = await request.form()
    qtype = form["type"]
    qid = int(form["question_id"])
    expected_version = int(form["version"])
    idk = form.get("idk") == "1"
    uid = user["tailscale_login"]

    s = service.get_session(session_repo, uid, sid)
    if s is None:
        raise HTTPException(404, "session not found")

    user_answer = _read_user_answer(form, qtype, idk)

    question_entity = q_repo.get(uid, qid)
    if question_entity is None:
        raise HTTPException(404, "question not found")
    question = question_entity.model_dump()
    question["choices_list"] = question_entity.choices or []

    # Slow path: code/short go through GradeAnswer workflow when an
    # agent is connected; otherwise render a self-grade form so the
    # session keeps advancing manually.
    from prep import agent as _agent_mod

    if qtype in ("code", "short") and not idk:
        deck_name = deck_repo.find_name(uid, s.deck_id) or ""

        if not _agent_mod.is_available:
            return templates.TemplateResponse(
                "self_grade.html",
                {
                    "request": request,
                    "deck_name": deck_name,
                    "q": question,
                    "user_answer": user_answer,
                    "session_id": sid,
                    "session_version": expected_version,
                },
            )
        from prep import temporal_client

        try:
            wid = await service.start_grading(
                temporal_client,
                session_repo,
                user_id=uid,
                sid=sid,
                qid=qid,
                deck_name=deck_name,
                expected_version=expected_version,
                user_answer=user_answer,
                idk=idk,
            )
        except _legacy_db.StaleVersionError as e:
            return _stale_response(request, sid, e.current_version)
        except Exception as e:
            raise HTTPException(500, f"failed to start grading workflow: {e}")
        return responses.redirect(request, f"/grading/{wid}?sid={sid}")

    # Fast path: idk / mcq / multi grade synchronously.
    verdict = grading.grade(question, user_answer, idk=idk)
    try:
        service.submit_sync_answer(
            session_repo,
            review_repo,
            user_id=uid,
            sid=sid,
            qid=qid,
            expected_version=expected_version,
            user_answer=user_answer,
            verdict=verdict,
        )
    except _legacy_db.StaleVersionError as e:
        return _stale_response(request, sid, e.current_version)
    return responses.redirect(request, f"/session/{sid}")


def _read_user_answer(form: Any, qtype: str, idk: bool) -> str:
    """Mcq/multi/idk read different form fields; centralize here so the
    submit + the no-session study path don't duplicate the logic."""
    if idk:
        return ""
    if qtype == "mcq":
        return form.get("choice", "")
    if qtype == "multi":
        return json.dumps(sorted(form.getlist("choice")))
    return form.get("answer", "")


@router.post("/session/{sid}/advance")
async def session_advance(
    request: Request,
    sid: str,
    user: dict = Depends(current_user),
    session_repo: SessionRepo = Depends(_session_repo),
):
    form = await request.form()
    expected_version = int(form["version"])
    try:
        service.advance_session(session_repo, user["tailscale_login"], sid, expected_version)
    except _legacy_db.StaleVersionError as e:
        return _stale_response(request, sid, e.current_version)
    return responses.redirect(request, f"/session/{sid}")


@router.post("/session/{sid}/abandon")
def session_abandon(
    request: Request,
    sid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
):
    """Manually kill a session. Returns the user to the deck page."""
    uid = user["tailscale_login"]
    s = service.get_session(session_repo, uid, sid)
    service.abandon_session(session_repo, uid, sid)
    deck_name = ""
    if s is not None:
        deck_name = deck_repo.find_name(uid, s.deck_id) or ""
    return responses.redirect(request, f"/deck/{deck_name}" if deck_name else "/")


# ---- legacy no-session study path --------------------------------------


@router.get("/study/{name}", response_class=HTMLResponse)
def study(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Older single-card study path (no session). Picks one due card
    from the deck and renders it; submission goes to the matching
    POST. Most users land on /study/{name}/begin which spins up a
    session instead, but this path stays in the surface for direct
    use + dev-tooling."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.get_or_create(uid, name)
    due = _legacy_db.due_questions(uid, deck_id, limit=1)
    if not due:
        return templates.TemplateResponse(
            "study_empty.html",
            {"request": request, "deck_name": name},
        )
    return templates.TemplateResponse(
        "study.html",
        {"request": request, "user": user, "deck_name": name, "q": due[0]},
    )


@router.post("/study/{name}", response_class=HTMLResponse)
async def study_submit(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    review_repo: ReviewRepo = Depends(_review_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Single-card answer submission (no session). Same sync/async
    branching as session_submit, but no session row to mutate."""
    form = await request.form()
    qid = int(form["question_id"])
    qtype = form["type"]
    idk = form.get("idk") == "1"
    uid = user["tailscale_login"]

    user_answer = _read_user_answer(form, qtype, idk)

    question_entity = q_repo.get(uid, qid)
    if question_entity is None:
        raise HTTPException(404, "question not found")
    question = question_entity.model_dump()
    question["choices_list"] = question_entity.choices or []

    from prep import agent as _agent_mod

    if qtype in ("code", "short") and not idk:
        if not _agent_mod.is_available:
            return templates.TemplateResponse(
                "self_grade.html",
                {
                    "request": request,
                    "deck_name": name,
                    "q": question,
                    "user_answer": user_answer,
                    "session_id": None,
                    "session_version": None,
                },
            )
        from prep import temporal_client

        try:
            res = await temporal_client.start_grading(qid, name, user_answer, idk, user_id=uid)
        except Exception as e:
            raise HTTPException(500, f"failed to start grading workflow: {e}")
        return responses.redirect(request, f"/grading/{res.workflow_id}")

    # Fast path: idk + mcq/multi.
    verdict = grading.grade(question, user_answer, idk=idk)
    state_dict = review_repo.record(
        uid, qid, verdict["result"], user_answer, notes=verdict.get("feedback") or ""
    ).model_dump()

    picked_set, correct_set = _picked_correct_sets(question, user_answer)

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "deck_name": name,
            "q": question,
            "user_answer": user_answer,
            "idk": idk,
            "verdict": verdict,
            "state": state_dict,
            "picked_set": picked_set,
            "correct_set": correct_set,
            "session_id": None,
            "session_version": None,
            **_handoff_ctx(
                deck_name=name,
                q=question,
                user_answer=user_answer,
                verdict=verdict,
                idk=idk,
                picked_set=picked_set,
                correct_set=correct_set,
            ),
        },
    )


# ---- grading workflow polling page ------------------------------------


def _parse_grading_wid(wid: str) -> tuple[str, int] | None:
    """`grade-<deck>-q<qid>-<rand>`. Walks from the right since deck
    names may themselves contain hyphens. Returns (deck_name, qid)."""
    if not wid.startswith("grade-"):
        return None
    parts = wid[len("grade-") :].split("-")
    if len(parts) < 3:
        return None
    qid_part = parts[-2]
    if not qid_part.startswith("q"):
        return None
    try:
        qid = int(qid_part[1:])
    except ValueError:
        return None
    deck_name = "-".join(parts[:-2])
    return deck_name, qid


@router.get("/grading/{wid}", response_class=HTMLResponse)
async def grading_view(
    request: Request,
    wid: str,
    sid: str = "",
    user: dict = Depends(current_user),
    session_repo: SessionRepo = Depends(_session_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Grading polling page. While the workflow is alive, renders the
    spinner; on terminal completion, renders the same result.html the
    sync path uses (verdict + state). If a session id is in the query
    string, reconciles the session row first."""
    parsed = _parse_grading_wid(wid)
    if not parsed:
        raise HTTPException(400, "malformed workflow id")
    deck_name, qid = parsed
    uid = user["tailscale_login"]

    # Ownership gate — the wid embeds qid + deck, but we still check
    # that the user owns the question. Otherwise user A can poll user
    # B's grading by guessing the wid format.
    if q_repo.get(uid, qid) is None:
        raise HTTPException(404, "question not found")

    from prep import temporal_client

    progress = await temporal_client.get_grade_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    status = (progress or {}).get("status") or (desc or {}).get("status") or "unknown"

    terminal = status in {"done", "failed", "COMPLETED", "FAILED", "CANCELED", "TERMINATED"}
    if terminal:
        result = await temporal_client.get_grade_result(wid)
        if result is None:
            return templates.TemplateResponse(
                "grading.html",
                {
                    "request": request,
                    "wid": wid,
                    "deck_name": deck_name,
                    "progress": progress,
                    "desc": desc,
                    "failed": True,
                },
            )
        question_entity = q_repo.get(uid, qid)
        if question_entity is None:
            raise HTTPException(404, "question not found")
        question = question_entity.model_dump()
        question["choices_list"] = question_entity.choices or []

        verdict = result["verdict"]
        state_raw = result["state"]
        state = {
            "step": state_raw["step"],
            "next_due": state_raw["next_due"],
            "interval_minutes": state_raw["interval_minutes"],
        }

        # Session reconciliation: if grading was started from a session,
        # the session is sitting in state='grading' waiting for us to
        # transition it to 'showing-result'. service.grading_landed is
        # idempotent — safe on every render.
        if sid:
            service.grading_landed(
                session_repo,
                user_id=uid,
                sid=sid,
                question_id=qid,
                workflow_id=wid,
                verdict=verdict,
                state=state,
            )
            # Subsequent loads should land on the session URL.
            return responses.redirect(request, f"/session/{sid}")

        # No session — render the result.html directly.
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "deck_name": deck_name,
                "q": question,
                "user_answer": result["user_answer"],
                "idk": result["idk"],
                "verdict": verdict,
                "state": state,
                "picked_set": [],
                "correct_set": [],
                **_handoff_ctx(
                    deck_name=deck_name,
                    q=question,
                    user_answer=result["user_answer"],
                    verdict=verdict,
                    idk=result["idk"],
                    picked_set=[],
                    correct_set=[],
                ),
            },
        )

    # Still grading — render the polling page.
    return templates.TemplateResponse(
        "grading.html",
        {
            "request": request,
            "wid": wid,
            "deck_name": deck_name,
            "progress": progress,
            "desc": desc,
            "failed": False,
            "sid": sid,
        },
    )


@router.get("/grading/{wid}/status")
async def grading_status(
    wid: str,
    user: dict = Depends(current_user),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """JSON status for the grading polling page."""
    parsed = _parse_grading_wid(wid)
    if not parsed:
        raise HTTPException(400, "malformed workflow id")
    _, qid = parsed
    if q_repo.get(user["tailscale_login"], qid) is None:
        raise HTTPException(404, "question not found")
    from prep import temporal_client

    progress = await temporal_client.get_grade_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    return JSONResponse({"progress": progress, "desc": desc})


# ---- self-grade (sync grading for code/short when no agent) -----------


@router.post("/study/{name}/self-grade/{qid}")
async def study_self_grade(
    request: Request,
    name: str,
    qid: int,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
):
    """User-driven grading for code/short submissions in no-agent mode.
    Posts {verdict: right|wrong, user_answer, session_id?, session_version?}."""
    uid = user["tailscale_login"]
    form = await request.form()
    verdict_str = form.get("verdict", "")
    if verdict_str not in ("right", "wrong"):
        raise HTTPException(422, "verdict must be 'right' or 'wrong'")
    user_answer = form.get("user_answer", "")
    sid = form.get("session_id") or None
    sver = form.get("session_version") or None

    verdict = {"result": verdict_str, "feedback": "(self-graded)"}
    state = review_repo.record(uid, qid, verdict_str, user_answer, notes="(self-graded)")

    if sid and sver:
        try:
            session_repo.record_answer_sync(
                uid,
                sid,
                qid,
                int(sver),
                user_answer,
                verdict,
                state.model_dump(),
            )
        except _legacy_db.StaleVersionError as e:
            return _stale_response(request, sid, e.current_version)
        return responses.redirect(request, f"/session/{sid}")
    # No session — back to the deck page.
    if not deck_repo.find_id(uid, name):
        # Defensive: deck name might be stale, but find_id returns None
        # so we just fall through to redirecting to /deck/<name>.
        pass
    return responses.redirect(request, f"/deck/{name}")
