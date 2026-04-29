"""HTTP routes for the decks bounded context.

Each handler is a thin translation layer:
  parse request → call service → render response.

No SQL, no temporal-client calls — those live behind the service /
repo / temporal-client modules respectively. Routes get an
APIRouter that app.py mounts at module load time.

Why a Router and not @app.get(): keeps each context's HTTP surface
in one place (this file is the contract for the decks UI), keeps
app.py thin, and makes it trivial to remount under a prefix later
if we ever expose deck routes under /api/v1/decks/* alongside the
HTML routes.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import FormData

from prep import db
from prep.auth import current_user
from prep.decks import service
from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.web import responses
from prep.web.templates import templates

router = APIRouter()


# ---- form-level validation ---------------------------------------------
#
# The deck-name regex is stricter than the entity's `min_length=1` —
# user-typed names go through this guard before they hit the entity, so
# we don't accept arbitrary unicode / spaces / uppercase via the form.
# The entity tolerates legacy data with looser shapes.

_DECK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")
_RESERVED_DECK_NAMES = frozenset(
    {
        "new",
        "create",
        "edit",
        "delete",
        "static",
        "dev",
        "preview",
        "notify",
        "session",
        "study",
        "deck",
        "decks",
        "manifest",
    }
)
_MAX_CONTEXT_PROMPT_CHARS = 8000


def _validate_deck_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not _DECK_NAME_RE.match(n):
        raise HTTPException(
            400,
            "Deck name must be 2-30 chars, lowercase, alphanumerics or hyphens, "
            "starting with a letter or digit.",
        )
    if n in _RESERVED_DECK_NAMES:
        raise HTTPException(400, f'"{n}" is reserved — pick another name.')
    return n


# ---- question-form parsing ---------------------------------------------
#
# Both the new-question and edit-question forms accept the same fields
# in the same shape. Centralizing the parse + validation keeps the two
# route handlers free of the duplication that built up in app.py.


def _parse_question_form(form: FormData) -> tuple[NewQuestion | None, dict, str | None]:
    """Extract a NewQuestion entity from a form submission.

    Returns (entity_or_None, raw_dict, error_or_None). The raw dict is
    what the template's `form` block re-renders on validation error,
    so users don't lose their typed input. The entity is None when
    validation fails (or pydantic itself rejects the values)."""
    qtype_raw = (form.get("type") or "").strip()
    prompt = (form.get("prompt") or "").strip()
    answer_raw = (form.get("answer") or "").strip()
    topic = (form.get("topic") or "").strip() or None
    skeleton = (form.get("skeleton") or "").strip() or None
    language = (form.get("language") or "").strip() or None
    rubric = (form.get("rubric") or "").strip() or None
    choices_raw = (form.get("choices") or "").strip()
    choices = [ln.strip() for ln in choices_raw.splitlines() if ln.strip()] or None

    raw = {
        "type": qtype_raw,
        "prompt": prompt,
        "answer": answer_raw,
        "topic": topic or "",
        "skeleton": skeleton or "",
        "language": language or "",
        "rubric": rubric or "",
        "choices": choices_raw,
    }

    err: str | None = None
    valid_types = sorted(t.value for t in QuestionType)
    if qtype_raw not in valid_types:
        err = f"Type must be one of: {', '.join(valid_types)}."
    elif not prompt:
        err = "Prompt is required."
    elif not answer_raw:
        err = "Answer is required."
    elif qtype_raw in ("mcq", "multi") and not choices:
        err = f"{qtype_raw.upper()} questions need at least one choice (one per line)."
    elif qtype_raw == "code" and not language:
        err = "Code questions need a language."

    if err:
        return None, raw, err

    # `multi` answers are stored as JSON arrays. Accept either a JSON
    # literal or a newline-separated list — be forgiving.
    answer: str = answer_raw
    if qtype_raw == "multi":
        try:
            parsed = json.loads(answer_raw)
            if isinstance(parsed, list):
                answer = json.dumps(parsed)
        except json.JSONDecodeError:
            answer = json.dumps([ln.strip() for ln in answer_raw.splitlines() if ln.strip()])

    return (
        NewQuestion(
            type=QuestionType(qtype_raw),
            prompt=prompt,
            answer=answer,
            topic=topic,
            choices=choices,
            rubric=rubric,
            skeleton=skeleton,
            language=language,
        ),
        raw,
        None,
    )


def _question_form_from_entity(q) -> dict:
    """Convert a Question entity into the dict shape the question_edit
    template's `form` block expects: list-typed fields rendered as
    newline-joined strings, multi-answer JSON unwrapped."""
    answer = q.answer or ""
    if q.type is QuestionType.MULTI:
        try:
            parsed = json.loads(answer) if answer else []
            if isinstance(parsed, list):
                answer = "\n".join(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "type": q.type.value,
        "topic": q.topic or "",
        "prompt": q.prompt,
        "choices": "\n".join(q.choices) if q.choices else "",
        "answer": answer,
        "rubric": q.rubric or "",
        "skeleton": q.skeleton or "",
        "language": q.language or "",
    }


# ---- workflow-id parsing ------------------------------------------------
#
# Workflow IDs are constructed by prep.temporal_client when starting a
# workflow. Their shapes are stable strings — the route layer parses
# them to recover the resource being operated on (deck or question)
# for ownership checks.


def _parse_transform_wid(wid: str) -> tuple[str, int] | None:
    """`transform-<scope>-<target_id>-<rand>`. Returns (scope, target_id)
    or None if malformed. scope ∈ {card, deck}."""
    if not wid.startswith("transform-"):
        return None
    parts = wid[len("transform-") :].split("-")
    if len(parts) < 3:
        return None
    scope = parts[0]
    if scope not in ("card", "deck"):
        return None
    try:
        target_id = int(parts[1])
    except ValueError:
        return None
    return scope, target_id


def _parse_plan_wid(wid: str) -> str | None:
    """`plan-<deck_name>-<rand>`. Returns deck_name or None.

    deck_name may itself contain hyphens, so we split on the *trailing*
    rand suffix (last hyphen) and treat what's left as the name."""
    if not wid.startswith("plan-"):
        return None
    rest = wid[len("plan-") :]
    if "-" not in rest:
        return None
    name, _, suffix = rest.rpartition("-")
    if not name or len(suffix) < 6:
        return None
    return name


def _deck_repo() -> DeckRepo:
    """Per-request DeckRepo. Cheap to instantiate (it's stateless —
    just delegates to module-level db functions). FastAPI's
    Depends() caches it per-request so the wiring is uniform across
    routes that need it."""
    return DeckRepo()


def _question_repo() -> QuestionRepo:
    return QuestionRepo()


# ---- Deck-level routes --------------------------------------------------


@router.get("/decks/new", response_class=HTMLResponse)
def deck_new_form(request: Request, user: dict = Depends(current_user)):
    """Render the new-deck form. The same form drives both the empty-deck
    path and the plan-first AI generation path; the user picks the
    submit button."""
    return templates.TemplateResponse(
        "deck_new.html",
        {
            "request": request,
            "user": user,
            "name_value": "",
            "context_value": "",
            "error": None,
        },
    )


@router.post("/decks/new", response_class=HTMLResponse)
async def deck_new_create(
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Create a deck. The submit button name picks the path:
      action=empty       → just create the deck row, redirect to /deck/<name>
      action=plan        → create the deck row, kick off PlanGenerateWorkflow
                           with the description, redirect to /plan/<wid>
    The 'plan' action requires both an agent AND a non-empty description.
    Re-renders the form with an inline error if either is missing.
    """
    from prep import agent as _agent_mod
    from prep import temporal_client

    uid = user["tailscale_login"]
    form = await request.form()
    name = (form.get("name") or "").strip()
    context_prompt = (form.get("context_prompt") or "").strip()
    action = (form.get("action") or "empty").strip()

    def rerender(error: str, status: int = 400):
        return templates.TemplateResponse(
            "deck_new.html",
            {
                "request": request,
                "user": user,
                "name_value": name,
                "context_value": context_prompt,
                "error": error,
            },
            status_code=status,
        )

    try:
        clean = _validate_deck_name(name)
    except HTTPException as e:
        return rerender(e.detail)

    if deck_repo.find_id(uid, clean) is not None:
        return rerender(f'You already have a deck named "{clean}".')

    if len(context_prompt) > _MAX_CONTEXT_PROMPT_CHARS:
        return rerender(
            f"Description is too long ({len(context_prompt)} chars; max "
            f"{_MAX_CONTEXT_PROMPT_CHARS})."
        )

    if action == "plan":
        if not _agent_mod.is_available:
            return rerender(
                "Plan & generate needs an AI agent. Set PREP_AGENT_URL or PREP_AGENT_BIN, "
                "or pick 'Create empty deck' instead."
            )
        if not context_prompt:
            return rerender("Plan & generate needs a description for claude to plan against.")

    deck_id = service.create_deck(deck_repo, uid, clean, context_prompt or None)

    if action == "plan":
        try:
            res = await service.start_plan_generation(
                temporal_client,
                user_id=uid,
                deck_id=deck_id,
                deck_name=clean,
                prompt=context_prompt,
            )
        except Exception as e:
            # Deck row was created but the workflow couldn't start —
            # surface the error and the user can retry from the deck page.
            raise HTTPException(500, f"deck created but failed to start plan workflow: {e}")
        return responses.redirect(request, f"/plan/{res.workflow_id}")

    return responses.redirect(request, f"/deck/{clean}")


@router.get("/deck/{name}", response_class=HTMLResponse)
def deck_view(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Show every question in a deck (suspended + active), with
    inline SRS state + the deck's free-form context_prompt. Uses
    `get_or_create_deck` so first-time navigation lazily materializes
    a deck row even if the user hasn't added any questions yet."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.get_or_create(uid, name)
    cards = q_repo.list_in_deck(uid, deck_id)
    now_ts = db.now()
    return templates.TemplateResponse(
        "deck.html",
        {
            "request": request,
            "user": user,
            "deck_name": name,
            "questions": cards,
            "due_count": sum(
                1 for c in cards if not c.suspended and c.next_due and c.next_due <= now_ts
            ),
        },
    )


@router.post("/deck/{name}/delete")
def deck_delete(
    request: Request,
    name: str,
    confirm: str = Form(...),
    user: dict = Depends(current_user),
    repo: DeckRepo = Depends(_deck_repo),
):
    """Delete a deck and (via FK CASCADE) all its questions/cards/
    reviews/sessions. Requires the user to type the deck name into a
    `confirm` field on the dialog form — guards against accidental
    clicks. Returns a redirect back to the index."""
    uid = user["tailscale_login"]
    if confirm.strip() != name:
        raise HTTPException(400, "deck name didn't match — delete not performed")
    if repo.find_id(uid, name) is None:
        raise HTTPException(404, "deck not found")
    service.delete_deck(repo, uid, name)
    return responses.redirect(request, "/")


# ---- Question-level routes ----------------------------------------------


@router.get("/deck/{name}/question/new", response_class=HTMLResponse)
def question_new_form(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Manual question entry form. The primary card-creation path when
    no AI agent is configured; an additional path otherwise."""
    uid = user["tailscale_login"]
    if deck_repo.find_id(uid, name) is None:
        raise HTTPException(404, "deck not found")
    return templates.TemplateResponse(
        "question_new.html",
        {"request": request, "deck_name": name, "form": {}, "error": None},
    )


@router.post("/deck/{name}/question/new", response_class=HTMLResponse)
async def question_new_submit(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")

    form = await request.form()
    new, raw, err = _parse_question_form(form)
    if err is not None or new is None:
        return templates.TemplateResponse(
            "question_new.html",
            {"request": request, "deck_name": name, "form": raw, "error": err},
            status_code=400,
        )
    service.add_question(q_repo, uid, deck_id, new)
    return responses.redirect(request, f"/deck/{name}")


@router.get("/question/{qid}/edit", response_class=HTMLResponse)
def question_edit_form(
    request: Request,
    qid: int,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Manual edit form. Always available regardless of agent —
    counterpart to the AI Improve button."""
    uid = user["tailscale_login"]
    q = q_repo.get(uid, qid)
    if q is None:
        raise HTTPException(404, "question not found")
    deck_name = deck_repo.find_name(uid, q.deck_id)
    if deck_name is None:
        raise HTTPException(404, "deck not found")
    return templates.TemplateResponse(
        "question_edit.html",
        {
            "request": request,
            "deck_name": deck_name,
            "q": q,
            "form": _question_form_from_entity(q),
            "error": None,
        },
    )


@router.post("/question/{qid}/edit", response_class=HTMLResponse)
async def question_edit_submit(
    request: Request,
    qid: int,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    uid = user["tailscale_login"]
    q = q_repo.get(uid, qid)
    if q is None:
        raise HTTPException(404, "question not found")
    deck_name = deck_repo.find_name(uid, q.deck_id)
    if deck_name is None:
        raise HTTPException(404, "deck not found")

    form = await request.form()
    new, raw, err = _parse_question_form(form)
    if err is not None or new is None:
        return templates.TemplateResponse(
            "question_edit.html",
            {
                "request": request,
                "deck_name": deck_name,
                "q": q,
                "form": raw,
                "error": err,
            },
            status_code=400,
        )
    service.update_question(q_repo, uid, qid, new)
    return responses.redirect(request, f"/deck/{deck_name}")


@router.post("/question/{qid}/suspend")
def question_suspend(
    request: Request,
    qid: int,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Toggle a question into the suspended state — it stops appearing
    in study sessions but stays in the deck. Redirects back to the
    deck page so the toggle in the UI can update inline."""
    uid = user["tailscale_login"]
    q = q_repo.get(uid, qid)
    if q is None:
        raise HTTPException(404, "question not found")
    service.suspend_question(q_repo, uid, qid)
    deck_name = deck_repo.find_name(uid, q.deck_id)
    return responses.redirect(request, f"/deck/{deck_name}")


@router.post("/question/{qid}/unsuspend")
def question_unsuspend(
    request: Request,
    qid: int,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Inverse of suspend — re-include the question in study sessions."""
    uid = user["tailscale_login"]
    q = q_repo.get(uid, qid)
    if q is None:
        raise HTTPException(404, "question not found")
    service.unsuspend_question(q_repo, uid, qid)
    deck_name = deck_repo.find_name(uid, q.deck_id)
    return responses.redirect(request, f"/deck/{deck_name}")


@router.post("/question/{qid}/improve")
async def question_improve(
    request: Request,
    qid: int,
    prompt: str = Form(...),
    user: dict = Depends(current_user),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Per-card free-text rewrite via the AI agent. Auto-applies on
    completion (card-scope transforms don't have an apply/reject
    review step — that's reserved for deck-wide changes)."""
    uid = user["tailscale_login"]
    if q_repo.get(uid, qid) is None:
        raise HTTPException(404, "question not found")
    if not prompt.strip():
        raise HTTPException(400, "empty prompt")
    # Late import: keeps the routes module's startup-time graph clean
    # of temporal-client setup, which has its own retry/dial logic.
    from prep import temporal_client

    try:
        result = await service.start_card_transform(
            temporal_client,
            user_id=uid,
            qid=qid,
            prompt=prompt.strip(),
        )
    except Exception as e:
        raise HTTPException(500, f"failed to start transform: {e}")
    return responses.redirect(request, f"/transform/{result.workflow_id}")


# ---- transform workflow signals + status -------------------------------


def _require_owns_transform(
    user: dict,
    wid: str,
    deck_repo: DeckRepo,
    q_repo: QuestionRepo,
) -> tuple[str, int]:
    """Parse + verify ownership of a transform workflow id. Routes use
    this as the gate before signaling. Returns (scope, target_id)."""
    parsed = _parse_transform_wid(wid)
    if not parsed:
        raise HTTPException(400, "malformed workflow id")
    scope, target_id = parsed
    uid = user["tailscale_login"]
    if scope == "card":
        if q_repo.get(uid, target_id) is None:
            raise HTTPException(404, "transform not found")
    else:
        if deck_repo.find_name(uid, target_id) is None:
            raise HTTPException(404, "transform not found")
    return scope, target_id


@router.get("/transform/{wid}/status")
async def transform_status(
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    _require_owns_transform(user, wid, deck_repo, q_repo)
    from prep import temporal_client

    progress = await temporal_client.get_transform_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    return JSONResponse({"progress": progress, "desc": desc})


@router.post("/transform/{wid}/apply")
async def transform_apply(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    _require_owns_transform(user, wid, deck_repo, q_repo)
    from prep import temporal_client

    try:
        await service.apply_transform(temporal_client, wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return responses.redirect(request, f"/transform/{wid}")


@router.post("/transform/{wid}/reject")
async def transform_reject(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    _require_owns_transform(user, wid, deck_repo, q_repo)
    from prep import temporal_client

    try:
        await service.reject_transform(temporal_client, wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return responses.redirect(request, f"/transform/{wid}")


# ---- plan-first generation: signals + status ---------------------------


def _require_owns_plan(user: dict, wid: str, deck_repo: DeckRepo) -> tuple[str, int]:
    """Parse + verify ownership of a plan workflow id. Returns
    (deck_name, deck_id)."""
    name = _parse_plan_wid(wid)
    if not name:
        raise HTTPException(400, "malformed workflow id")
    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "plan not found")
    return name, deck_id


@router.get("/plan/{wid}/status")
async def plan_status(
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """JSON status for the plan polling page. Returns the live progress
    while the workflow is alive, or {"status": "gone"} once the query
    handler is no longer registered."""
    _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    progress = await service.get_plan_progress(temporal_client, wid)
    if progress is None:
        return JSONResponse({"status": "gone"})
    return JSONResponse(progress)


@router.post("/plan/{wid}/feedback")
async def plan_feedback(
    request: Request,
    wid: str,
    feedback: str = Form(...),
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    _require_owns_plan(user, wid, deck_repo)
    if not feedback.strip():
        raise HTTPException(400, "empty feedback")
    from prep import temporal_client

    await service.submit_plan_feedback(temporal_client, wid, feedback.strip())
    return responses.redirect(request, f"/plan/{wid}")


@router.post("/plan/{wid}/accept")
async def plan_accept(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    await service.accept_plan(temporal_client, wid)
    return responses.redirect(request, f"/plan/{wid}")


@router.post("/plan/{wid}/reject")
async def plan_reject(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    name, _ = _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    await service.reject_plan(temporal_client, wid)
    # On reject, return to the deck view rather than the plan page —
    # the plan workflow is being torn down, no use polling it.
    return responses.redirect(request, f"/deck/{name}")
