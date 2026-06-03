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
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
    or None if malformed. scope ∈ {card, deck, reorganize}.
    For reorganize, target_id is the literal 0 (no single deck)."""
    if not wid.startswith("transform-"):
        return None
    parts = wid[len("transform-") :].split("-")
    if len(parts) < 3:
        return None
    scope = parts[0]
    if scope not in ("card", "deck", "reorganize"):
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
        if not _agent_mod.is_available_for(uid):
            return rerender(
                "Plan & generate needs an AI agent. Configure one on the "
                "agent settings page (/settings/agent), or pick "
                "'Create empty deck' to add cards yourself."
            )
        if not context_prompt:
            return rerender("Plan & generate needs a description for the AI to plan against.")

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
        return rerender(
            "Topic is required — it describes what the deck is about, "
            "and the AI uses it later if you configure one."
        )

    try:
        interval = int(raw_interval)
    except ValueError:
        return rerender("Notification interval must be an integer.")
    if interval < 1 or interval > 720:
        return rerender("Notification interval must be 1–720 minutes.")

    deck_id = deck_repo.create_trivia(uid, clean, topic=topic, interval_minutes=interval)

    # AI is optional. When no agent is configured, create the deck and
    # send the user to the deck page — they can add cards manually
    # there (the existing /trivia/decks/<id>/manual-add path). When an
    # agent IS configured, kick off the batch-generation workflow and
    # redirect to the polling page.
    if not _agent_mod.is_available_for(uid):
        return responses.redirect(request, f"/deck/{clean}")

    # Kick off the workflow that does the actual AI call + per-card
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
    # Register with the active-workflows tracker — masthead badge
    # picks it up on the next 5s poll. Late import to avoid eager
    # module loading; the workflows context is tracker-only.
    from prep.workflows import service as _workflows_service
    from prep.workflows.entities import WorkflowType as _WT

    _workflows_service.register(
        user_login=uid,
        workflow_id=res.workflow_id,
        workflow_type=_WT.TRIVIA_GEN,
        deck_id=deck_id,
        deck_name=clean,
        url_path=f"/trivia/gen/{res.workflow_id}",
        initial_status="computing",
    )
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
    # Both srs and trivia decks expose the per-deck notification toggle
    # — srs honors it via the digest count filter; trivia honors it via
    # the per-deck scheduler skip.
    deck_meta = deck_repo.get_meta(uid, deck_id)

    # Trivia-only stats for the mastery-bar header. Cheap one-query
    # group-by; only computed when the deck is actually trivia.
    trivia_stats: dict[str, int] | None = None
    if deck_type is not None and deck_type.value == "trivia":
        from prep.trivia.repo import TriviaQueueRepo

        trivia_stats = TriviaQueueRepo().deck_stats(deck_id)

    # SRS-only: surface this deck's retention override (if any) and the
    # user-level default so the deck page can render the override picker
    # with the right preselected option. NULL deck_retention means "use
    # the default"; the template just needs both values + the preset
    # list to render the radio set.
    deck_retention: float | None = None
    user_retention: float | None = None
    retention_presets = None
    if deck_type is not None and deck_type.value == "srs":
        deck_retention = deck_repo.get_desired_retention(uid, deck_id)
        from prep.auth.repo import UserRepo
        from prep.auth.routes import _RETENTION_PRESETS

        user_retention = UserRepo().get_desired_retention(uid)
        retention_presets = _RETENTION_PRESETS

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
            # Trivia decks no longer create `cards` rows (fix #335);
            # next_due is None for trivia questions, and the truthiness
            # check skips them. The deck route also gates trivia from
            # ever reaching this counting path, but the guard is cheap
            # and makes the semantics local.
            "due_count": sum(
                1
                for c in cards
                if not c.suspended and c.next_due is not None and c.next_due <= now_ts
            ),
            "deck_retention": deck_retention,
            "user_retention": user_retention,
            "retention_presets": retention_presets,
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
    if request.headers.get("hx-request") == "true":
        # htmx caller (data-popover form): nothing to swap; the popover
        # closes via hx-on::after-request on the form. 204 = "no body".
        return Response(status_code=204)
    return responses.redirect(request, f"/deck/{name}")


@router.post("/deck/{name}/rename")
def deck_rename(
    request: Request,
    name: str,
    new_name: str = Form(...),
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Rename a deck. Validates the new name with the same regex used
    on creation and rejects collisions with another existing deck of
    the same user. Redirects to the deck under its new URL."""
    uid = user["tailscale_login"]
    if deck_repo.find_id(uid, name) is None:
        raise HTTPException(404, "deck not found")
    cleaned = _validate_deck_name(new_name)
    if cleaned == name:
        return responses.redirect(request, f"/deck/{name}")
    if not deck_repo.rename(uid, name, cleaned):
        raise HTTPException(400, f'a deck named "{cleaned}" already exists')
    return responses.redirect(request, f"/deck/{cleaned}")


@router.get("/deck/{name}/edit-with-claude", response_class=HTMLResponse)
def deck_edit_with_claude(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Dedicated page for the deck-wide AI edit prompt. Replaces the
    inline toggle panel that used to live on the deck page; the prompt
    + apply flow now has its own focused view."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    deck_type = deck_repo.get_type(uid, deck_id)
    return templates.TemplateResponse(
        "deck_edit_ai.html",
        {
            "request": request,
            "user": user,
            "deck_name": name,
            "deck_type": deck_type.value if deck_type else "srs",
            "error": None,
        },
    )


@router.get("/deck/{name}/split", response_class=HTMLResponse)
def deck_split_form(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Render the manual split-deck form: card list with checkboxes,
    new-deck-name field, optional new-topic-prompt for trivia."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    cards = q_repo.list_in_deck(uid, deck_id)
    deck_type = deck_repo.get_type(uid, deck_id)
    source_topic = ""
    if deck_type is not None and deck_type.value == "trivia":
        source_topic = deck_repo.get_context_prompt(uid, name) or ""
    return templates.TemplateResponse(
        "deck_split.html",
        {
            "request": request,
            "user": user,
            "deck_name": name,
            "deck_type": deck_type.value if deck_type else "srs",
            "cards": cards,
            "source_topic": source_topic,
            "error": None,
            "form": {"new_name": "", "new_topic": "", "selected_ids": set()},
        },
    )


@router.post("/deck/{name}/split", response_class=HTMLResponse)
async def deck_split_submit(
    request: Request,
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Apply the split: create the new deck, reassign selected
    questions. Re-renders the form on validation error so the user
    keeps their input."""
    uid = user["tailscale_login"]
    source_deck_id = deck_repo.find_id(uid, name)
    if source_deck_id is None:
        raise HTTPException(404, "deck not found")

    form = await request.form()
    new_name = (form.get("new_name") or "").strip()
    new_topic = (form.get("new_topic") or "").strip()
    selected_raw = form.getlist("question_ids")
    selected_ids: list[int] = []
    for sid in selected_raw:
        try:
            selected_ids.append(int(sid))
        except (TypeError, ValueError):
            continue

    def rerender(error: str, status: int = 400):
        deck_type = deck_repo.get_type(uid, source_deck_id)
        return templates.TemplateResponse(
            "deck_split.html",
            {
                "request": request,
                "user": user,
                "deck_name": name,
                "deck_type": deck_type.value if deck_type else "srs",
                "cards": q_repo.list_in_deck(uid, source_deck_id),
                "source_topic": (deck_repo.get_context_prompt(uid, name) or ""),
                "error": error,
                "form": {
                    "new_name": new_name,
                    "new_topic": new_topic,
                    "selected_ids": set(selected_ids),
                },
            },
            status_code=status,
        )

    try:
        service.split_deck(
            deck_repo=deck_repo,
            question_repo=q_repo,
            user_id=uid,
            source_deck_id=source_deck_id,
            new_deck_name=new_name,
            question_ids=selected_ids,
            new_topic_prompt=new_topic or None,
        )
    except ValueError as e:
        return rerender(str(e))

    return responses.redirect(request, f"/deck/{new_name}")


@router.post("/deck/{name}/pin")
def deck_toggle_pin(
    request: Request,
    name: str,
    pinned: str = Form(...),
    user: dict = Depends(current_user),
    repo: DeckRepo = Depends(_deck_repo),
):
    """Toggle deck pin. Pinned decks float to the top of the index,
    most-recently-pinned first. 404 if the deck doesn't belong to
    the user.

    htmx-aware: when the request carries `HX-Request: true`, return
    just the pin-form fragment so the client can swap it in place
    (no full-page reload, no nav race with a follow-up tap).
    Otherwise, fall back to the POST→303 redirect for the no-JS path."""
    uid = user["tailscale_login"]
    deck_id = repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    is_pinned = pinned == "on"
    repo.set_pinned(uid, deck_id, is_pinned)
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/pin_form.html",
            {"deck_name": name, "pinned": is_pinned},
        )
    return responses.redirect_back(request, f"/deck/{name}")


@router.post("/deck/{name}/retention")
def deck_set_retention(
    request: Request,
    name: str,
    retention: str = Form(...),
    user: dict = Depends(current_user),
    repo: DeckRepo = Depends(_deck_repo),
):
    """Per-deck FSRS retention override (SRS decks only).

    `retention` is either a float in [MIN, MAX] (override the user
    default for this deck) or the literal string "default" (clear
    the override → fall back to the user's setting at /settings/srs).

    Resolution at review time happens in prep/study/repo.py:record —
    deck override wins; user-level fallback; FSRS algorithm default."""
    from prep.domain.srs import MAX_DESIRED_RETENTION, MIN_DESIRED_RETENTION

    uid = user["tailscale_login"]
    deck_id = repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    if (deck_type := repo.get_type(uid, deck_id)) is None or deck_type.value != "srs":
        raise HTTPException(400, "retention applies only to SRS decks")

    raw = retention.strip().lower()
    if raw in ("default", "none", ""):
        new_val: float | None = None
    else:
        try:
            new_val = float(raw)
        except ValueError as e:
            raise HTTPException(400, f"retention must be a number or 'default', got {raw!r}") from e
        if not (MIN_DESIRED_RETENTION <= new_val <= MAX_DESIRED_RETENTION):
            raise HTTPException(
                400,
                f"retention must be between {MIN_DESIRED_RETENTION:.0%} and "
                f"{MAX_DESIRED_RETENTION:.0%}",
            )
    if not repo.set_desired_retention(uid, deck_id, new_val):
        raise HTTPException(404, "deck not found")
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
    per-deck scheduler skip. Pausing also abandons any in-progress
    sessions on the deck — see service.set_notifications_enabled.
    404 if the deck doesn't belong to the user."""
    uid = user["tailscale_login"]
    deck_id = repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    if not service.set_notifications_enabled(repo, uid, deck_id, enabled == "on"):
        raise HTTPException(404, "deck not found")
    return responses.redirect_back(request, f"/deck/{name}")


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
    service.add_question(q_repo, uid, deck_id, new, deck_repo=deck_repo)
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
    if request.headers.get("hx-request") == "true":
        # htmx caller toggles the .is-suspended class via
        # hx-on::after-request — nothing to swap, return 204.
        return Response(status_code=204)
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
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204)
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
            deck_repo=deck_repo,
            user_id=uid,
            deck_id=deck_id,
            prompt=prompt.strip(),
            deck_name=name,
        )
    except Exception as e:
        raise HTTPException(500, f"failed to start transform: {e}")
    return responses.redirect(request, f"/transform/{result.workflow_id}")


@router.get("/reorganize", response_class=HTMLResponse)
def reorganize_form(
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Form for the cross-deck reorganize flow. Free-text prompt,
    plus a collapsible preview of the user's current decks so they
    can see what claude has to work with."""
    uid = user["tailscale_login"]
    decks = sorted(deck_repo.list_summaries(uid), key=lambda d: d.name)
    deck_views = []
    for d in decks:
        topic = ""
        if d.deck_type.value == "trivia":
            topic = deck_repo.get_context_prompt(uid, d.name) or ""
        deck_views.append(
            {
                "name": d.name,
                "deck_type": d.deck_type.value,
                "total": d.total,
                "topic": topic,
            }
        )
    return templates.TemplateResponse(
        "reorganize.html",
        {
            "request": request,
            "user": user,
            "decks": deck_views,
            "form": {"prompt": ""},
            "error": None,
        },
    )


@router.post("/reorganize")
async def reorganize_submit(
    request: Request,
    prompt: str = Form(...),
    user: dict = Depends(current_user),
):
    """Kick off a cross-deck reorganize workflow."""
    uid = user["tailscale_login"]
    cleaned = (prompt or "").strip()
    if not cleaned:
        raise HTTPException(400, "empty prompt")
    from prep import temporal_client

    try:
        result = await temporal_client.start_transform(
            user_id=uid, scope="reorganize", target_id=0, prompt=cleaned
        )
    except Exception as e:
        raise HTTPException(500, f"failed to start reorganize: {e}")
    # Register the cross-deck workflow so the badge shows it as
    # "reorganize" (no single deck name to attach).
    from prep.workflows import service as _workflows_service
    from prep.workflows.entities import WorkflowType as _WT

    _workflows_service.register(
        user_login=uid,
        workflow_id=result.workflow_id,
        workflow_type=_WT.TRANSFORM,
        deck_id=None,
        deck_name=None,
        url_path=f"/transform/{result.workflow_id}",
        initial_status="computing",
    )
    return responses.redirect(request, f"/transform/{result.workflow_id}")


@router.post("/question/{qid}/improve")
async def question_improve(
    request: Request,
    qid: int,
    prompt: str = Form(...),
    user: dict = Depends(current_user),
    q_repo: QuestionRepo = Depends(_question_repo),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Per-card free-text rewrite via the AI agent. Auto-applies on
    completion (card-scope transforms don't have an apply/reject
    review step — that's reserved for deck-wide changes)."""
    uid = user["tailscale_login"]
    q_entity = q_repo.get(uid, qid)
    if q_entity is None:
        raise HTTPException(404, "question not found")
    if not prompt.strip():
        raise HTTPException(400, "empty prompt")
    # Resolve the deck name eagerly so the workflow tracker has a
    # human-friendly label for the badge popover (card-scope transforms
    # auto-apply with no review screen, so the badge is the only UI
    # surface that shows the running operation).
    deck_repo_for_name = DeckRepo()
    deck_name = deck_repo_for_name.find_name(uid, q_entity.deck_id)
    # Late import: keeps the routes module's startup-time graph clean
    # of temporal-client setup, which has its own retry/dial logic.
    from prep import temporal_client

    try:
        result = await service.start_card_transform(
            temporal_client,
            deck_repo=deck_repo,
            question_repo=q_repo,
            user_id=uid,
            qid=qid,
            prompt=prompt.strip(),
            deck_name=deck_name,
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
    this as the gate before signaling. Returns (scope, target_id).
    Reorganize scope has no single target — only ownership-by-user
    matters; the workflow itself enforces user_id scoping at apply time."""
    parsed = _parse_transform_wid(wid)
    if not parsed:
        raise HTTPException(400, "malformed workflow id")
    scope, target_id = parsed
    uid = user["tailscale_login"]
    if scope == "card":
        if q_repo.get(uid, target_id) is None:
            raise HTTPException(404, "transform not found")
    elif scope == "deck":
        if deck_repo.find_name(uid, target_id) is None:
            raise HTTPException(404, "transform not found")
    # reorganize: no per-target check; the workflow's UserID matches
    # the route's user via current_user, which is the trust boundary.
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
        # Workflow's query handler is gone (workflow closed). Map the
        # describe-status into the same shape the partial expects.
        # We deliberately do NOT call get_transform_result(wid) here:
        # that's a blocking long-poll on handle.result() and was the
        # source of the 1-5s button hang this refactor removes. The
        # progress.result field is only used to render the
        # `Applied — modified N…` summary, which we can live without
        # in the rare edge where the user lands on this URL after the
        # workflow closed AND the in-memory query handler aged out.
        if status == "COMPLETED":
            progress = {"status": "done"}
        else:
            progress = {"status": "gone"}

    uid = user["tailscale_login"]
    ctx = service.build_transform_view_ctx(
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=uid,
        scope=scope,
        target_id=target_id,
        progress=progress,
    )

    return templates.TemplateResponse(
        "transform.html",
        {
            "request": request,
            "user": user,
            "wid": wid,
            "scope": scope,
            "target_id": target_id,
            "deck_name": ctx.deck_name,
            "progress": progress or {},
            "desc": desc or {},
            "status": status,
            "modification_diffs": ctx.modification_diffs,
            "deletion_decks": ctx.deletion_decks,
            "move_source_decks": ctx.move_source_decks,
            "deck_id_to_name": ctx.deck_id_to_name,
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


@router.get("/transform/{wid}/fragment", response_class=HTMLResponse)
async def transform_fragment(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """htmx polling endpoint — returns just the #transform-progress
    fragment that swaps in place via hx-swap=outerHTML. The partial
    embeds its own hx-trigger ONLY when the workflow is still mid-flight,
    so when the server returns a terminal-state fragment the client
    stops polling automatically (no JS state machine, no separate
    /status call). Shares the same auth/ctx builder as transform_view."""
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
        # Workflow's query handler is gone (workflow closed). Map the
        # describe-status into the same shape the partial expects.
        # We deliberately do NOT call get_transform_result(wid) here:
        # that's a blocking long-poll on handle.result() and was the
        # source of the 1-5s button hang this refactor removes. The
        # progress.result field is only used to render the
        # `Applied — modified N…` summary, which we can live without
        # in the rare edge where the user lands on this URL after the
        # workflow closed AND the in-memory query handler aged out.
        if status == "COMPLETED":
            progress = {"status": "done"}
        else:
            progress = {"status": "gone"}

    # Workflow-tracker hook: diff status against the last poll and fire
    # the push-notification side effect on awaiting-action / terminal
    # transitions. update_status is idempotent + swallows errors, so a
    # tracking failure never breaks the fragment response.
    from prep.workflows import service as _workflows_service

    _workflows_service.update_status(
        workflow_id=wid, new_status=(progress or {}).get("status") or status
    )

    uid = user["tailscale_login"]
    ctx = service.build_transform_view_ctx(
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=uid,
        scope=scope,
        target_id=target_id,
        progress=progress,
    )

    return templates.TemplateResponse(
        "partials/transform_progress.html",
        {
            "request": request,
            "user": user,
            "wid": wid,
            "scope": scope,
            "target_id": target_id,
            "deck_name": ctx.deck_name,
            "progress": progress or {},
            "modification_diffs": ctx.modification_diffs,
            "deletion_decks": ctx.deletion_decks,
            "move_source_decks": ctx.move_source_decks,
            "deck_id_to_name": ctx.deck_id_to_name,
        },
    )


async def _render_transform_fragment(
    request: Request,
    wid: str,
    scope: str,
    target_id: int,
    user: dict,
    deck_repo: DeckRepo,
    q_repo: QuestionRepo,
):
    """Shared body for apply/reject routes (and transform_fragment).

    Queries the workflow for fresh progress and renders the
    transform_progress partial. Critically, this NEVER blocks on
    handle.result() — if the query handler is gone (workflow already
    closed), we report status="gone" and let the partial render a
    benign terminal state. The htmx fragment polling loop continues
    via /transform/{wid}/fragment if the user wants to refresh."""
    from prep import temporal_client

    progress = await temporal_client.get_transform_progress(wid)
    if progress is None:
        # Workflow closed before the query landed (e.g. reject path
        # exited fast). Pull the workflow's terminal status via
        # describe — that's a metadata fetch, not a long-poll on
        # the result, so it's cheap and non-blocking. If the
        # workflow COMPLETED we render `done`; if anything else
        # (FAILED/CANCELED/TERMINATED) we fall through to `gone`
        # which the template treats as a benign cancelled state.
        # We deliberately do NOT call get_transform_result() here:
        # that's a blocking long-poll and is exactly the
        # anti-pattern this refactor is removing.
        try:
            desc = await temporal_client.describe_workflow(wid)
        except Exception:
            desc = {}
        if desc.get("status") == "COMPLETED":
            progress = {"status": "done"}
        else:
            progress = {"status": "gone"}

    uid = user["tailscale_login"]
    ctx = service.build_transform_view_ctx(
        deck_repo=deck_repo,
        question_repo=q_repo,
        user_id=uid,
        scope=scope,
        target_id=target_id,
        progress=progress,
    )
    return templates.TemplateResponse(
        "partials/transform_progress.html",
        {
            "request": request,
            "user": user,
            "wid": wid,
            "scope": scope,
            "target_id": target_id,
            "deck_name": ctx.deck_name,
            "progress": progress or {},
            "modification_diffs": ctx.modification_diffs,
            "deletion_decks": ctx.deletion_decks,
            "move_source_decks": ctx.move_source_decks,
            "deck_id_to_name": ctx.deck_id_to_name,
        },
    )


@router.post("/transform/{wid}/apply")
async def transform_apply(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Send the apply signal and immediately render the partial.

    Was: signal → 303 redirect → transform_view → blocked on
    get_transform_result if the workflow had closed (1-5s hang).
    Now: signal → query → render partial. The htmx-targeted swap
    replaces #transform-progress in place; the partial's own
    hx-trigger continues the polling loop if status is non-terminal.
    No blocking calls."""
    scope, target_id = _require_owns_transform(user, wid, deck_repo, q_repo)
    from prep import temporal_client

    try:
        await service.apply_transform(temporal_client, wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return await _render_transform_fragment(request, wid, scope, target_id, user, deck_repo, q_repo)


@router.post("/transform/{wid}/reject")
async def transform_reject(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Send the reject signal and immediately render the partial.
    See transform_apply for the rationale."""
    scope, target_id = _require_owns_transform(user, wid, deck_repo, q_repo)
    from prep import temporal_client

    try:
        await service.reject_transform(temporal_client, wid)
    except Exception as e:
        raise HTTPException(500, f"signal failed: {e}")
    return await _render_transform_fragment(request, wid, scope, target_id, user, deck_repo, q_repo)


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


@router.get("/plan/{wid}/fragment", response_class=HTMLResponse)
async def plan_fragment(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """htmx polling endpoint — returns just the #plan-progress fragment.
    The partial embeds hx-trigger only for non-terminal states; once the
    workflow hits awaiting_feedback / done / rejected / failed / gone,
    the trigger is omitted and htmx stops polling. Server-driven loop
    lifecycle — no JS state machine."""
    deck_name, _ = _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    progress = await service.get_plan_progress(temporal_client, wid)
    if progress is None:
        progress = {"status": "gone"}

    # Workflow-tracker hook — see transform_fragment for the rationale.
    from prep.workflows import service as _workflows_service

    _workflows_service.update_status(
        workflow_id=wid, new_status=progress.get("status") or "unknown"
    )

    return templates.TemplateResponse(
        "partials/plan_progress.html",
        {
            "request": request,
            "user": user,
            "wid": wid,
            "deck_name": deck_name,
            "progress": progress,
        },
    )


async def _render_plan_fragment(
    request: Request,
    wid: str,
    deck_name: str,
    user: dict,
):
    """Shared body for plan accept/reject/feedback routes (and
    plan_fragment). Queries the workflow for fresh progress and
    renders the plan_progress partial. Never blocks on
    handle.result()."""
    from prep import temporal_client

    progress = await service.get_plan_progress(temporal_client, wid)
    if progress is None:
        progress = {"status": "gone"}
    return templates.TemplateResponse(
        "partials/plan_progress.html",
        {
            "request": request,
            "user": user,
            "wid": wid,
            "deck_name": deck_name,
            "progress": progress,
        },
    )


@router.post("/plan/{wid}/feedback")
async def plan_feedback(
    request: Request,
    wid: str,
    feedback: str = Form(...),
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Send feedback signal and render the partial fragment.
    Was: signal → 303 redirect → full page re-render. Now: signal →
    query → partial. htmx swaps #plan-progress in place."""
    deck_name, _ = _require_owns_plan(user, wid, deck_repo)
    if not feedback.strip():
        raise HTTPException(400, "empty feedback")
    from prep import temporal_client

    await service.submit_plan_feedback(temporal_client, wid, feedback.strip())
    return await _render_plan_fragment(request, wid, deck_name, user)


@router.post("/plan/{wid}/accept")
async def plan_accept(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Send accept signal and render the partial fragment. Status
    will be `accepting` post-signal (transient state added in
    plan.go)."""
    deck_name, _ = _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    await service.accept_plan(temporal_client, wid)
    return await _render_plan_fragment(request, wid, deck_name, user)


@router.post("/plan/{wid}/reject")
async def plan_reject(
    request: Request,
    wid: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Send reject signal and render the partial fragment. Status
    will be `rejecting` post-signal (transient state added in
    plan.go)."""
    deck_name, _ = _require_owns_plan(user, wid, deck_repo)
    from prep import temporal_client

    await service.reject_plan(temporal_client, wid)
    return await _render_plan_fragment(request, wid, deck_name, user)


# ---- CSV import / export ----------------------------------------------


@router.get("/deck/{name}/export.csv", include_in_schema=False)
def deck_export_csv(
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Download every question in `name` as a CSV file. Wire format
    lives in `prep.decks.io.CSV_COLUMNS` — same shape the public API
    + future MCP server emit. 404 if the deck doesn't exist for this
    user (cross-user IDOR via guessed name returns same shape)."""
    from fastapi.responses import Response

    from prep.decks.io import deck_to_csv

    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    body = deck_to_csv(uid, deck_id)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.csv"',
            # CSV files should not be cached — a re-download right after
            # adding a card needs to see the new row.
            "Cache-Control": "no-store",
        },
    )


@router.get("/deck/{name}/export", response_class=HTMLResponse)
def deck_export_hub(
    name: str,
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Dedicated export landing page. Hosts the three format options
    (prepdeck, CSV, Anki) with full explanations + one download button
    per format. The deck overflow menu links here instead of carrying
    three flat actions that don't fit the popover.

    The page's JS triggers the Web Share API (with file) for iOS PWAs,
    falling back to a regular download elsewhere — see
    static/js/modules/deck-export.js."""
    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    deck_type = deck_repo.get_type(uid, deck_id)
    return templates.TemplateResponse(
        "deck_export.html",
        {
            "request": request,
            "user": user,
            "deck_name": name,
            "deck_type": deck_type.value if deck_type else "srs",
        },
    )


@router.get("/deck/{name}/export.prepdeck", include_in_schema=False)
def deck_export_prepdeck(
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Download every question + FSRS state + reviews log + trivia
    queue state in `name` as a `.prepdeck` archive (zip).

    Full-fidelity backup format — what gets exported here is exactly
    what `.prepdeck` import can restore (modulo regenerated question
    ids). Wire shape lives in `prep.decks.archive`."""
    from prep.decks.archive import deck_to_prepdeck

    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    body = deck_to_prepdeck(uid, deck_id)
    return Response(
        content=body,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.prepdeck"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/deck/{name}/export.apkg", include_in_schema=False)
def deck_export_apkg(
    name: str,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
):
    """Download every question in `name` as an Anki .apkg file.
    Drops review state (cards arrive fresh) and flattens all four
    question types to Basic notes — see anki_export.py for the
    structure choices."""
    from prep.decks.anki_export import deck_to_apkg

    uid = user["tailscale_login"]
    deck_id = deck_repo.find_id(uid, name)
    if deck_id is None:
        raise HTTPException(404, "deck not found")
    body = deck_to_apkg(uid, deck_id, name)
    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.apkg"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/decks/import-csv", response_class=HTMLResponse)
def decks_import_csv_form(request: Request, user: dict = Depends(current_user)):
    """Render the CSV upload page. Posts to the same path."""
    return templates.TemplateResponse(
        "deck_import_csv.html",
        {"request": request, "user": user, "outcome": None, "error": None},
    )


@router.post("/decks/import-csv", response_class=HTMLResponse)
async def decks_import_csv_submit(
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Multipart-form upload: a CSV file + an optional deck name.
    Creates a new deck or appends to an existing one. Returns the
    same template with an `outcome` block (inserted / skipped /
    errors)."""
    from prep.decks.io import csv_to_deck

    uid = user["tailscale_login"]
    form = await request.form()
    name = (form.get("name") or "").strip()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return templates.TemplateResponse(
            "deck_import_csv.html",
            {
                "request": request,
                "user": user,
                "outcome": None,
                "error": "Pick a CSV file to upload.",
            },
            status_code=400,
        )

    try:
        clean = _validate_deck_name(name)
    except HTTPException as e:
        return templates.TemplateResponse(
            "deck_import_csv.html",
            {"request": request, "user": user, "outcome": None, "error": e.detail},
            status_code=400,
        )

    raw = await upload.read()
    try:
        csv_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        csv_text = raw.decode("utf-8", errors="replace")

    outcome = csv_to_deck(
        uid,
        clean,
        csv_text,
        deck_repo=deck_repo,
        question_repo=q_repo,
    )
    return templates.TemplateResponse(
        "deck_import_csv.html",
        {"request": request, "user": user, "outcome": outcome, "error": None},
    )


@router.get("/decks/import-prepdeck", response_class=HTMLResponse)
def decks_import_prepdeck_form(request: Request, user: dict = Depends(current_user)):
    """Render the .prepdeck upload page. POSTs to the same path."""
    return templates.TemplateResponse(
        "deck_import_prepdeck.html",
        {"request": request, "user": user, "outcome": None, "error": None},
    )


@router.post("/decks/import-prepdeck", response_class=HTMLResponse)
async def decks_import_prepdeck_submit(
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Multipart-form upload: a `.prepdeck` zip + a deck name. Restores
    a deck with full FSRS state, review log, and (for trivia decks)
    queue state. Refuses if the target deck name already exists — the
    semantics are 'restore deck,' not 'append cards.'"""
    from prep.decks.archive import prepdeck_to_deck

    uid = user["tailscale_login"]
    form = await request.form()
    name = (form.get("name") or "").strip()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return templates.TemplateResponse(
            "deck_import_prepdeck.html",
            {
                "request": request,
                "user": user,
                "outcome": None,
                "error": "Pick a .prepdeck file to upload.",
            },
            status_code=400,
        )

    try:
        clean = _validate_deck_name(name)
    except HTTPException as e:
        return templates.TemplateResponse(
            "deck_import_prepdeck.html",
            {"request": request, "user": user, "outcome": None, "error": e.detail},
            status_code=400,
        )

    raw = await upload.read()
    outcome = prepdeck_to_deck(
        uid,
        clean,
        raw,
        deck_repo=deck_repo,
        question_repo=q_repo,
    )
    return templates.TemplateResponse(
        "deck_import_prepdeck.html",
        {"request": request, "user": user, "outcome": outcome, "error": None},
    )


@router.get("/decks/import-anki", response_class=HTMLResponse)
def decks_import_anki_form(request: Request, user: dict = Depends(current_user)):
    """Render the .apkg upload page. POSTs to the same path."""
    return templates.TemplateResponse(
        "deck_import_anki.html",
        {"request": request, "user": user, "outcome": None, "error": None},
    )


@router.post("/decks/import-anki", response_class=HTMLResponse)
async def decks_import_anki_submit(
    request: Request,
    user: dict = Depends(current_user),
    deck_repo: DeckRepo = Depends(_deck_repo),
    q_repo: QuestionRepo = Depends(_question_repo),
):
    """Multipart-form upload: an .apkg file + a deck name. Parses the
    zipped sqlite collection, drops HTML and media, inserts each note
    as a short-type card. Returns an outcome block showing
    inserted / dedup / cloze-skipped / errors."""
    from prep.decks.anki import apkg_to_deck

    uid = user["tailscale_login"]
    form = await request.form()
    name = (form.get("name") or "").strip()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return templates.TemplateResponse(
            "deck_import_anki.html",
            {
                "request": request,
                "user": user,
                "outcome": None,
                "error": "Pick an .apkg file to upload.",
            },
            status_code=400,
        )

    try:
        clean = _validate_deck_name(name)
    except HTTPException as e:
        return templates.TemplateResponse(
            "deck_import_anki.html",
            {"request": request, "user": user, "outcome": None, "error": e.detail},
            status_code=400,
        )

    raw = await upload.read()
    try:
        outcome = apkg_to_deck(uid, clean, raw, deck_repo=deck_repo, question_repo=q_repo)
    except ValueError as e:
        return templates.TemplateResponse(
            "deck_import_anki.html",
            {"request": request, "user": user, "outcome": None, "error": str(e)},
            status_code=400,
        )

    return templates.TemplateResponse(
        "deck_import_anki.html",
        {"request": request, "user": user, "outcome": outcome, "error": None},
    )
