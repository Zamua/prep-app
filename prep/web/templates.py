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

import hashlib
import os
import re
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
    manual-flashcard SRS for users without claude installed.

    Per-request, per-user: on a clerk-mode multi-user deploy the
    module-level cached probe (file-presence of /data/claude-oauth-token)
    misses every BYOK row, so users who saved their own key would see
    "AI not configured" forever. We resolve `agent_for_user(uid)` here
    — if it would hand back a usable adapter, the agent is available
    for THIS request, regardless of the deploy-wide cache. When there's
    no resolved user (error pages, public surfaces), we fall back to
    the cached deploy-wide flag — the old behavior."""
    user = getattr(request.state, "user", None)
    if user is not None:
        from prep.agent.selector import agent_available_for_user

        uid = user.get("tailscale_login") if isinstance(user, dict) else None
        return {"agent_available": agent_available_for_user(uid)}
    return {"agent_available": _agent_mod.is_available}


# Cache-bust the static-asset URLs (CSS link + importmap base) in
# base.html so deploys actually invalidate the browser's cached copy.
#
# The token must be BUILD-stable, not boot-stable: the service-worker
# precache (prep/web/pwa.py) keys its cache on this value, so a token
# that changed on every process restart would push every client
# through a full SW reinstall (re-downloading the whole asset tree)
# for bytes that did not change, and multi-replica deploys would
# flip-flop /sw.js between per-pod tokens. Resolution order:
#
# 1. PREP_BUILD_ID env var already token-shaped (lowercase hex,
#    7-40 chars: a git SHA or SHA prefix): used verbatim.
# 2. PREP_BUILD_ID set but shaped differently (an image tag like
#    "v0.44.0"): sha1 of the value, first 12 hex chars.
# 3. Unset (dev): sha1 over the sorted relative paths + mtimes +
#    sizes of every file under static/ plus templates/offline.html,
#    first 12 hex chars, computed once at import. Identical bytes
#    produce an identical token across restarts; an edited asset
#    mints a new token on the next boot.
#
# The lowercase-hex 7-40 shape is load-bearing: after the "v" prefix
# strip on the versioned asset routes (prep/app.py), a token can
# never collide with a real path segment under static/js or
# static/css (the directories are "components", "modules", "offline",
# "vendor"; every file name carries a dot).

_BUILD_TOKEN_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _hash_static_tree() -> str:
    """Dev fallback: fingerprint the static tree + the offline shell
    template. Relative paths, mtimes, and sizes are enough to change
    the token whenever an asset changes, without hashing contents."""
    h = hashlib.sha1()
    files = [p for p in sorted((_REPO_ROOT / "static").rglob("*")) if p.is_file()]
    offline_tpl = _REPO_ROOT / "templates" / "offline.html"
    if offline_tpl.is_file():
        files.append(offline_tpl)
    for p in files:
        st = p.stat()
        rel = p.relative_to(_REPO_ROOT).as_posix()
        h.update(f"{rel}\n{st.st_mtime_ns}\n{st.st_size}\n".encode())
    return h.hexdigest()[:12]


def _resolve_build_token() -> str:
    raw = (os.environ.get("PREP_BUILD_ID") or "").strip()
    if _BUILD_TOKEN_RE.fullmatch(raw):
        return raw
    if raw:
        return hashlib.sha1(raw.encode()).hexdigest()[:12]
    return _hash_static_tree()


_BUILD_TOKEN = _resolve_build_token()


def get_build_token() -> str:
    """The deploy's build-stable asset token. Same value templates see
    as `static_css_mtime`; the pwa/offline routes use it for the SW
    precache manifest and the offline shell's ?build= echo."""
    return _BUILD_TOKEN


def is_accepted_version_token(segment: str) -> bool:
    """True when a versioned-asset URL segment (the part after the
    "v" prefix) is an acceptable build token: the current lowercase
    hex shape, or the legacy all-digit boot stamps still referenced
    by pages cached from pre-offline deploys. ASCII digits only:
    str.isdigit() alone also accepts Unicode digit codepoints, which
    would widen the echo/alias charset past the SW's ASCII regex."""
    return bool(_BUILD_TOKEN_RE.fullmatch(segment)) or (segment.isascii() and segment.isdigit())


def _assets_context(request: Request) -> dict:
    """Expose the static-asset cache-bust token to all templates. The
    context key keeps its legacy name (the value used to be a file
    mtime, then a boot stamp) so every template consuming
    `static_css_mtime` keeps working unchanged."""
    return {"static_css_mtime": _BUILD_TOKEN}


def _auth_provider_context(request: Request) -> dict:
    """Expose the active PREP_AUTH_MODE to templates as `auth_provider`.
    Used by the user-menu to conditionally show entries that only make
    sense on a provider with first-class user accounts (e.g. account
    delete on Clerk; Tailscale identity is proxy-managed, no upstream
    user to delete)."""
    import os

    return {"auth_provider": (os.environ.get("PREP_AUTH_MODE") or "tailscale").strip().lower()}


def _clerk_bootstrap_context(request: Request) -> dict:
    """Expose Clerk publishable key + frontend API host to base.html
    so it can load ClerkJS on every page (not just the landing). The
    JS keeps the short-lived `__session` cookie refreshed in the
    background; without it, an idle tab's POST would 401 and bounce
    through Clerk sign-in, losing form data.

    Returns Nones on Tailscale-mode deploys; base.html's `{% if %}`
    guard then renders nothing.
    """
    import base64
    import os

    if (os.environ.get("PREP_AUTH_MODE") or "").strip() != "clerk":
        return {"clerk_publishable_key": None, "clerk_frontend_api_host": None}
    pk = (os.environ.get("CLERK_PUBLISHABLE_KEY") or "").strip()
    if not pk or "_" not in pk:
        return {"clerk_publishable_key": None, "clerk_frontend_api_host": None}
    # pk_<env>_<base64-encoded-frontend-api-host with trailing $>
    encoded = pk.split("_", 2)[-1]
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        host = base64.b64decode(padded).decode("ascii", errors="ignore").rstrip("$").strip()
    except Exception:  # noqa: BLE001
        host = ""
    if not host:
        return {"clerk_publishable_key": None, "clerk_frontend_api_host": None}
    return {"clerk_publishable_key": pk, "clerk_frontend_api_host": host}


def _notif_unseen_context(request: Request) -> dict:
    """Drives the masthead's "Notification log" badge — count of
    notifications the user hasn't viewed since they last opened the
    log page. Cheap COUNT() on every render; fine for a single-user
    install at this scale."""
    user = getattr(request.state, "user", None)
    if not user:
        return {"notif_unseen_count": 0}
    try:
        from prep.notify.repo import NotificationLogRepo

        return {"notif_unseen_count": NotificationLogRepo().count_unseen(user["tailscale_login"])}
    except Exception:
        return {"notif_unseen_count": 0}


def _deck_display_for_slug(uid: str | None, slug: str | None) -> str:
    """Resolve a deck's user-facing label from its URL slug, falling
    back to the slug for legacy decks (no display_name set) or when
    the lookup fails. Single indexed SELECT — cheap to call per
    render at this scale."""
    if not slug:
        return ""
    if not uid:
        return slug
    try:
        from prep.infrastructure.db import cursor

        with cursor() as c:
            row = c.execute(
                "SELECT display_name FROM decks WHERE user_id = ? AND name = ?",
                (uid, slug),
            ).fetchone()
        if row and row["display_name"]:
            return row["display_name"]
    except Exception:
        pass
    return slug


def _deck_display_context(request: Request) -> dict:
    """Bind a deck-display helper into the template scope.

    Templates that render a deck name as text — page titles, hero
    headings, breadcrumbs — call `{{ deck_display(deck_name) }}`
    instead of `{{ deck_name }}` so the user sees what they typed
    instead of the opaque slug. The closure carries the active
    user_id so each call is a single-arg lookup.
    """
    user = getattr(request.state, "user", None)
    uid = user.get("tailscale_login") if isinstance(user, dict) else None

    def deck_display(slug: str | None) -> str:
        return _deck_display_for_slug(uid, slug)

    return {"deck_display": deck_display}


templates = Jinja2Templates(
    directory=str(_REPO_ROOT / "templates"),
    context_processors=[
        _user_context,
        _agent_context,
        _assets_context,
        _auth_provider_context,
        _clerk_bootstrap_context,
        _notif_unseen_context,
        _deck_display_context,
    ],
)
