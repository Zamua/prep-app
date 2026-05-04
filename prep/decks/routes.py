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
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import FormData

from prep.auth import current_user
from prep.decks import service
from prep.decks.entities import NewQuestion, QuestionType
from prep.decks.repo import DeckRepo, QuestionRepo
from prep.infrastructure.db import now as db_now
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
    answer_regex = (form.get("answer_regex") or "").strip() or None
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
        "answer_regex": answer_regex or "",
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
            answer_regex=answer_regex,
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
        "answer_regex": q.answer_regex or "",
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
def deck_new_chooser(request: Request, user: dict = Depends(current_user)):
    """Step 1 of deck creation — pick which kind. Each option leads
    to its own type-specific form (`/decks/new/srs` or
    `/decks/new/trivia`). Keeps each form focused on the fields that
    flow actually needs, instead of one mega-form with a multi-action
    submit row."""
    return templates.TemplateResponse(
        "deck_new_chooser.html",
        {"request": request, "user": user},
    )


@router.get("/decks/new/srs", response_class=HTMLResponse)
def deck_new_srs_form(request: Request, user: dict = Depends(current_user)):
    """Step 2 (SRS): name + description + plan-vs-empty submit."""
    return templates.TemplateResponse(
        "deck_new_srs.html",
        {
            "request": request,
            "user": user,
            "name_value": "",
            "context_value": "",
            "error": None,
        },
    )


@router.get("/decks/new/trivia", response_class=HTMLResponse)
def deck_new_trivia_form(request: Request, user: dict = Depends(current_user)):
    """Step 2 (trivia): name + topic + interval. Single submit
    button — there's no empty/plan branch here."""
    return templates.TemplateResponse(
        "deck_new_trivia.html",
        {
            "request": request,
            "user": user,
            "name_value": "",
            "topic_value": "",
            "interval_value": 30,
            "error": None,
        },
    )


@router.post("/decks/new/srs", response_class=HTMLResponse)
async def deck_new_srs_create(
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Create an SRS deck. `action` chooses the path:
    action=empty       → just create the deck row, redirect to /deck/<name>
    action=plan        → create the deck row, kick off PlanGenerateWorkflow,
                         redirect to /plan/<wid>
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
            "deck_new_srs.html",
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
            raise HTTPException(500, f"deck created but failed to start plan workflow: {e}")
        return responses.redirect(request, f"/plan/{res.workflow_id}")

    return responses.redirect(request, f"/deck/{clean}")


@router.post("/decks/new/trivia", response_class=HTMLResponse)
async def deck_new_trivia_create(
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Create a trivia deck + kick off the initial-batch generation as
    a background Temporal workflow. The deck row lands sync; the
    response is an immediate redirect to /trivia/gen/<wid> which polls
    a status endpoint until the workflow's `done` (then jumps to the
    deck page). Same pattern as /plan/<wid> for SRS plan-generate."""
    from prep import agent as _agent_mod
    from prep import temporal_client

    uid = user["tailscale_login"]
    form = await request.form()
    name = (form.get("name") or "").strip()
    topic = (form.get("topic") or "").strip()
    raw_interval = (form.get("notification_interval_minutes") or "30").strip()

    def rerender(error: str, status: int = 400):
        # Best-effort coerce interval back to an int for the form's
        # value attribute; fall back to the default if the user typed
        # garbage that we're complaining about.
        try:
            interval_value = int(raw_interval)
        except ValueError:
            interval_value = 30
        return templates.TemplateResponse(
            "deck_new_trivia.html",
            {
                "request": request,
                "user": user,
                "name_value": name,
                "topic_value": topic,
                "interval_value": interval_value,
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

    if len(topic) > _MAX_CONTEXT_PROMPT_CHARS:
        return rerender(f"Topic is too long ({len(topic)} chars; max {_MAX_CONTEXT_PROMPT_CHARS}).")
    if not topic:
        return rerender("Topic is required — claude reads it to generate questions.")

    if not _agent_mod.is_available:
        return rerender(
            "Trivia decks need an AI agent for the initial batch. "
            "Set PREP_AGENT_URL or PREP_AGENT_BIN."
        )

    try:
        interval = int(raw_interval)
    except ValueError:
        return rerender("Notification interval must be an integer.")
    if interval < 1 or interval > 720:
        return rerender("Notification interval must be 1–720 minutes.")

    deck_id = deck_repo.create_trivia(uid, clean, topic=topic, interval_minutes=interval)
    # Kick off the workflow that does the actual claude call + per-card
    # inserts. Returns immediately — UI redirects to a polling page.
    try:
        res = await temporal_client.start_trivia_generate(
            user_id=uid,
            deck_id=deck_id,
            deck_name=clean,
            topic=topic,
        )
    except Exception as e:
        # Deck row was created but the workflow couldn't start. Surface
        # the error so the user can either retry or hit the manual
        # /trivia/decks/<id>/generate route.
        raise HTTPException(500, f"deck created but failed to start trivia workflow: {e}") from e
    return responses.redirect(request, f"/trivia/gen/{res.workflow_id}")


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
    now_ts = db_now()
    deck_type = deck_repo.get_type(uid, deck_id)
    # Cheap row read for the per-deck notification toggle state.
    # Both srs and trivia decks expose the toggle now — srs honors
    # it via the digest count filter; trivia honors it via the
    # per-deck scheduler skip.
    from prep.infrastructure.db import cursor

    deck_meta: dict[str, Any] = {"deck_id": deck_id, "notifications_enabled": True}
    with cursor() as c:
        row = c.execute(
            "SELECT notifications_enabled, notification_interval_minutes, "
            "trivia_session_size, context_prompt "
            "FROM decks WHERE id=? AND user_id=?",
            (deck_id, uid),
        ).fetchone()
    if row:
        deck_meta = {
            "deck_id": deck_id,
            "notifications_enabled": bool(row["notifications_enabled"]),
            "interval_minutes": row["notification_interval_minutes"],
            "session_size": int(row["trivia_session_size"] or 3),
            "context_prompt": row["context_prompt"] or "",
        }

    # Trivia-only stats for the mastery-bar header. Cheap one-query
    # group-by; only computed when the deck is actually trivia.
    trivia_stats: dict[str, int] | None = None
    if deck_type is not None and deck_type.value == "trivia":
        from prep.trivia.repo import TriviaQueueRepo

        trivia_stats = TriviaQueueRepo().deck_stats(deck_id)

    return templates.TemplateResponse(
        "deck.html",
        {
            "request": request,
            "user": user,
            "deck_name": name,
            "questions": cards,
            "deck_type": deck_type.value if deck_type else "srs",
            "trivia": deck_meta,  # template still uses `trivia` for the trivia path
            "deck_meta": deck_meta,
            "trivia_stats": trivia_stats,
            # next_due is None for trivia-deck questions (no `cards`
            # row); the truthiness check handles that path implicitly.
            "due_count": sum(
                1
                for c in cards
                if not c.suspended and c.next_due is not None and c.next_due <= now_ts
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


_MAX_TOPIC_PROMPT_CHARS = 4000


@router.post("/deck/{name}/topic")
def deck_update_topic(
    request: Request,
    name: str,
    context_prompt: str = Form(...),
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Update the trivia deck's topic prompt. Subsequent batch
    generations (scheduler refill OR explicit Generate) will use the
    updated text. Existing cards are not touched. Trivia-only —
    SRS decks have a different edit surface."""
    uid = user["tailscale_login"]
    if deck_repo.find_id(uid, name) is None:
        raise HTTPException(404, "deck not found")
    deck_type = deck_repo.get_type(uid, deck_repo.find_id(uid, name))
    if deck_type is None or deck_type.value != "trivia":
        raise HTTPException(400, "topic prompt only applies to trivia decks")
    cleaned = (context_prompt or "").strip()
    if not cleaned:
        raise HTTPException(400, "topic prompt cannot be empty")
    if len(cleaned) > _MAX_TOPIC_PROMPT_CHARS:
        raise HTTPException(
            400, f"topic prompt too long ({len(cleaned)} chars; max {_MAX_TOPIC_PROMPT_CHARS})"
        )
    deck_repo.update_context_prompt(uid, name, cleaned)
    return responses.redirect(request, f"/deck/{name}")


@router.post("/deck/{name}/notifications")
def deck_toggle_notifications(
    request: Request,
    name: str,
    enabled: str = Form(...),
    user: dict = Depends(current_user),
    repo: DeckRepo = Depends(_deck_repo),
):
    """Per-deck notification toggle. SRS decks honor it via the
    digest count (paused decks subtract from the user's due_total
    + drop out of the digest body). Trivia decks honor it via the
    per-deck scheduler skip. 404 if the deck doesn't belong to the
    user."""
    uid = user["tailscale_login"]
    deck_id = repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    repo.set_notifications_enabled(uid, deck_id, enabled == "on")
    return responses.redirect(request, f"/deck/{name}")


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


@router.post("/deck/{name}/transform")
async def deck_transform(
    request: Request,
    name: str,
    prompt: str = Form(...),
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Deck-level free-text transform — returns a Plan and waits on
    apply/reject signal before writing. The user is redirected to the
    transform polling page where they can review the proposed changes."""
    uid = user["tailscale_login"]
    if not prompt.strip():
        raise HTTPException(400, "empty prompt")
    deck_id = deck_repo.get_or_create(uid, name)  # materialize if first time
    from prep import temporal_client

    try:
        result = await service.start_deck_transform(
            temporal_client,
            user_id=uid,
            deck_id=deck_id,
            prompt=prompt.strip(),
        )
    except Exception as e:
        raise HTTPException(500, f"failed to start transform: {e}")
    return responses.redirect(request, f"/transform/{result.workflow_id}")


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


@router.get("/transform/{wid}", response_class=HTMLResponse)
async def transform_view(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Transform polling page — shows live progress while the workflow
    is alive, falls back to the awaited result on terminal completion
    (since the queryable handler is gone by then)."""
    scope, target_id = _require_owns_transform(user, wid, deck_repo, q_repo)
    from prep import temporal_client

    progress = await temporal_client.get_transform_progress(wid)
    desc = await temporal_client.describe_workflow(wid)
    status = (progress or {}).get("status") or (desc or {}).get("status") or "unknown"

    terminal = status in {
        "done",
        "failed",
        "rejected",
        "COMPLETED",
        "FAILED",
        "TERMINATED",
        "CANCELED",
    }
    if terminal and progress is None:
        progress = {"status": "done", "result": await temporal_client.get_transform_result(wid)}

    # Recover the deck name for the back link. Card-scope walks
    # question → deck.
    uid = user["tailscale_login"]
    deck_name = ""
    if scope == "deck":
        deck_name = deck_repo.find_name(uid, target_id) or ""
    else:
        q = q_repo.get(uid, target_id)
        if q is not None:
            deck_name = deck_repo.find_name(uid, q.deck_id) or ""

    return templates.TemplateResponse(
        "transform.html",
        {
            "request": request,
            "user": user,
            "wid": wid,
            "scope": scope,
            "target_id": target_id,
            "deck_name": deck_name,
            "progress": progress or {},
            "desc": desc or {},
            "status": status,
        },
    )


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


@router.get("/plan/{wid}", response_class=HTMLResponse)
async def plan_view(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Plan polling page — renders the live planner UI while the
    workflow is alive. If the query handler is gone (workflow ended),
    fall back to the deck page since there's nothing to poll."""
    deck_name, _ = _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    progress = await service.get_plan_progress(temporal_client, wid)
    if progress is None:
        return responses.redirect(request, f"/deck/{deck_name}")
    return templates.TemplateResponse(
        "plan.html",
        {
            "request": request,
            "wid": wid,
            "deck_name": deck_name,
            "progress": progress,
        },
    )


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
