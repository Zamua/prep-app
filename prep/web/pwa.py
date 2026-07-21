"""PWA install routes: manifest + service worker + offline shell.

Intentionally NOT gated by current_user: the manifest and service
worker have to be reachable before the PWA is "installed", and the
install-from-Safari flow doesn't reliably carry Tailscale-User-Login
on its first hit. Auth kicks in for any actual app view the moment
the PWA navigates into one.

The /offline shell is un-auth-gated for the same reason: it must be
reachable and cacheable without a live session, and it renders
nothing user-specific server-side (all data comes from IndexedDB
client-side). See docs/OFFLINE.md for the full design.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from prep.web.templates import get_build_token, is_accepted_version_token, templates

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


# ---- service worker -------------------------------------------------------

# The PWA icons the manifest above references. Precached at their
# plain URLs (the SW's push handler + the manifest reference them
# unversioned).
_MANIFEST_ICONS = ("icon-192.png", "icon-512.png")

# In-process cache of the rendered precache manifest, keyed by
# (build token, root_path). The token only changes across builds and
# root_path is fixed per deploy, so this is computed once and reused
# for every /sw.js fetch.
_PRECACHE_CACHE: dict[tuple[str, str], str] = {}


def _precache_urls(token: str, root: str) -> list[str]:
    """Enumerate every URL the service worker must precache for the
    offline shell to cold-launch with no network:

    - the shell itself, fetched WITH the token in the query string so
      the asset URLs rendered into the shell match the cache keys the
      same install stores even when a deploy races the install (the
      /offline route echoes the token back),
    - the whole CSS tree at its versioned URLs (index.css @imports
      every component file, so the entire tree must be cached),
    - every module under static/js/offline/ and static/js/modules/,
      wholesale, so a new import inside a shared module can never
      silently fall outside the manifest,
    - the PWA icons the manifest references.

    The server is the only party that knows the file list, so it is
    enumerated here rather than hand-maintained in sw.js. URLs are
    scope-relative (prefixed with the deploy's root_path).
    """
    urls = [f"{root}/offline?build={token}"]
    css_root = _REPO_ROOT / "static" / "css"
    for f in sorted(css_root.rglob("*")):
        if f.is_file():
            rel = f.relative_to(css_root).as_posix()
            urls.append(f"{root}/static/css/v{token}/{rel}")
    js_root = _REPO_ROOT / "static" / "js"
    for sub in ("offline", "modules"):
        subdir = js_root / sub
        if not subdir.is_dir():
            continue
        for f in sorted(subdir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(js_root).as_posix()
                urls.append(f"{root}/static/js/v{token}/{rel}")
    for icon in _MANIFEST_ICONS:
        urls.append(f"{root}/static/pwa/{icon}")
    return urls


def _precache_json(token: str, root: str) -> str:
    key = (token, root)
    if key not in _PRECACHE_CACHE:
        _PRECACHE_CACHE[key] = json.dumps(_precache_urls(token, root))
    return _PRECACHE_CACHE[key]


@router.get("/sw.js")
def service_worker(request: Request) -> Response:
    """Serve the SW from the app's root scope (rather than /static/sw.js
    whose default scope is /static/). The browser uses the SW's URL
    path as its scope, so this URL is what determines what the SW
    controls.

    Rendered, not a plain FileResponse: static/sw.js carries two
    placeholders the server substitutes at request time:

    - __BUILD__: the deploy's build-stable token. A new build changes
      the served bytes, which is exactly the browser's trigger to
      install the new SW version; a restart of the same build changes
      nothing and triggers nothing.
    - __PRECACHE__: the JSON array of scope-relative URLs the install
      handler precaches (see _precache_urls above).

    Cache-Control: no-cache so the browser's SW update check always
    revalidates against the server instead of reusing a heuristically
    cached copy.
    """
    token = get_build_token()
    root = request.scope.get("root_path", "")
    source = (_REPO_ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    body = source.replace("__BUILD__", token).replace("__PRECACHE__", _precache_json(token, root))
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ---- offline shell --------------------------------------------------------


@router.get("/offline")
def offline_shell(request: Request, build: str | None = None):
    """The offline companion app's shell (docs/OFFLINE.md section 3).

    A standalone client-rendered page: no base.html, no identity
    provider JS, no external CDN or fonts. The page renders its real
    content from IndexedDB client-side via offline-app.js.

    The ?build= echo is load-bearing for cache consistency: the SW
    fetches the shell as /offline?build=<its own token> so the
    stylesheet URL + importmap prefix rendered into the shell match
    the cache keys the same install stores, even when a deploy lands
    between the /sw.js fetch and the precache. The query value is
    validated against the accepted token charset before being echoed;
    anything else falls back to the current build token and is never
    reflected.
    """
    token = get_build_token()
    if build and is_accepted_version_token(build):
        token = build
    return templates.TemplateResponse("offline.html", {"request": request, "build": token})
