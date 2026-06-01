"""Static legal/info pages — /privacy.

Lives at prep/web/ rather than under a bounded context because the
content cross-cuts everything (auth, BYOK, the AI flows) and there's
no domain entity behind it. Stays auth-free so the landing footer can
link directly and unauthenticated visitors can read before signing up.

Only renders on Clerk-mode deploys today — the Tailscale-mode mac-mini
install is single-user and doesn't surface a public privacy notice.
That guard keeps the route from 404'ing surprises if a Tailscale user
follows a stale link, but its primary purpose is "this content is
written for the prepcards.app product specifically."
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from prep.web.templates import templates

router = APIRouter()


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy(request: Request):
    """Plain-English privacy page. No auth dependency — visitors who
    haven't signed up yet should be able to read it before pasting
    a key or creating an account."""
    if (os.environ.get("PREP_AUTH_MODE") or "").strip() != "clerk":
        # Self-hosted Tailscale install — there's nothing to disclose
        # we don't already control end-to-end.
        raise HTTPException(404)
    return templates.TemplateResponse("privacy.html", {"request": request, "user": None})
