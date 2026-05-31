"""HTTP routes for the auth bounded context.

- Editor input mode preference (vanilla / vim / emacs) — a per-user
  setting that picks which CodeMirror keybinding extension loads.
- `/sign-in` + `/sign-out` redirects that hand off to whichever
  IdentityProvider is active (Clerk's hosted UI on the public
  deploy; 404 on Tailscale since auth is tied to the proxy).

The Clerk webhook receiver lives in `prep.auth.webhooks_clerk` and
is registered separately so the route stays callable without the
clerk-backend-api import on Tailscale-mode deploys.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from prep.auth import current_user
from prep.auth.providers import get_provider
from prep.auth.repo import UserRepo
from prep.web.templates import templates

router = APIRouter()


def _user_repo() -> UserRepo:
    return UserRepo()


# ---- sign-in / sign-out --------------------------------------------


@router.get("/sign-in")
def sign_in(request: Request):
    """Redirect to whatever sign-in URL the active provider exposes.
    Clerk: hosted accounts UI. Tailscale: 404 — auth happens at the
    proxy layer, there's no in-app sign-in flow."""
    urls = get_provider().urls()
    if not urls.sign_in:
        raise HTTPException(404, "this deploy has no in-app sign-in flow")
    return RedirectResponse(urls.sign_in, status_code=303)


@router.get("/sign-out")
def sign_out(request: Request):
    """Redirect to provider sign-out. Cookies / session revocation
    happen provider-side (Clerk handles it on their /sign-out)."""
    urls = get_provider().urls()
    if not urls.sign_out:
        raise HTTPException(404, "this deploy has no in-app sign-out flow")
    return RedirectResponse(urls.sign_out, status_code=303)


@router.get("/_debug/auth")
def debug_auth(request: Request):
    """Diagnostic endpoint — does NOT touch the DB, just reports what
    the active provider sees on this request. Useful when sign-in
    completes but the user is bounced back to the landing page —
    answer is almost always 'cookie not present' or 'JWT verify
    failed'.

    Returns JSON; safe to leave deployed (no secrets, only request
    metadata about the caller's own session)."""
    provider = get_provider()
    cookie_header = request.headers.get("cookie") or ""
    cookie_names = sorted(
        {c.split("=", 1)[0].strip() for c in cookie_header.split(";") if c.strip()}
    )
    has_session_cookie = "__session" in cookie_names
    has_auth_header = bool(request.headers.get("authorization"))

    out: dict = {
        "provider": provider.name,
        "cookie_names": cookie_names,
        "has_session_cookie": has_session_cookie,
        "has_authorization_header": has_auth_header,
        "host": request.headers.get("host"),
        "referer": request.headers.get("referer"),
    }

    if provider.name == "clerk":
        try:
            from clerk_backend_api import AuthenticateRequestOptions

            class _Adapter:
                def __init__(self, h):
                    self.headers = h

            headers = {}
            for k, v in request.headers.items():
                headers[k] = v
                headers[k.lower()] = v
            state = provider._sdk.authenticate_request(  # type: ignore[attr-defined]
                _Adapter(headers),
                AuthenticateRequestOptions(authorized_parties=provider._authorized_parties),  # type: ignore[attr-defined]
            )
            out["clerk"] = {
                "is_signed_in": getattr(state, "is_signed_in", None),
                "reason": str(getattr(state, "reason", None)),
                "message": getattr(state, "message", None),
                "payload_keys": sorted((state.payload or {}).keys())
                if getattr(state, "payload", None)
                else None,
                "sub": (state.payload or {}).get("sub")
                if getattr(state, "payload", None)
                else None,
            }
        except Exception as e:  # noqa: BLE001
            out["clerk"] = {"error": f"{type(e).__name__}: {e}"}

    return out


@router.get("/settings/editor", response_class=HTMLResponse)
def editor_settings(
    request: Request,
    user: dict = Depends(current_user),
    repo: UserRepo = Depends(_user_repo),
):
    return templates.TemplateResponse(
        "settings_editor.html",
        {
            "request": request,
            "user": user,
            "current_mode": repo.get_editor_input_mode(user["tailscale_login"]),
            "modes": repo.editor_input_modes,
            "saved": False,
        },
    )


@router.post("/settings/editor", response_class=HTMLResponse)
def editor_settings_save(
    request: Request,
    mode: str = Form(...),
    user: dict = Depends(current_user),
    repo: UserRepo = Depends(_user_repo),
):
    if mode not in repo.editor_input_modes:
        raise HTTPException(400, f'Unknown input mode "{mode}".')
    repo.set_editor_input_mode(user["tailscale_login"], mode)
    return templates.TemplateResponse(
        "settings_editor.html",
        {
            "request": request,
            # Reflect the saved value in the next render.
            "user": {**user, "editor_input_mode": mode},
            "current_mode": mode,
            "modes": repo.editor_input_modes,
            "saved": True,
        },
    )
