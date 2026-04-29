"""HTTP routes for the auth bounded context.

Currently just the editor input mode preference (vanilla / vim /
emacs) — a per-user setting that determines which CodeMirror
keybinding extension loads when studying a code question.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from prep.auth import current_user
from prep.auth.repo import UserRepo
from prep.web.templates import templates

router = APIRouter()


def _user_repo() -> UserRepo:
    return UserRepo()


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
