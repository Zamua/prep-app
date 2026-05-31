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
