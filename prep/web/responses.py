"""HTTP response helpers shared across routers.

Lives at prep/web/ rather than under any one bounded context because
every router needs the redirect (and probably future helpers like
htmx-aware rendering). Keeps the per-context router files focused
on the routes themselves rather than on transport concerns.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse


def redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    """Build a RedirectResponse whose Location header includes the
    request's root_path.

    FastAPI's RedirectResponse takes the URL verbatim — it does NOT
    auto-prepend root_path — so a bare /deck/foo would land outside
    the /prep/ Tailscale Serve mount and the user would see a white
    screen. This was hit on 2026-04-26; preserve it here.
    """
    prefix = request.scope.get("root_path", "") or ""
    if path.startswith("/"):
        return RedirectResponse(f"{prefix}{path}", status_code=status_code)
    return RedirectResponse(f"{prefix}/{path}", status_code=status_code)
