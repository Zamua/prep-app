"""HTTP routes for the agent bounded context.

Four endpoints:
- GET  /settings/agent             → settings template (user-facing)
- POST /settings/agent/connect     → forward token to agent-server
- POST /settings/agent/disconnect  → wipe token via agent-server
- POST /api/agent/run              → SDK-backed one-shot prompt
                                     (machine-to-machine; used by the
                                      Temporal worker once its env var
                                      flips from agent-server to prep)

The /api/agent/run endpoint speaks the same wire format the agent-
server's /run does ({prompt, session_id?, resume_id?} → {stdout}),
so the worker swap is a one-line env-var change with no Go-side
diff. Auth: requires a matching PREP_INTERNAL_TOKEN header — the
worker shares the same docker network + container, but we still
gate the endpoint so it can't burn credits if accidentally exposed.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from prep import agent as _agent_mod
from prep.agent.port import AgentBudgetExhausted, AgentUnavailable
from prep.auth import current_user
from prep.web.templates import templates

logger = logging.getLogger(__name__)
router = APIRouter()


# ---- machine-to-machine /api/agent/run -------------------------------


class _RunRequest(BaseModel):
    """Wire format matching worker-go/agent.RunInput so the worker can
    point at prep with no client-side change. session_id / resume_id
    are accepted-but-ignored — the SDK port doesn't currently expose
    multi-turn sessions for our one-shot callers.

    `user_id` is plumbed end-to-end so the selector can route to the
    user's BYOK key when present. Optional for backwards compat (an
    older worker without the field falls through to the subscription
    OAuth adapter), but any new caller should send it.
    """

    prompt: str
    session_id: str | None = None
    resume_id: str | None = None
    user_id: str | None = None
    # Optional escape hatches; callers normally omit and inherit the
    # adapter's defaults (Sonnet 4.6 + medium reasoning effort).
    model: str | None = None
    reasoning: str | None = None


def _require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    """Reject /api/agent/* calls that don't carry the shared secret.
    Configured via PREP_INTERNAL_TOKEN env var (set in deploy/*.env).
    If the env var is unset we refuse all calls — fail-closed."""
    expected = (os.environ.get("PREP_INTERNAL_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(503, "PREP_INTERNAL_TOKEN not configured")
    if not x_internal_token or x_internal_token != expected:
        raise HTTPException(401, "invalid X-Internal-Token")


@router.post("/api/agent/run")
async def api_agent_run(
    body: _RunRequest,
    _gate: None = Depends(_require_internal_token),
):
    """Execute a single prompt via the user's configured adapter (BYOK
    first, subscription OAuth fallback), log usage, return {stdout}
    (matching the legacy agent-server response shape so the Go
    worker doesn't notice it's hitting a different host)."""
    adapter = _agent_mod.get_agent(body.user_id)
    try:
        result = await adapter.run(body.prompt, model=body.model, reasoning=body.reasoning)
    except AgentBudgetExhausted as e:
        logger.warning("agent budget exhausted: %s", e)
        # 429 maps cleanly to "you've been throttled" — workflow code
        # can distinguish from 502 to surface the budget-specific UI.
        return JSONResponse({"error": str(e), "kind": "budget_exhausted"}, status_code=429)
    except AgentUnavailable as e:
        logger.warning("agent adapter unavailable: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)

    return {"stdout": result.text}


# ---- user-facing /settings/agent --------------------------------------

# Anthropic-issued `claude setup-token` values look like
# `sk-ant-oat01-…`. Reject anything else upstream so the UI surfaces
# a clear error instead of a downstream "auth failed" later.
_TOKEN_PREFIX = "sk-ant-oat01-"


def _refresh_agent_status() -> dict:
    """Re-probe the agent and update the cached availability flag the
    template context_processor surfaces. Called after a connect or
    disconnect so the UI sees the new state on the next render."""
    s = _agent_mod.status()
    _agent_mod.set_available(bool(s.get("logged_in")))
    return s


def _byok_sections_for(user_id: str):
    """Surface every supported BYOK provider as a list of dicts the
    settings template can render. Each entry carries:
      - `info`: static metadata (label, prefixes, console URL)
      - `metadata`: the user's stored credential (None if not set)
      - `is_active`: True if this provider is the user's explicit
        choice OR it's the implicit pick (first configured provider in
        precedence order when no explicit choice is set)

    Side effect: if the user's `active_byok_provider` points at a
    provider whose key has been removed, clear the column here so the
    next render lands on a clean state. Don't surface this as an
    error; users delete keys all the time and the implicit fallback
    is the right behavior.

    A single bad PREP_KEY_ENCRYPTION_SECRET shouldn't blank the entire
    settings page — we fall back to `metadata=None` per provider and
    keep rendering."""
    from prep.auth.repo import UserRepo
    from prep.byok.entities import PROVIDERS
    from prep.byok.repo import BYOKRepo

    repo = None
    try:
        repo = BYOKRepo()
    except Exception:  # noqa: BLE001 — bad master key, etc.
        logger.exception("byok repo init failed for %s", user_id)

    user_repo = UserRepo()
    chosen = user_repo.get_active_byok_provider(user_id)

    raw: list[dict] = []
    for provider, info in PROVIDERS.items():
        metadata = None
        if repo is not None:
            try:
                metadata = repo.metadata(user_id=user_id, provider=provider)
            except Exception:  # noqa: BLE001
                logger.exception("byok metadata lookup failed for %s / %s", user_id, provider.value)
        raw.append(
            {
                "provider": provider.value,
                "info": info,
                "metadata": metadata,
                "is_active": False,
            }
        )

    # Resolve active: explicit choice wins if its key is still saved;
    # otherwise the first configured provider in display order is the
    # implicit active one. Display order matches PROVIDERS dict order
    # (Anthropic, OpenAI, OpenRouter).
    active_idx: int | None = None
    if chosen:
        for i, s in enumerate(raw):
            if s["provider"] == chosen and s["metadata"]:
                active_idx = i
                break
        if active_idx is None:
            # Stale choice — the user deleted the key. Clear the
            # column so we don't keep trying to honor a ghost.
            user_repo.set_active_byok_provider(user_id, None)
    if active_idx is None:
        for i, s in enumerate(raw):
            if s["metadata"]:
                active_idx = i
                break

    if active_idx is not None:
        raw[active_idx]["is_active"] = True

    return raw


def _render_settings(
    request: Request,
    user: dict,
    *,
    status: dict | None = None,
    error: str | None = None,
    flash: str | None = None,
    byok_error: str | None = None,
    byok_flash: str | None = None,
    status_code: int = 200,
):
    """One render-helper for all settings routes — keeps the template
    context shape consistent so a new field added here surfaces
    everywhere automatically."""
    s = status if status is not None else _agent_mod.status()
    return templates.TemplateResponse(
        "settings_agent.html",
        {
            "request": request,
            "status": s,
            "error": error,
            "flash": flash,
            "byok_sections": _byok_sections_for(user["tailscale_login"]),
            "byok_error": byok_error,
            "byok_flash": byok_flash,
        },
        status_code=status_code,
    )


@router.get("/settings/agent", response_class=HTMLResponse)
def settings_agent_view(request: Request, user: dict = Depends(current_user)):
    # Fold a cache refresh into the page render — whatever the live
    # status says is what the agent_available context_processor will
    # serve next, so AI-gated UI snaps to truth on the next nav.
    s = _refresh_agent_status()
    return _render_settings(request, user, status=s)


@router.post("/settings/agent/connect", response_class=HTMLResponse)
async def settings_agent_connect(request: Request, user: dict = Depends(current_user)):
    """Persist a `claude setup-token` value to prep-data + activate it
    in-process. Post-SDK migration: no HTTP round-trip to a separate
    container — token storage is fully prep-side.

    HARD-GATED to non-clerk deploys. A clerk-mode deploy is multi-user;
    a single deploy-wide token would fund every signup's AI usage from
    the operator's Anthropic credit pool (the 2026-06-02 incident on
    prepcards.app). Per-user subscription tokens are tracked separately
    as task #326."""
    if (os.environ.get("PREP_AUTH_MODE") or "tailscale").strip().lower() == "clerk":
        # Use byok_error (not error) — `error` only renders inside the
        # subscription <details> panel, which is hidden on clerk mode.
        # byok_error is rendered above the BYOK provider sections so the
        # user actually sees the refusal.
        return _render_settings(
            request,
            user,
            byok_error=(
                "Deploy-wide subscription tokens are disabled on multi-user "
                "deploys. Add a personal API key on this page instead "
                "(Anthropic, OpenAI, OpenRouter, or your own Claude "
                "subscription token via the new BYOK section)."
            ),
            status_code=403,
        )

    from prep.agent import token_store

    form = await request.form()
    token = (form.get("token") or "").strip()
    if not token:
        return _render_settings(request, user, error="Token is required.", status_code=400)
    if not token.startswith(_TOKEN_PREFIX):
        return _render_settings(
            request,
            user,
            error=(
                f"Token must start with {_TOKEN_PREFIX!r}. "
                "Run `claude setup-token` on a machine you control and paste the output here."
            ),
            status_code=400,
        )

    try:
        token_store.write_token(token)
    except OSError as e:
        logger.exception("token write failed")
        return _render_settings(
            request,
            user,
            error=f"Couldn't write token to prep-data: {e}",
            status_code=500,
        )
    # Stamp into the live process env so the SDK adapter picks it up
    # without a container restart.
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token

    s = _refresh_agent_status()
    return _render_settings(
        request,
        user,
        status=s,
        flash="Connected. AI features should be available now.",
    )


@router.post("/settings/agent/disconnect", response_class=HTMLResponse)
def settings_agent_disconnect(request: Request, user: dict = Depends(current_user)):
    """Delete the persisted token + clear the process env. Idempotent
    — calling on an already-disconnected instance is a no-op."""
    from prep.agent import token_store

    token_store.delete_token()
    token_store.clear_env()
    s = _refresh_agent_status()
    return _render_settings(
        request,
        user,
        status=s,
        flash="Disconnected. AI features are now hidden; manual flows still work.",
    )


# ---- BYOK provider-agnostic routes ------------------------------------


def _parse_provider(slug: str):
    """Map a URL slug back to the Provider enum. Slug = enum value
    (e.g. `anthropic-api`). Raises 404 on anything unknown so an
    attacker can't probe which providers we support."""
    from prep.byok.entities import PROVIDERS, Provider

    try:
        p = Provider(slug)
    except ValueError as e:
        raise HTTPException(404, "unknown provider") from e
    if p not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    return p


@router.post("/settings/agent/byok/{provider}/connect", response_class=HTMLResponse)
async def settings_byok_connect(
    provider: str, request: Request, user: dict = Depends(current_user)
):
    """Store the user's API key for `provider` (encrypted). Key shape
    is validated against the provider's accepted prefixes; the
    encrypted row replaces whatever was there before."""
    from prep.byok.crypto import MasterKeyError
    from prep.byok.entities import PROVIDERS
    from prep.byok.repo import BYOKRepo

    p = _parse_provider(provider)
    info = PROVIDERS[p]

    form = await request.form()
    secret = (form.get("api_key") or "").strip()
    if not secret:
        return _render_settings(request, user, byok_error="API key is required.", status_code=400)
    if not any(secret.startswith(pref) for pref in info.key_prefixes):
        expected = info.key_prefixes[0]
        return _render_settings(
            request,
            user,
            byok_error=(
                f"That doesn't look like a {info.label} key — expected one "
                f"starting with {expected!r}. Generate one at "
                f"{info.console_url} and paste the output here."
            ),
            status_code=400,
        )

    try:
        BYOKRepo().store(user_id=user["tailscale_login"], provider=p, secret=secret)
    except MasterKeyError as e:
        # Master key not configured on this deploy — BYOK feature is
        # disabled regardless of what the user pastes. Surface it
        # plainly so they don't think their key is the problem.
        logger.error("byok store failed: master key not configured: %s", e)
        return _render_settings(
            request,
            user,
            byok_error=(
                "BYOK isn't available on this deploy — the operator hasn't "
                "configured PREP_KEY_ENCRYPTION_SECRET. Ask whoever runs this "
                "instance to enable it."
            ),
            status_code=503,
        )

    return _render_settings(
        request,
        user,
        byok_flash=f"Your {info.label} key is saved. AI features now use your account.",
    )


@router.post("/settings/agent/byok/{provider}/disconnect", response_class=HTMLResponse)
def settings_byok_disconnect(provider: str, request: Request, user: dict = Depends(current_user)):
    """Delete the user's BYOK row for `provider`. Idempotent: missing
    key → still 200. Selector falls back to the next provider in the
    precedence order, or the subscription path, or Noop after this.

    If the user had explicitly marked this provider active, clear the
    `active_byok_provider` column so the next render's implicit
    fallback isn't fighting a ghost choice."""
    from prep.auth.repo import UserRepo
    from prep.byok.repo import BYOKRepo

    p = _parse_provider(provider)
    uid = user["tailscale_login"]
    try:
        BYOKRepo().delete(user_id=uid, provider=p)
    except Exception:  # noqa: BLE001
        logger.exception("byok delete failed")
        # Still render the page — even if the delete blew up, the
        # user's intent ("get rid of it") matters most. Stale row
        # will be cleaned up on master rotation / user delete.

    user_repo = UserRepo()
    if user_repo.get_active_byok_provider(uid) == p.value:
        user_repo.set_active_byok_provider(uid, None)

    return _render_settings(
        request,
        user,
        byok_flash="API key removed.",
    )


@router.post("/settings/agent/byok/{provider}/use", response_class=HTMLResponse)
def settings_byok_use(provider: str, request: Request, user: dict = Depends(current_user)):
    """Mark `provider` as the user's active BYOK choice. Refuses if
    the user doesn't have a stored key for that provider (UX
    invariant: the 'Use this one' button only appears for configured
    rows, so this is mostly defense against a stale form post)."""
    from prep.auth.repo import UserRepo
    from prep.byok.entities import PROVIDERS
    from prep.byok.repo import BYOKRepo

    p = _parse_provider(provider)
    uid = user["tailscale_login"]

    if BYOKRepo().metadata(user_id=uid, provider=p) is None:
        return _render_settings(
            request,
            user,
            byok_error=(f"Add a {PROVIDERS[p].label} key before making it active."),
            status_code=400,
        )

    UserRepo().set_active_byok_provider(uid, p.value)
    return _render_settings(
        request,
        user,
        byok_flash=f"{PROVIDERS[p].label} is now your active provider.",
    )
