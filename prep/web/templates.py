"""Jinja2 templates wiring shared across all routers.

Lives at prep/web/ so the per-context routers (decks/, study/,
notify/, etc.) can render templates without importing app.py
(which would cycle).

The two context_processors plug into:
- `user`: surfaced from request.state by prep.auth.current_user
- `agent_available`: read from prep.agent's cached probe result.
  The /settings/agent/{connect,disconnect} routes update the
  underlying flag via prep.agent.set_available().
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from prep import agent as _agent_mod

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _user_context(request: Request) -> dict:
    """Surface `user` to every template that gets a Request whose
    state was populated by current_user(). Lets routes / error handlers
    omit the explicit `"user": user` entry without losing the masthead
    chip + base.html `data-editor-mode` attribute."""
    return {"user": getattr(request.state, "user", None)}


def _agent_context(request: Request) -> dict:
    """Surface `agent_available` to every template. Templates use it
    to hide AI-driven controls (Generate cards, Transform, Improve)
    and surface manual paths instead — so the app stays useful as a
    manual-flashcard SRS for users without claude installed."""
    return {"agent_available": _agent_mod.is_available}


# Cache-bust the static CSS link in base.html so deploys actually
# invalidate the browser's cached copy. Computed once at module
# import (i.e. per app boot, which lines up with each deploy since
# the container restarts on every `make deploy-stag`).
try:
    _STATIC_CSS_MTIME = int((_REPO_ROOT / "static" / "style.css").stat().st_mtime)
except OSError:
    _STATIC_CSS_MTIME = 0


def _assets_context(request: Request) -> dict:
    """Expose static-asset cache-bust tokens to all templates."""
    return {"static_css_mtime": _STATIC_CSS_MTIME}


templates = Jinja2Templates(
    directory=str(_REPO_ROOT / "templates"),
    context_processors=[_user_context, _agent_context, _assets_context],
)
