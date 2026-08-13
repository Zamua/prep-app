"""JSON study API: the surface the browser study components drive.

Shapes mirror the CardSource contract in static/js/study/source.js so
a client adapter stays a thin translation:

  next   -> {card, draft} | {caughtUp: {nextDueMinutes}}
  submit -> {verdict, nextDueMinutes, idk, card} | {selfGrade: true}
          | {pending: {poll}}          the Temporal-graded path
  author -> {card}

Orchestration lives in prep.study.service, the same functions the
HTML routes call. These handlers translate JSON to a service call and
back; no study logic is re-implemented here.

Every handler is auth-gated by `current_user` and every repo call is
user-scoped, so a guessed session / deck / question id answers 404
exactly as a missing one does.

Response conventions:
  - a non-2xx body is {"error": {"code", "message", ...}}
  - a 200 body carries exactly one outcome key: card, caughtUp,
    pending, verdict, selfGrade, failed, or ended
  - a malformed request body takes the app's standard 422 path
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from prep import chat_handoff
from prep.auth import current_user
from prep.decks import service as decks_service
from prep.decks.entities import DeckType, NewQuestion, Question, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.domain import grading
from prep.study import service
from prep.study.entities import RevealedCard, SessionState, SessionStatus, StudyCard, StudySession
from prep.study.repo import ReviewRepo, SessionRepo, StaleVersionError
from prep.study.service import parse_grading_wid

router = APIRouter(prefix="/api/study")

# Workflow states past which no further verdict will arrive.
_TERMINAL = {"done", "failed", "COMPLETED", "FAILED", "CANCELED", "TERMINATED"}

# Free-text types have no deterministic grader: they go to the
# Temporal judge when an agent is configured, else to self-grade.
_FREE_TEXT = {"code", "short"}

_INBOX_DECK = "inbox"


# ---- per-request repo dependencies --------------------------------------


def _session_repo() -> SessionRepo:
    return SessionRepo()


def _review_repo() -> ReviewRepo:
    return ReviewRepo()


def _deck_repo() -> DeckRepo:
    return DeckRepo()


def _question_repo() -> QuestionRepo:
    return QuestionRepo()


# ---- request bodies -----------------------------------------------------


class BeginBody(BaseModel):
    """`fresh` abandons any open session on the deck and starts over."""

    fresh: bool = False


class SubmitBody(BaseModel):
    """One answer submission. Exactly one of the three modes applies:
    `verdict` (a self-grade decision), `idk`, or a plain `answer`.
    `version` is the session's optimistic-concurrency token and is
    required on the session routes, ignored on the deck routes."""

    question_id: int
    version: int | None = None
    answer: str = ""
    idk: bool = False
    verdict: str | None = None


class VersionBody(BaseModel):
    version: int


class DraftBody(BaseModel):
    version: int
    draft: str = ""


class SnoozeBody(BaseModel):
    """Either a `preset` or a `custom` + `unit` pair; `preset=wake`
    clears an existing snooze."""

    preset: str | None = None
    custom: str | None = None
    unit: str | None = None


class AuthorBody(BaseModel):
    """A manually written card. `deck_id` null files it into the
    inbox deck, matching the offline author flow."""

    prompt: str
    answer: str
    deck_id: int | None = None


# ---- error + payload builders ------------------------------------------


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", f"{what} not found")


def _stale(e: StaleVersionError) -> JSONResponse:
    return _error(
        409,
        "stale_version",
        "session moved on another device",
        current_version=e.current_version,
    )


def _dump_card(card: StudyCard) -> dict:
    """Serialize a card payload. `topic` is dropped when unset (the
    offline snapshot card has no such field, and the views branch on
    its presence); every other optional stays present as null so the
    two sources hand the components an identical key set."""
    payload = card.model_dump()
    if payload.get("topic") is None:
        payload.pop("topic", None)
    return payload


def _card(q: Question) -> dict:
    return _dump_card(
        StudyCard(
            question_id=q.id,
            deck_id=q.deck_id,
            type=q.type.value,
            prompt=q.prompt,
            choices=q.choices or None,
            skeleton=q.skeleton,
            language=q.language,
            topic=q.topic,
        )
    )


def _revealed(q: Question) -> dict:
    return _dump_card(
        RevealedCard(
            question_id=q.id,
            deck_id=q.deck_id,
            type=q.type.value,
            prompt=q.prompt,
            choices=q.choices or None,
            skeleton=q.skeleton,
            language=q.language,
            topic=q.topic,
            answer=q.answer,
            rubric=q.rubric,
        )
    )


def _picked_correct(q: Question, user_answer: str, idk: bool) -> tuple[list[str], list[str]]:
    """Chosen and correct option lists for mcq/multi, empty for every
    other type. `multi` stores both sides as a JSON array string."""
    qtype = q.type.value
    if qtype not in ("mcq", "multi"):
        return [], []
    blank = idk or not user_answer
    try:
        if qtype == "multi":
            picked = [] if blank else json.loads(user_answer)
            correct = json.loads(q.answer) if q.answer else []
        else:
            picked = [] if blank else [user_answer]
            correct = [q.answer] if q.answer else []
    except (json.JSONDecodeError, TypeError):
        return [], []
    if not isinstance(picked, list) or not isinstance(correct, list):
        return [], []
    return [str(p) for p in picked], [str(c) for c in correct]


def _handoff(q: Question, *, deck_name: str, verdict: dict, user_answer: str, idk: bool) -> dict:
    """The "discuss this card" payload: one prefilled chat URL per
    provider. Composed here because the message embeds the whole card
    context and the provider URL templates are server-side config."""
    payload = q.model_dump()
    payload["choices_list"] = q.choices or []
    picked, correct = _picked_correct(q, user_answer, idk)
    message = chat_handoff.build_message(
        deck_name=deck_name,
        q=payload,
        user_answer=user_answer,
        verdict=verdict,
        idk=idk,
        picked_set=picked,
        correct_set=correct,
    )
    return {
        "message": message,
        "urls": chat_handoff.provider_urls(message),
        "providers": {k: v["label"] for k, v in chat_handoff.CHAT_PROVIDERS.items()},
        "default": chat_handoff.DEFAULT_PROVIDER,
    }


def _session_payload(s: StudySession, deck_name: str) -> dict:
    return {
        "id": s.id,
        "version": s.version,
        "status": s.status.value,
        "state": s.state.value,
        "deck_id": s.deck_id,
        "deck_name": deck_name,
    }


def _poll_url(request: Request, wid: str, sid: str = "") -> str:
    """Absolute-from-root polling URL, root_path aware so the client
    can fetch it verbatim behind a path-mounted deploy."""
    prefix = request.scope.get("root_path", "") or ""
    url = f"{prefix}/api/study/grading/{wid}"
    return f"{url}?sid={sid}" if sid else url


def _verdict_outcome(
    q: Question,
    verdict: dict,
    state: dict,
    user_answer: str,
    idk: bool,
    session: dict | None,
    deck_name: str = "",
) -> dict:
    """The recorded-verdict shape: what the verdict view renders."""
    return {
        "verdict": verdict.get("result"),
        "feedback": verdict.get("feedback") or "",
        "nextDueMinutes": state.get("interval_minutes"),
        "idk": idk,
        "answer": user_answer,
        "card": _revealed(q),
        "session": session,
        "handoff": _handoff(
            q, deck_name=deck_name, verdict=verdict, user_answer=user_answer, idk=idk
        ),
    }


def _caught_up(review_repo: ReviewRepo, uid: str, deck_id: int, session: dict | None) -> dict:
    return {
        "caughtUp": {"nextDueMinutes": review_repo.next_due_minutes(uid, deck_id)},
        "session": session,
    }


# ---- session view -------------------------------------------------------


def _session_view(
    request: Request,
    uid: str,
    s: StudySession,
    *,
    deck_repo: DeckRepo,
    session_repo: SessionRepo,
    review_repo: ReviewRepo,
    q_repo: QuestionRepo,
) -> JSONResponse:
    """What the client should render for this session right now.

    Branches exactly like the HTML session view, so a session picked up
    on another device resumes into the same screen: a card to answer, a
    verdict already waiting, an in-flight grade, or caught-up."""
    deck_name = deck_repo.find_name(uid, s.deck_id) or ""
    payload = _session_payload(s, deck_name)

    if s.status is SessionStatus.ABANDONED:
        return JSONResponse({"ended": {"reason": "abandoned"}, "session": payload})
    if s.status is SessionStatus.COMPLETED:
        return JSONResponse(_caught_up(review_repo, uid, s.deck_id, payload))

    if s.state is SessionState.SHOWING_RESULT:
        q = q_repo.get(uid, s.last_answered_qid) if s.last_answered_qid else None
        if q is None:
            return _not_found("question")
        user_answer = review_repo.get_last_user_answer(q.id) or ""
        return JSONResponse(
            _verdict_outcome(
                q,
                s.last_answered_verdict or {},
                s.last_answered_state or {},
                user_answer,
                user_answer == "",
                payload,
                deck_name,
            )
        )

    if s.state is SessionState.GRADING:
        wid = s.current_grading_workflow_id or ""
        return JSONResponse(
            {
                "pending": {"poll": _poll_url(request, wid, s.id), "workflow_id": wid},
                "session": payload,
            }
        )

    q = q_repo.get(uid, s.current_question_id) if s.current_question_id else None
    if q is None:
        # No due card left: the session is done. Same synchronous bump
        # the HTML view does, so both surfaces agree on the row.
        session_repo.mark_completed(uid, s.id)
        done = service.get_session(session_repo, uid, s.id)
        payload = _session_payload(done, deck_name) if done else payload
        return JSONResponse(_caught_up(review_repo, uid, s.deck_id, payload))

    return JSONResponse(
        {"card": _card(q), "draft": s.current_draft or (q.skeleton or ""), "session": payload}
    )


# ---- submission ---------------------------------------------------------


async def _submit(
    request: Request,
    uid: str,
    body: SubmitBody,
    *,
    q: Question,
    deck_name: str,
    s: StudySession | None,
    session_repo: SessionRepo,
    review_repo: ReviewRepo,
    deck_repo: DeckRepo,
) -> JSONResponse:
    """One answer, three outcomes: a recorded verdict, a reveal to
    self-grade, or a started grading workflow to poll.

    `s` None is the deck-scoped (no session) path: reviews are recorded
    but no session row moves."""
    qtype = q.type.value
    idk = bool(body.idk)
    session = _session_payload(s, deck_name) if s is not None else None
    if s is not None and body.version is None:
        return _error(422, "version_required", "session submissions must carry the version")

    # A self-grade decision arrives after the reveal, so it is already
    # a verdict: record it like any deterministic one.
    if body.verdict is not None:
        if body.verdict not in ("right", "wrong"):
            return _error(422, "invalid_verdict", "verdict must be 'right' or 'wrong'")
        verdict = {"result": body.verdict, "feedback": "(self-graded)"}
        return _record(
            uid,
            body,
            q=q,
            verdict=verdict,
            user_answer=body.answer,
            idk=False,
            s=s,
            session_repo=session_repo,
            review_repo=review_repo,
            deck_repo=deck_repo,
        )

    user_answer = "" if idk else body.answer

    if qtype in _FREE_TEXT and not idk:
        from prep import agent as agent_mod

        if not agent_mod.is_available_for(uid):
            return JSONResponse(
                {"selfGrade": True, "answer": user_answer, "card": _revealed(q), "session": session}
            )
        from prep import temporal_client

        try:
            if s is not None:
                wid = await service.start_grading(
                    temporal_client,
                    session_repo,
                    user_id=uid,
                    sid=s.id,
                    qid=q.id,
                    deck_name=deck_name,
                    expected_version=body.version,
                    user_answer=user_answer,
                    idk=idk,
                )
            else:
                wid = await _start_grading_no_session(
                    temporal_client, uid, q.id, deck_name, user_answer, idk
                )
        except StaleVersionError as e:
            return _stale(e)
        except Exception as e:
            return _error(502, "grading_start_failed", f"failed to start grading workflow: {e}")
        sid = s.id if s is not None else ""
        moved = service.get_session(session_repo, uid, sid) if s is not None else None
        return JSONResponse(
            {
                "pending": {"poll": _poll_url(request, wid, sid), "workflow_id": wid},
                "session": _session_payload(moved, deck_name) if moved else session,
            }
        )

    try:
        verdict = grading.grade(q.model_dump(), user_answer, idk=idk)
    except ValueError as e:
        return _error(422, "not_gradable", str(e))
    return _record(
        uid,
        body,
        q=q,
        verdict=verdict,
        user_answer=user_answer,
        idk=idk,
        s=s,
        session_repo=session_repo,
        review_repo=review_repo,
        deck_repo=deck_repo,
    )


def _record(
    uid: str,
    body: SubmitBody,
    *,
    q: Question,
    verdict: dict,
    user_answer: str,
    idk: bool,
    s: StudySession | None,
    session_repo: SessionRepo,
    review_repo: ReviewRepo,
    deck_repo: DeckRepo,
) -> JSONResponse:
    """Write a settled verdict, then reply with the recorded outcome."""
    if s is None:
        state = review_repo.record(
            uid, q.id, verdict["result"], user_answer, notes=verdict.get("feedback") or ""
        )
        return JSONResponse(
            _verdict_outcome(q, verdict, state.model_dump(), user_answer, idk, None)
        )
    try:
        state, _ = service.submit_sync_answer(
            session_repo,
            review_repo,
            user_id=uid,
            sid=s.id,
            qid=q.id,
            expected_version=body.version,
            user_answer=user_answer,
            verdict=verdict,
        )
    except StaleVersionError as e:
        return _stale(e)
    moved = service.get_session(session_repo, uid, s.id)
    deck_name = deck_repo.find_name(uid, s.deck_id) or ""
    return JSONResponse(
        _verdict_outcome(
            q,
            verdict,
            state.model_dump(),
            user_answer,
            idk,
            _session_payload(moved, deck_name) if moved else None,
        )
    )


async def _start_grading_no_session(
    client: Any, uid: str, qid: int, deck_name: str, user_answer: str, idk: bool
) -> str:
    """Deck-scoped grading start: no session row to mark, but the
    masthead badge still needs the workflow registered."""
    res = await client.start_grading(qid, deck_name, user_answer, idk, user_id=uid)
    from prep.workflows import service as workflows_service
    from prep.workflows.entities import WorkflowType

    workflows_service.register(
        user_login=uid,
        workflow_id=res.workflow_id,
        workflow_type=WorkflowType.GRADING,
        deck_id=None,
        deck_name=deck_name,
        url_path=f"/grading/{res.workflow_id}",
        initial_status="grading",
    )
    return res.workflow_id


# ---- session lifecycle --------------------------------------------------


@router.post("/decks/{name}/session", include_in_schema=False)
def begin(
    request: Request,
    name: str,
    body: BeginBody | None = None,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
) -> JSONResponse:
    """Resume the open session on this deck, or start a fresh one, and
    return the first view. `fresh` abandons the open one first."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.get_or_create(uid, name)
    if deck_repo.get_type(uid, deck_id) is DeckType.TRIVIA:
        return _error(400, "not_studiable", "trivia decks have no study sessions")
    fresh = bool(body and body.fresh)
    existing = service.find_active_session(session_repo, uid, deck_id)
    if existing and not fresh:
        s = existing
    else:
        if existing:
            service.abandon_session(session_repo, uid, existing.id)
        label = session_repo.device_label_from_ua(request.headers.get("user-agent"))
        sid = service.start_session(session_repo, uid, deck_id, label)
        s = service.get_session(session_repo, uid, sid)
    if s is None:
        return _not_found("session")
    return _session_view(
        request,
        uid,
        s,
        deck_repo=deck_repo,
        session_repo=session_repo,
        review_repo=review_repo,
        q_repo=q_repo,
    )


@router.get("/sessions/{sid}/next", include_in_schema=False)
def session_next(
    request: Request,
    sid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
) -> JSONResponse:
    """Read-only: the current view of the session. Advancing past a
    verdict is POST /advance, which returns this same shape."""
    uid = user["tailscale_login"]
    s = service.get_session(session_repo, uid, sid)
    if s is None:
        return _not_found("session")
    return _session_view(
        request,
        uid,
        s,
        deck_repo=deck_repo,
        session_repo=session_repo,
        review_repo=review_repo,
        q_repo=q_repo,
    )


@router.post("/sessions/{sid}/advance", include_in_schema=False)
def session_advance(
    request: Request,
    sid: str,
    body: VersionBody,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
) -> JSONResponse:
    """Move to the next due card (or to completed) and return the new
    view, so "next card" is one round trip. Body: {version}."""
    uid = user["tailscale_login"]
    s = service.get_session(session_repo, uid, sid)
    if s is None:
        return _not_found("session")
    try:
        service.advance_session(session_repo, uid, sid, body.version)
    except StaleVersionError as e:
        return _stale(e)
    moved = service.get_session(session_repo, uid, sid)
    if moved is None:
        return _not_found("session")
    return _session_view(
        request,
        uid,
        moved,
        deck_repo=deck_repo,
        session_repo=session_repo,
        review_repo=review_repo,
        q_repo=q_repo,
    )


@router.post("/sessions/{sid}/submit", include_in_schema=False)
async def session_submit(
    request: Request,
    sid: str,
    body: SubmitBody,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
) -> JSONResponse:
    uid = user["tailscale_login"]
    s = service.get_session(session_repo, uid, sid)
    if s is None:
        return _not_found("session")
    q = q_repo.get(uid, body.question_id)
    if q is None:
        return _not_found("question")
    return await _submit(
        request,
        uid,
        body,
        q=q,
        deck_name=deck_repo.find_name(uid, s.deck_id) or "",
        s=s,
        session_repo=session_repo,
        review_repo=review_repo,
        deck_repo=deck_repo,
    )


@router.post("/sessions/{sid}/draft", include_in_schema=False)
def session_draft(
    sid: str,
    body: DraftBody,
    user: dict = Depends(current_user),
    session_repo: SessionRepo = Depends(_session_repo),
) -> JSONResponse:
    """Autosave the in-progress answer. Version-checked like every
    other session mutation."""
    uid = user["tailscale_login"]
    try:
        new_v = service.update_draft(session_repo, uid, sid, body.draft, body.version)
    except StaleVersionError as e:
        return _stale(e)
    except ValueError:
        return _not_found("session")
    return JSONResponse({"version": new_v})


@router.post("/sessions/{sid}/abandon", include_in_schema=False)
def session_abandon(
    sid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
) -> JSONResponse:
    uid = user["tailscale_login"]
    s = service.get_session(session_repo, uid, sid)
    if s is None:
        return _not_found("session")
    service.abandon_session(session_repo, uid, sid)
    moved = service.get_session(session_repo, uid, sid)
    deck_name = deck_repo.find_name(uid, s.deck_id) or ""
    return JSONResponse(
        {
            "ended": {"reason": "abandoned"},
            "session": _session_payload(moved or s, deck_name),
        }
    )


@router.post("/sessions/{sid}/snooze", include_in_schema=False)
def session_snooze(
    sid: str,
    body: SnoozeBody,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
) -> JSONResponse:
    """Hide the session from the Continue strip until it wakes. The row
    stays active; `preset=wake` clears an existing snooze."""
    from prep.web.durations import DurationError, parse_until

    uid = user["tailscale_login"]
    s = service.get_session(session_repo, uid, sid)
    if s is None:
        return _not_found("session")
    preset = (body.preset or "").strip().lower()
    until: str | None = None
    if preset != "wake":
        try:
            until = parse_until(preset=preset or None, custom=body.custom, unit=body.unit)
        except DurationError as e:
            return _error(400, "invalid_duration", str(e))
    session_repo.snooze(uid, sid, until)
    deck_name = deck_repo.find_name(uid, s.deck_id) or ""
    return JSONResponse({"snoozed_until": until, "session": _session_payload(s, deck_name)})


# ---- deck-scoped study (no session) ------------------------------------


@router.get("/decks/{name}/next", include_in_schema=False)
def deck_next(
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
) -> JSONResponse:
    """One due card from the deck, no session state. A read never
    creates the deck, so an unknown or foreign name is 404."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        return _not_found("deck")
    if deck_repo.get_type(uid, deck_id) is DeckType.TRIVIA:
        return _error(400, "not_studiable", "trivia decks have no study queue")
    due = review_repo.due_questions(uid, deck_id, limit=1)
    if not due:
        return JSONResponse(_caught_up(review_repo, uid, deck_id, None))
    q = q_repo.get(uid, due[0]["id"])
    if q is None:
        return _not_found("question")
    return JSONResponse({"card": _card(q), "draft": q.skeleton or "", "session": None})


@router.post("/decks/{name}/submit", include_in_schema=False)
async def deck_submit(
    request: Request,
    name: str,
    body: SubmitBody,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    review_repo: ReviewRepo = Depends(_review_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
) -> JSONResponse:
    uid = user["tailscale_login"]
    if deck_repo.find_id(uid, name) is None:
        return _not_found("deck")
    q = q_repo.get(uid, body.question_id)
    if q is None:
        return _not_found("question")
    return await _submit(
        request,
        uid,
        body,
        q=q,
        deck_name=name,
        s=None,
        session_repo=session_repo,
        review_repo=review_repo,
        deck_repo=deck_repo,
    )


# ---- authoring ----------------------------------------------------------


@router.post("/cards", include_in_schema=False)
def author_card(
    body: AuthorBody,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
) -> JSONResponse:
    """Write a manually authored card. Stored as a `short` question due
    immediately, matching the offline author flow."""
    uid = user["tailscale_login"]
    prompt = body.prompt.strip()
    answer = body.answer.strip()
    if not prompt or not answer:
        return _error(422, "invalid_card", "both the front and the back are required")
    deck_id = body.deck_id
    if deck_id is None:
        deck_id = deck_repo.get_or_create(uid, _INBOX_DECK)
    deck_type = deck_repo.get_type(uid, deck_id)
    if deck_type is None:
        return _not_found("deck")
    if deck_type is DeckType.TRIVIA:
        return _error(400, "not_studiable", "trivia decks are not studied from the card loop")
    qid = decks_service.add_question(
        q_repo,
        uid,
        deck_id,
        NewQuestion(type=QuestionType.SHORT, prompt=prompt, answer=answer),
        deck_repo=deck_repo,
    )
    q = q_repo.get(uid, qid)
    if q is None:
        return _not_found("question")
    return JSONResponse({"card": _revealed(q)}, status_code=201)


# ---- grading polling ----------------------------------------------------


@router.get("/grading/{wid}", include_in_schema=False)
async def grading_poll(
    request: Request,
    wid: str,
    sid: str = "",
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    session_repo: SessionRepo = Depends(_session_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
) -> JSONResponse:
    """Poll an in-flight grade. Returns the same {pending} shape the
    submit did until the workflow lands, then the verdict outcome.
    With `sid`, reconciles the session row on the way through. The
    reconcile is idempotent, so repeat polls are safe."""
    parsed = parse_grading_wid(wid)
    if not parsed:
        return _error(400, "malformed_workflow_id", "workflow id is not a grading id")
    _, qid = parsed
    uid = user["tailscale_login"]
    # The wid embeds the question id, so ownership is checked here or
    # not at all: guessing a wid must not expose another user's grade.
    q = q_repo.get(uid, qid)
    if q is None:
        return _not_found("question")

    from prep import temporal_client

    progress = await temporal_client.get_grade_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    status = (progress or {}).get("status") or (desc or {}).get("status") or "unknown"

    # Same tracker hook the HTML fragment carries: status transitions
    # drive the masthead badge and its push notifications.
    from prep.workflows import service as workflows_service

    workflows_service.update_status(workflow_id=wid, new_status=status)

    if status not in _TERMINAL:
        # The worker writes progress.error while still running (the
        # busy-free-tier "add your own key" pointer arrives this way),
        # so it has to travel with the pending payload or the user
        # never sees why their grade is slow.
        pending = {
            "poll": _poll_url(request, wid, sid),
            "workflow_id": wid,
            "status": status,
        }
        note = (progress or {}).get("error") or ""
        if note:
            pending["error"] = note
        return JSONResponse({"pending": pending})

    # Terminal per the status query, so the result should already be
    # there. Bounded wait rather than a long-poll: a straggler shows up
    # on the next poll instead of hanging this request.
    try:
        result = await asyncio.wait_for(temporal_client.get_grade_result(wid), timeout=0.5)
    except asyncio.TimeoutError:
        result = None
    if result is None:
        error = (progress or {}).get("error") or ""
        # Release the session before answering. Left in 'grading' it
        # would hand every later read another pending screen for a
        # workflow that will never produce a verdict, and the client
        # would poll it forever.
        if sid:
            session_repo.grading_abandoned(uid, sid, wid)
        return JSONResponse(
            {
                "failed": {
                    "code": "grading_failed",
                    "message": error or "the grader returned nothing",
                }
            }
        )

    verdict = result["verdict"]
    raw = result["state"]
    state = {
        "step": raw["step"],
        "next_due": raw["next_due"],
        "interval_minutes": raw["interval_minutes"],
    }

    session = None
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
        s = service.get_session(session_repo, uid, sid)
        if s is not None:
            session = _session_payload(s, deck_repo.find_name(uid, s.deck_id) or "")

    return JSONResponse(
        _verdict_outcome(q, verdict, state, result["user_answer"], result["idk"], session)
    )
