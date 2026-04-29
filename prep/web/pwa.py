"""PWA install routes — manifest + service worker.

Intentionally NOT gated by current_user: the manifest and service
worker have to be reachable before the PWA is "installed", and the
install-from-Safari flow doesn't reliably carry Tailscale-User-Login
on its first hit. Auth kicks in for any actual app view the moment
the PWA navigates into one.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@router.get("/manifest.json")
def manifest() -> JSONResponse:
    """Web App Manifest, dynamic so the scope/start_url match whatever
    ROOT_PATH this instance is served at (so prep vs prep-staging both
    install correctly without a hand-edited manifest each time)."""
    root = (os.environ.get("ROOT_PATH") or "").strip()
    env_label = "staging" if "staging" in root else ""
    short = "prep" + (f" ({env_label})" if env_label else "")
    return JSONResponse(
        {
            "name": f"prep · a commonplace book{' (staging)' if env_label else ''}",
            "short_name": short,
            "description": "Spaced-repetition flashcards. Learn anything.",
            "display": "standalone",
            "scope": (root + "/") or "/",
            "start_url": (root + "/") or "/",
            "background_color": "#f4ecdc",
            "theme_color": "#f5efe6",
            "icons": [
                {"src": f"{root}/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": f"{root}/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
    )


@router.get("/sw.js")
def service_worker():
    """Serve the SW from the app's root scope (rather than /static/sw.js
    whose default scope is /static/). The browser uses the SW's URL
    path as its scope, so this URL is what determines what the SW
    controls."""
    return FileResponse(_REPO_ROOT / "static" / "sw.js", media_type="application/javascript")
